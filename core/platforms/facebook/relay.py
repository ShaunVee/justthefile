"""Every Facebook *website* request, and the relay they can be routed through.

Facebook walls this project's VPS the way Reddit does, with a twist: it does not
refuse outright so much as demand a login. From a datacenter IP a page request
is answered with a redirect to /login.php, served under a 200. Taken at face
value that is a post page with no video in it, which is how a wall comes to be
reported as "nothing in that post". Meanwhile fbcdn.net serves the same machine
happily, so media keeps coming straight from the server and only the small
lookups need to originate somewhere else.

Facebook's Graph oEmbed would have been a supported route and it now needs an
app access token, which is the account-and-review burden this project exists to
avoid. Hence a relay rather than a token.

Set FACEBOOK_RELAY_URL (and the matching FACEBOOK_RELAY_KEY) and the page
requests in this package go through the worker in `relay/facebook_worker.js`.
Leave it unset and they go straight out, which is what a laptop, a clean IP and
the test suite all want. Nothing else in the codebase changes shape either way.

The environment is read per call rather than at import, for the same reason the
Reddit relay reads it per call: a module-level constant freezes whatever was set
when the first import happened, which is wrong for a test that sets the variable
and wrong for a process that reloads config.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import httpx

from ..base import RelayMisconfigured
from .headers import HEADERS

log = logging.getLogger(__name__)

URL_VAR = "FACEBOOK_RELAY_URL"
KEY_VAR = "FACEBOOK_RELAY_KEY"

# Where the worker reports the request really landed, since the hops happen at
# its end. Named in `relay/facebook_worker.js`, which sets it.
RELAY_FINAL_URL_HEADER = "X-Relay-Final-Url"


def _relay() -> tuple[str, str]:
    return (
        os.environ.get(URL_VAR, "").strip(),
        os.environ.get(KEY_VAR, "").strip(),
    )


def enabled() -> bool:
    """Whether Facebook website requests are being routed through the relay."""
    return bool(_relay()[0])


def _reject_unauthorized(status: int) -> None:
    """Raise on the relay's own 401, which only ever means a mismatched key.

    Facebook cannot produce this: the caller is talking to the worker, and the
    worker answers 401 before it fetches anything. Kept distinct from Facebook's
    own refusal so a one-line config error does not read as "Facebook is
    blocking us" in every log.
    """
    if status != 401:
        return
    log.error(
        "relay rejected our key: check %s matches the worker's RELAY_KEY", KEY_VAR
    )
    raise RelayMisconfigured(
        f"the Facebook relay rejected our key: {KEY_VAR} does not match the "
        "worker's RELAY_KEY"
    )


def landed_on(response: httpx.Response) -> str:
    """Where the request actually ended up, redirects and relay included.

    Through the relay the hops happen at the far end, so httpx only ever sees
    the worker's own address. The worker reports the real destination in a
    header for exactly this reason, and without it the login wall is invisible
    from here. See `login_wall`.
    """
    return response.headers.get(RELAY_FINAL_URL_HEADER) or str(response.url)


def login_wall(response: httpx.Response) -> bool:
    """Whether Facebook answered by demanding a login.

    Its logged-out limit does not refuse: it redirects to /login.php (or
    /login/) and serves that page under a 200. Taken at face value it is a post
    page with nothing in it, which is how a rate limit comes to be reported as
    an empty post to everyone who pastes a link during one. The check is on the
    landed path so a real page whose URL merely contains the word login is not
    mistaken for it.
    """
    try:
        path = httpx.URL(landed_on(response)).path or ""
    except (httpx.InvalidURL, ValueError, TypeError, UnicodeError):
        return False
    stem = path.rstrip("/").lower()
    return stem in ("/login", "/login.php", "/checkpoint")


async def get(
    url: str, client: httpx.AsyncClient, *, timeout: float = 15.0
) -> httpx.Response:
    """Fetch a Facebook page, through the relay when one is configured.

    The response carries Facebook's own status either way, so callers keep
    treating a refusal as Facebook refusing them. A 401 is the relay itself
    refusing, which means the shared secret is wrong, and that is raised rather
    than returned: handed back as a status it is indistinguishable from
    Facebook's own refusal, and every caller then reports a wrong key as a
    blocked address.
    """
    base, key = _relay()
    if not base:
        return await client.get(
            url, headers=HEADERS, timeout=timeout, follow_redirects=True
        )

    response = await client.get(
        base,
        params={"url": url, "mode": "page"},
        headers={"X-Relay-Key": key},
        timeout=timeout,
        follow_redirects=True,
    )
    _reject_unauthorized(response.status_code)
    return response


async def redirect_of(
    url: str, client: httpx.AsyncClient, *, timeout: float = 10.0
) -> tuple[int, Optional[str]]:
    """Where `url` points, as (status, destination or None).

    Split from `get` because an fb.watch or /share/ link is only ever asked one
    question: what does this token mean. The body behind it is never wanted, and
    through the relay it is never even fetched.

    None as the destination means the hop did not happen, and the status says
    whether that was a refusal or something else. Both are the caller's call.
    """
    base, key = _relay()

    if not base:
        response = await client.get(
            url, headers=HEADERS, follow_redirects=True, timeout=timeout
        )
        final = str(response.url)
        moved = response.is_success and final != url
        return response.status_code, final if moved else None

    response = await client.get(
        base,
        params={"url": url, "mode": "redirect"},
        headers={"X-Relay-Key": key},
        timeout=timeout,
        follow_redirects=True,
    )
    _reject_unauthorized(response.status_code)
    if not response.is_success:
        return response.status_code, None

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        # The relay answering with something other than its own JSON means the
        # relay is broken, not Facebook. Reported as a 502 so it cannot be
        # mistaken for Facebook's own refusal.
        log.warning("relay returned unreadable JSON for %s: %s", url, exc)
        return 502, None

    final = payload.get("final")
    return int(payload.get("status") or 0), final if isinstance(final, str) else None
