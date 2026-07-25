/**
 * justthefile relay: the one address in the system Reddit still talks to.
 *
 * Reddit blocks by IP, and it blocks the VPS this project runs on. Measured
 * from the server, with a browser User-Agent, on 2026-07-24:
 *
 *     www.reddit.com/r/../s/<token>   403   page titled "Blocked"
 *     old.reddit.com/comments/<id>/   403   same
 *     www.reddit.com/comments/<id>/.json  403   same
 *     oauth.reddit.com                403   (401 without a key elsewhere)
 *     v.redd.it/<id>/CMAF_720.mp4     206   fine
 *     i.redd.it/<anything>            404   fine, meaning reachable
 *
 * So the ban covers the website and not the CDNs. Video, audio and images
 * still come straight from the server as before, at full speed and no cost
 * here; only the small "what is in this post" questions come through this
 * worker, which is why the free plan's 100k requests/day is not a constraint.
 *
 * Reddit's own API would have been the supported way through and is closed:
 * self-serve app registration ended in late 2025, and new credentials are
 * granted by application under the Responsible Builder Policy.
 *
 * Two modes, because the bot asks Reddit two different kinds of question:
 *
 *     ?mode=page       fetch it, hand back the body under Reddit's own status
 *     ?mode=redirect   follow nothing, report where it points as JSON
 *
 * The second exists because a /s/ share link carries no post ID at all. Only
 * the Location header matters there, so the body is never fetched.
 *
 * Locked to reddit.com and behind a shared secret: an open relay on a public
 * address gets found and used by strangers, and this one would be used to
 * launder traffic at the exact site that blocked us.
 *
 * Answers are cached here, which is the only place caching helps. Reddit sees
 * this worker as one visitor for the whole project: the site, both bots, every
 * visitor, all of it. Ask too often as that one visitor and Reddit shuts it out
 * for tens of seconds to a few minutes, during which nothing works at all.
 * Measured against the live site: fifteen posts refused back to back, the same
 * fifteen fine twelve minutes later, and fine throughout from an address that
 * had not been asking. So the budget is small, shared, and worth not spending
 * twice on the same question.
 *
 * What gets cached is only ever an answer that arrived intact. A refusal comes
 * back as an ordinary-looking page under a 200, and caching one of those would
 * pin the outage in place for the length of the TTL: the one failure mode a
 * cache can invent on its own.
 */

// Subdomains included: old, www, oauth and the bare domain are all in play.
const ALLOWED_HOST = /^([a-z0-9-]+\.)*reddit\.com$/;

// The one header set Reddit answers. Anything more earns a 403, ordering
// included. Kept in step with core/platforms/reddit/headers.py by hand: two
// runtimes, one value, and no way to share it.
const USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36";

// How long an intact answer is reused.
//
// A post page for five minutes: what the caller takes off it are CDN links,
// which outlive that comfortably, and the site's own cache already holds a
// resolved post for fifteen. A share link for a day, because a /s/ token
// points at one post and always will.
const PAGE_TTL_S = 300;
const REDIRECT_TTL_S = 86400;

// Enough of a page to tell a real one from a "slow down" interstitial. The
// post's own div lands about 30 kB in on every page measured, so this has
// room to spare and still costs nothing next to decoding half a megabyte.
const SNIFF_BYTES = 131072;

// The marker of a real post page, and the reason this file knows anything
// about Reddit's markup: kept deliberately dumb, and in step with
// core/platforms/reddit/providers/oldhtml.py, which makes the same judgement
// for the same reason at the other end of the wire.
const POST_MARKER = 'id="thing_t3_';

/** Cache identity for one question. The target sits in the path rather than
 *  the query so nothing depends on how query strings are keyed. */
function cacheKey(origin, mode, target) {
  return new Request(`${origin}/__relay/${mode}/${encodeURIComponent(target)}`, {
    method: "GET",
  });
}

/** Did this arrive intact, or is it the wall wearing a 200? */
function looksIntact(target, buffer) {
  const head = new TextDecoder("utf-8", { fatal: false }).decode(
    buffer.slice(0, SNIFF_BYTES),
  );
  if (target.includes("/.json")) {
    const first = head.trimStart()[0];
    return first === "[" || first === "{";
  }
  return head.includes(POST_MARKER);
}

function tagged(response, state) {
  // A Response out of the cache is not mutable, so this rebuilds rather than
  // sets. The header is here to be looked at: it is the only way to know from
  // outside whether caching is doing anything at all.
  const copy = new Response(response.body, response);
  copy.headers.set("X-Relay-Cache", state);
  return copy;
}

async function pageAnswer(target) {
  const page = await fetch(target, {
    headers: { "User-Agent": USER_AGENT },
    redirect: "follow",
  });
  // Buffered rather than streamed because the body is the evidence: whether
  // this is cacheable cannot be known without reading it.
  const body = await page.arrayBuffer();
  const intact = page.status === 200 && looksIntact(target, body);

  const headers = {
    "Content-Type": page.headers.get("Content-Type") || "text/plain",
    "X-Relay-Final-Url": page.url,
    // Nothing downstream may hold on to a refusal, ever.
    "Cache-Control": intact ? `max-age=${PAGE_TTL_S}` : "no-store",
  };

  // Reddit's status is passed through untouched: a 403 here has to stay
  // distinguishable from this worker's own 401, or the caller cannot tell
  // "Reddit refused the relay too" from "your key is wrong".
  return { response: new Response(body, { status: page.status, headers }), intact };
}

async function redirectAnswer(target) {
  const hop = await fetch(target, {
    headers: { "User-Agent": USER_AGENT },
    redirect: "manual",
  });
  const location = hop.headers.get("Location");
  // Relative Locations are legal and Reddit has used them. Resolved here so
  // the caller never has to know that.
  const final = location ? new URL(location, target).toString() : null;
  // No destination means the hop did not happen, which is a refusal as often
  // as not. Only an answer that names a post is worth keeping.
  const intact = final !== null;

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

    // No key configured is a misconfiguration, not an invitation.
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
