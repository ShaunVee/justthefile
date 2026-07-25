/* Front end.
 *
 * The server only ever hands back JSON. Media is fetched here, straight from
 * video.twimg.com, which reflects arbitrary origins in Access-Control-Allow-Origin
 * and so is readable cross-origin. That matters for more than bandwidth: the
 * `download` attribute is ignored on cross-origin URLs, so a plain link would
 * navigate to the video and play it instead of saving it under a real name.
 * Reading the response into a blob gives us a same-origin blob: URL, where
 * `download` is honoured, at the cost of buffering the file in memory.
 */

const form = document.getElementById("resolve-form");
const input = document.getElementById("url");
const submit = document.getElementById("submit");
const pasteButton = document.getElementById("paste");
const fieldIcon = document.getElementById("field-icon");
const message = document.getElementById("message");
const results = document.getElementById("results");
const template = document.getElementById("media-card");
const batch = document.getElementById("batch");
const downloadAllButton = document.getElementById("download-all");
const batchStatus = document.getElementById("batch-status");

const KIND_LABELS = { video: "Video", gif: "GIF", photo: "Photo" };

/* Sprite symbol per platform name. A platform with no glyph yet falls back to
   the generic link icon, so adding one to the registry never leaves a hole in
   the row. */
const PLATFORM_ICONS = { x: "i-x", reddit: "i-reddit" };

function icon(id) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#${id}`);
  svg.appendChild(use);
  return svg;
}

function say(text, kind) {
  message.textContent = text;
  message.className = `message${kind ? ` ${kind}` : ""}`;
  message.hidden = !text;
}

