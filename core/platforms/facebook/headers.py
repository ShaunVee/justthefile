"""The header set Facebook answers a logged-out page request with.

A plain desktop browser User-Agent. The page that carries the playable_url blob
is the one a logged-out browser is served, so anything browser-shaped is what
gets it: a bot-shaped or empty UA is answered with a login redirect far sooner.

A browser User-Agent is no longer enough on its own. Facebook now withholds the
playable_url blob unless the request also *looks* like a top-level navigation,
not a script's background fetch:

  * Without an `Accept: text/html` header it serves a media-less JS shell, the
    page a `fetch()` gets, and the extraction finds nothing in it.
  * Without `Sec-Fetch-Mode: navigate` the /reel/ path answers 400 outright, so
    a reel never even reaches a page to read. The /watch path tolerates its
    absence, but a reel is the shape people paste, so both headers are sent
    every time rather than guessed at per path.

Both were missing, which is how every Facebook link came to report an empty
post: the blob had moved behind a header the request did not send, not out of
the page. It is still inline HTML, so the providers' regex is unchanged.

In its own module because every Facebook-bound page request needs the same
value: both providers and the share-link redirect in `urls`. Sent from one
place, `relay`, which is also where those requests get routed when Facebook is
walling this server's address. The same values are repeated once more in
`relay/facebook_worker.js`, because the far end of that route is a different
runtime with no way to import this.
"""

from __future__ import annotations

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    # Facebook varies what it serves by locale; pinning one keeps the markup the
    # extraction reads against stable rather than shifting under it.
    "Accept-Language": "en-US,en;q=0.9",
    # Marks the request a document load rather than a background fetch. Without
    # it Facebook returns the media-less JS shell, and the playable_url blob the
    # providers read is simply not in it. See the module docstring.
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    # The /reel/ path answers 400 without this; the /watch path tolerates its
    # absence. Sent on both so a reel, the shape people actually paste, works.
    "Sec-Fetch-Mode": "navigate",
}
