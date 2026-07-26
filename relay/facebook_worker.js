/**
 * justthefile Facebook relay: an address Facebook will answer logged out.
 *
 * Facebook walls this project's VPS the way Reddit does, with a twist. It does
 * not refuse a datacenter IP outright, it redirects it to /login.php and serves
 * that page under a 200. So a wall looks like a video page with nothing in it
 * unless the request comes from somewhere Facebook will answer. fbcdn.net serves
 * the machine fine either way, so only the small "what is in this post" lookups
 * come through this worker; the video bytes still come straight from the server.
 *
 * Graph oEmbed would have been the supported route and now needs an app access
 * token, which is the account-and-review burden this project avoids. Hence a
 * relay locked to Facebook's hosts and behind a shared secret, so it cannot be
 * found and used as an open proxy.
 *
 * Two modes, the same pair the Reddit worker exposes:
 *
 *     ?mode=page       fetch it, hand back the body under Facebook's own status
 *     ?mode=redirect   follow nothing, report where it points as JSON
 *
 * The second exists because an fb.watch or /share/ link carries no video ID at
 * all, only a token, so only the Location header matters and the body is never
 * fetched.
 *
 * What gets cached is only ever an answer that arrived intact. A login wall
 * comes back as an ordinary-looking page under a 200, and caching one would pin
 * the outage in place for the length of the TTL: the one failure mode a cache
 * can invent on its own.
 */

// Facebook subdomains (www, m, web, mbasic, mobile, the bare domain) plus the
// fb.watch and fb.com shorteners.
const ALLOWED_HOST = /^([a-z0-9-]+\.)*(facebook\.com|fb\.watch|fb\.com)$/;

// The header set Facebook answers a logged-out page with. Kept in step with
// core/platforms/facebook/headers.py by hand: two runtimes, one value, and no
// way to share it.
const USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36";
const ACCEPT_LANGUAGE = "en-US,en;q=0.9";

// A video page for five minutes: what the caller takes off it are fbcdn links,
// which outlive that. A share link for a day, because a token points at one
// video and always will.
const PAGE_TTL_S = 300;
const REDIRECT_TTL_S = 86400;

// Enough of a page to tell a real one from a login wall. The playable_url blob
// and the mbasic video link both land well inside this.
const SNIFF_BYTES = 262144;

// Markers of a real video page, kept deliberately dumb and in step with the two
// providers at the other end of the wire: the primary reads playable_url from
// the script blob, the fallback reads a video_redirect or fbcdn link off mbasic.
const PAGE_MARKERS = ["playable_url", "/video_redirect/", ".fbcdn.net"];

// A landed URL on one of these paths is the login wall, not a video page, no
// matter what status it wears.
function isWall(finalUrl) {
  try {
    const path = new URL(finalUrl).pathname.replace(/\/+$/, "").toLowerCase();
    return path === "/login" || path === "/login.php" || path === "/checkpoint";
  } catch {
    return false;
  }
}

function cacheKey(origin, mode, target) {
  return new Request(`${origin}/__relay/${mode}/${encodeURIComponent(target)}`, {
    method: "GET",
  });
}

/** Did this arrive intact, or is it the wall wearing a 200? */
function looksIntact(finalUrl, buffer) {
  if (isWall(finalUrl)) {
    return false;
  }
  const head = new TextDecoder("utf-8", { fatal: false }).decode(
    buffer.slice(0, SNIFF_BYTES),
  );
  return PAGE_MARKERS.some((marker) => head.includes(marker));
}

function tagged(response, state) {
  const copy = new Response(response.body, response);
  copy.headers.set("X-Relay-Cache", state);
  return copy;
}

async function pageAnswer(target) {
  const page = await fetch(target, {
    headers: { "User-Agent": USER_AGENT, "Accept-Language": ACCEPT_LANGUAGE },
    redirect: "follow",
  });
  const body = await page.arrayBuffer();
  const intact = page.status === 200 && looksIntact(page.url, body);

  const headers = {
    "Content-Type": page.headers.get("Content-Type") || "text/plain",
    // The providers read this to catch a login wall, which the request landed on
    // at the far end where they cannot see it.
    "X-Relay-Final-Url": page.url,
    "Cache-Control": intact ? `max-age=${PAGE_TTL_S}` : "no-store",
  };

  // Facebook's status is passed through untouched, so a real refusal stays
  // distinguishable from this worker's own 401.
  return { response: new Response(body, { status: page.status, headers }), intact };
}

async function redirectAnswer(target) {
  const hop = await fetch(target, {
    headers: { "User-Agent": USER_AGENT, "Accept-Language": ACCEPT_LANGUAGE },
    redirect: "manual",
  });
  const location = hop.headers.get("Location");
  const final = location ? new URL(location, target).toString() : null;
  // A destination that is itself the login wall is no answer at all.
  const intact = final !== null && !isWall(final);

  return {
    response: new Response(JSON.stringify({ status: hop.status, final }), {
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": intact ? `max-age=${REDIRECT_TTL_S}` : "no-store",
      },
    }),
    intact,
  };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (!env.RELAY_KEY || request.headers.get("X-Relay-Key") !== env.RELAY_KEY) {
      return new Response("unauthorized\n", { status: 401 });
    }

    const target = url.searchParams.get("url") || "";
    let host;
    try {
      host = new URL(target).hostname.toLowerCase();
    } catch {
      return new Response("bad url\n", { status: 400 });
    }
    if (!ALLOWED_HOST.test(host)) {
      return new Response("host not allowed\n", { status: 400 });
    }

    const mode = url.searchParams.get("mode") === "redirect" ? "redirect" : "page";
    const cache = caches.default;
    const key = cacheKey(url.origin, mode, target);

    const hit = await cache.match(key);
    if (hit) {
      return tagged(hit, "hit");
    }

    const answer =
      mode === "redirect" ? await redirectAnswer(target) : await pageAnswer(target);

    if (answer.intact) {
      ctx.waitUntil(cache.put(key, answer.response.clone()));
    }
    return tagged(answer.response, "miss");
  },
};