function humanSize(bytes) {
  if (!bytes) return "";
  const mb = bytes / (1024 * 1024);
  if (mb < 1) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${mb.toFixed(mb < 10 ? 1 : 0)} MB`;
}

function humanDuration(seconds) {
  if (!seconds) return "";
  const total = Math.round(seconds);
  const mins = Math.floor(total / 60);
  return `${mins}:${String(total % 60).padStart(2, "0")}`;
}

/* Insert the quality into the filename, so saving 1080p and 720p of the same
   post doesn't leave you with "file (1).mp4". */
function nameFor(filename, label, multipleRungs) {
  if (!multipleRungs) return filename;
  const dot = filename.lastIndexOf(".");
  if (dot < 1) return `${filename}-${label}`;
  return `${filename.slice(0, dot)}-${label}${filename.slice(dot)}`;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* Above this, a proxied file is handed to the browser as a navigation instead
   of being read here: the blob route holds the whole file in memory, which is
   a fine trade for a gallery image and a bad one for a long video. */
const STREAM_CAP_BYTES = 64 * 1024 * 1024;

/* Whether a proxied file is worth reading in JS rather than navigating to.
   Doing so buys a real progress bar, a real error when the server says no
   (a navigation would quietly save the error page as if it were the file),
   and, for "download all", knowing when one file has finished. */
function streamable(item, variant) {
  if (item.needs_mux) return false;
  return Boolean(variant.size_bytes) && variant.size_bytes <= STREAM_CAP_BYTES;
}

async function saveToDisk(url, filename, onProgress, source = "the source CDN") {
  const response = await fetch(url, {
    mode: "cors",
    credentials: "omit",
    // Mandatory. video.twimg.com hotlink-protects on Referer: it serves the
    // file to a request with no Referer, and 403s any Referer that isn't
    // x.com. The browser's default policy would attach ours on every request,
    // so without this every download fails. (Origin is fine: it only checks
    // Referer.)
    referrerPolicy: "no-referrer",
  });
  if (!response.ok) {
    throw new Error(
      response.status === 429
        ? "too many requests at once, wait a few seconds"
        : `${source} returned ${response.status}`
    );
  }

  // content-length is CORS-safelisted, so it is readable without the CDN
  // opting in via Access-Control-Expose-Headers.
  const total = Number(response.headers.get("content-length")) || 0;
  const chunks = [];
  let received = 0;

  const reader = response.body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    received += value.length;
    onProgress(received, total);
  }

  const blob = new Blob(chunks, { type: response.headers.get("content-type") || "" });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();

  // Revoking immediately can cancel the save in some browsers.
  setTimeout(() => URL.revokeObjectURL(href), 60_000);
  return received;
}

function renderCard(item, payloadMeta) {
  const node = template.content.cloneNode(true);
  const card = node.querySelector(".card");
  const img = node.querySelector(".thumb:not(.placeholder)");
  const placeholder = node.querySelector(".thumb.placeholder");
  const badge = node.querySelector(".badge");
  const meta = node.querySelector(".meta");
  const qualities = node.querySelector(".qualities");
  const progress = node.querySelector(".progress");
  const bar = node.querySelector(".bar");
  const fill = node.querySelector(".bar span");
  const progressLabel = node.querySelector(".progress-label");

  if (item.thumbnail) {
    img.src = item.thumbnail;
    img.hidden = false;
    placeholder.remove();
    img.addEventListener("error", () => { img.hidden = true; }, { once: true });
  }

  badge.textContent = KIND_LABELS[item.kind] || item.kind;

  const bits = [];
  if (item.width && item.height) bits.push(`${item.width}x${item.height}`);
  const duration = humanDuration(item.duration_s);
  if (duration) bits.push(duration);
  meta.textContent = bits.join(" · ") || "Ready to download";

  const multipleRungs = item.variants.length > 1;

  /* One rung, saved. Drives this card's own progress row either way it goes,
     and resolves to whether the file made it, so "download all" can count the
     ones that didn't. Never rejects: a failure in one file of nine is not a
     reason to abandon the other eight. */
  async function run(variant) {
    const buttons = qualities.querySelectorAll("button");
    buttons.forEach((b) => (b.disabled = true));
    progress.hidden = false;
    bar.classList.add("indeterminate");
    fill.style.width = "";
    progressLabel.classList.remove("done");

    const proxied = item.delivery !== "direct";
    // Media the browser is not allowed to fetch comes back through the server
    // instead, addressed by index rather than by URL.
    const href = proxied
      ? `/api/download?platform=${encodeURIComponent(payloadMeta.platform)}` +
        `&post_id=${encodeURIComponent(payloadMeta.post_id)}` +
        `&item=${item.index}&variant=${variant.index}`
      : variant.url;

    try {
      // A mux has no size until ffmpeg has run, so there is nothing to show
      // progress against and no reason to hold the result in memory: a plain
      // navigation hands the lot to the browser, and the server's
      // Content-Disposition names the file.
      if (proxied && !streamable(item, variant)) {
        progressLabel.textContent =
          item.needs_mux
            ? "Joining audio and video on the server\u2026"
            : "Fetching through the server\u2026";

        const anchor = document.createElement("a");
        anchor.href = href;
        anchor.download = "";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();

        // The browser owns the download from here; there is no progress event
        // to listen to, so say so rather than leaving a bar spinning forever.
        await sleep(1500);
        bar.classList.remove("indeterminate");
        fill.style.width = "100%";
        progressLabel.textContent = "Started. Check your downloads.";
        progressLabel.classList.add("done");
        return true;
      }

      progressLabel.textContent = proxied
        ? "Fetching through the server…"
        : "Starting…";
      const saved = await saveToDisk(
        href,
        nameFor(item.filename, variant.label, multipleRungs),
        (received, total) => {
          if (total) {
            bar.classList.remove("indeterminate");
            fill.style.width = `${Math.round((received / total) * 100)}%`;
            progressLabel.textContent =
              `${humanSize(received)} of ${humanSize(total)}`;
          } else {
            progressLabel.textContent = `${humanSize(received)} downloaded`;
          }
        },
        proxied ? "the server" : "the source CDN"
      );
      bar.classList.remove("indeterminate");
      fill.style.width = "100%";
      progressLabel.textContent = `Saved · ${humanSize(saved)}`;
      progressLabel.classList.add("done");
      return true;
    } catch (error) {
      bar.classList.remove("indeterminate");
      fill.style.width = "0";
      progressLabel.textContent =
        `Download failed: ${error.message}. The link may have expired; try resolving the post again.`;
      return false;
    } finally {
      buttons.forEach((b) => (b.disabled = false));
    }
  }

  item.variants.forEach((variant) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quality";
    button.appendChild(icon("i-download"));
    button.appendChild(document.createTextNode(variant.label));

    const size = humanSize(variant.size_bytes);
    if (size) {
      const tag = document.createElement("span");
      tag.className = "size";
      tag.textContent = size;
      button.appendChild(tag);
    }

    button.addEventListener("click", () => run(variant));
    qualities.appendChild(button);
  });

  // The ladder arrives best first, so index 0 is what "download all" takes:
  // choosing a quality per file is the work that button exists to skip.
  return { card, download: () => run(item.variants[0]) };
}

/* Sequential, not parallel. Nine concurrent fetches compete for the one pipe,
   and the proxied ones would land nine muxes on the one vCPU at once. Going in
   order also gives the run an honest "3 of 9" to report. */
async function downloadAll(cards) {
  downloadAllButton.disabled = true;
  batchStatus.classList.remove("done");
  let saved = 0;

  for (const [position, card] of cards.entries()) {
    batchStatus.textContent = `Downloading ${position + 1} of ${cards.length}…`;
    let ok = await card.download();
    if (!ok) {
      // One retry, unhurried. What fails here is usually the rate limiter or a
      // dropped connection, and both clear on their own within a second or two.
      batchStatus.textContent =
        `Retrying ${position + 1} of ${cards.length}…`;
      await sleep(2500);
      ok = await card.download();
    }
    if (ok) saved += 1;
    // Browsers throttle saves that arrive back to back; a gap between them
    // keeps the later files from being dropped silently.
    if (position < cards.length - 1) await sleep(400);
  }

  const failed = cards.length - saved;
  batchStatus.textContent = failed
    ? `${saved} of ${cards.length} saved. Retry the rest from the cards below.`
    : `All ${cards.length} files done. Check your downloads.`;
  batchStatus.classList.toggle("done", failed === 0);
  downloadAllButton.disabled = false;
}

async function resolve(url) {
  submit.disabled = true;
  results.replaceChildren();
  batch.hidden = true;
  say("Looking up that post…", "busy");

  try {
    const response = await fetch(`/api/resolve?url=${encodeURIComponent(url)}`);
    const payload = await response.json();

    if (!response.ok) {
      say(payload.error || "Something went wrong.", "error");
      return;
    }

    const heading = payload.author ? `From @${payload.author}` : "Ready";
    const count = payload.media.length;
    say(`${heading}: ${count} ${count === 1 ? "file" : "files"} ready.`);

    const cards = payload.media.map((item) => renderCard(item, payload));
    results.replaceChildren(...cards.map((entry) => entry.card));

    // One file needs no batch button: the card already is one.
    if (cards.length > 1) {
      batch.hidden = false;
      downloadAllButton.querySelector(".label").textContent =
        `Download all ${cards.length}`;
      batchStatus.classList.remove("done");
      batchStatus.textContent = "Best quality of each, one after another.";
      downloadAllButton.disabled = false;
      downloadAllButton.onclick = () => downloadAll(cards);
    }
  } catch {
    say("Couldn't reach the server. Check your connection and try again.", "error");
  } finally {
    submit.disabled = false;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const url = input.value.trim();
  if (url) resolve(url);
});

/* A long URL scrolls the field to its tail, hiding the start behind the caret
   and leaving the button edge where the text runs out: you can't see what you
   pasted. Snapping back to the left rests the field on the start of the link,
   the part that names the post, so it can be read before submitting. */
function showLinkStart() {
  input.scrollLeft = 0;
}

input.addEventListener("blur", showLinkStart);

/* readText is the gate: iOS Safari and Chrome on Android have it behind a tap,
   which is exactly this button. Firefox on desktop doesn't, so there the link
   icon stays and nothing here runs. */
if (navigator.clipboard && navigator.clipboard.readText) {
  pasteButton.hidden = false;
  fieldIcon.hidden = true;

  pasteButton.addEventListener("click", async () => {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (!text) return;
      input.value = text;
      // Rest on the start of the link, not the tail the caret would scroll to,
      // so it can be read before submitting. Submitting stays deliberate: the
      // paste fills the box, the button does the download.
      input.setSelectionRange(0, 0);
      showLinkStart();
    } catch {
      // Denied or empty. Fall back to a normal focus so the manual paste
      // gesture is one tap away.
      input.focus();
    }
  });
}

// Deep link support: /?url=... resolves on load, so the site can be wired up as
// a share target or a bookmarklet.
/* Keep the platform row in step with the registry, so shipping a new platform
   is a server change only. The markup carries the current list as a fallback,
   so a failed request leaves the page correct rather than blank. */
async function showSupported() {
  try {
    const response = await fetch("/api/platforms");
    if (!response.ok) return;

    const { platforms } = await response.json();
    if (!platforms.length) return;

    const row = document.getElementById("supported");
    row.replaceChildren(
      ...platforms.map((platform) => {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.appendChild(icon(PLATFORM_ICONS[platform.name] || "i-link"));
        chip.appendChild(document.createTextNode(platform.label));
        return chip;
      })
    );

    showBots(platforms);
  } catch {
    /* Leave the fallback chips in place. */
  }
}

/* One card per platform, carrying that platform's logo. A platform whose
   TELEGRAM_BOT is unset still gets a card, marked as pending: the row is then
   a roadmap rather than a silent omission. */
function showBots(platforms) {
  const grid = document.getElementById("bots");

  grid.replaceChildren(
    ...platforms.map((platform) => {
      const live = Boolean(platform.telegram_bot);
      const card = document.createElement(live ? "a" : "span");
      card.className = live ? "bot" : "bot pending";

      if (live) {
        card.href = `https://t.me/${platform.telegram_bot}`;
        card.rel = "noopener";
        card.target = "_blank";
      }

      const brand = icon(PLATFORM_ICONS[platform.name] || "i-link");
      brand.setAttribute("class", "icon brand");
      card.appendChild(brand);

      const text = document.createElement("span");
      text.className = "bot-text";

      const name = document.createElement("strong");
      name.textContent = platform.label;
      text.appendChild(name);

      const handle = document.createElement("span");
      handle.className = "handle";
      handle.textContent = live
        ? `@${platform.telegram_bot}`
        : "Bot coming soon. Use the box above.";
      text.appendChild(handle);
      card.appendChild(text);

      if (live) {
        const go = icon("i-telegram");
        go.setAttribute("class", "icon go");
        card.appendChild(go);
      }

      return card;
    })
  );
}

showSupported();

const preset = new URLSearchParams(location.search).get("url");
if (preset) {
  input.value = preset;
  resolve(preset);
} else {
  input.focus();
}
