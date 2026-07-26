"""The header set Facebook answers a logged-out page request with.

A plain desktop browser User-Agent. The page that carries the playable_url blob
is the one a logged-out browser is served, so anything browser-shaped is what
gets it: a bot-shaped or empty UA is answered with a login redirect far sooner.

In its own module because every Facebook-bound page request needs the same
value: both providers and the share-link redirect in `urls`. Sent from one
place, `relay`, which is also where those requests get routed when Facebook is
walling this server's address. The same value is repeated once more in
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
}
