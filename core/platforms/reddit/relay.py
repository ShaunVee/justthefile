"""Every Reddit *website* request, and the relay they can be routed through.

Reddit bans by IP address and the ban covers reddit.com only: from the VPS
this project runs on, every reddit.com request answers 403 with a page titled
"Blocked", while v.redd.it and i.redd.it serve the same machine happily. So
media keeps coming straight from the server at full speed, and only the small
"what is in this post" questions need to originate somewhere else.

Reddit's own API was the supported way through and is shut: self-serve app
registration ended in late 2025 and new credentials now need an approved
application. Hence a relay rather than a token.

Set REDDIT_RELAY_URL (and the matching REDDIT_RELAY_KEY) and the three website
requests in this package go through the worker in `relay/worker.js`. Leave it
unset and they go straight out, which is what a laptop, a clean IP and the
test suite all want. Nothing else in the codebase changes shape either way.

The environment is read per call rather than at import. A module-level
constant would freeze whatever was set when the first import happened, which
is the wrong answer for a test that sets the variable and the wrong answer for
a process that reloads config.
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

URL_VAR = "REDDIT_RELAY_URL"
KEY_VAR = "REDDIT_RELAY_KEY"


def _relay() -> tuple[str, str]:
    return (
        os.environ.get(URL_VAR, "").strip(),
        os.environ.get(KEY_VAR, "").strip(),
    )


def enabled() -> bool:
    """Whether Reddit website requests are being routed through the relay."""
    return bool(_relay()[0])


def _reject_unauthorized(status: int) -> None:
    """Raise on the relay's own 401, which only ever means a mismatched key.

    Reddit cannot produce this: the caller is talking to the worker, and the
    worker answers 401 before it fetches anything.
    """
    if status != 401:
        return
    log.error(
        "relay rejected our key: check %s matches the worker's RELAY_KEY", KEY_VAR
    )
    raise RelayMisconfigured(
        f"the Reddit relay rejected our key: {KEY_VAR} does not match the "
        "worker's RELAY_KEY"
    )


async def get(
    url: str, client: httpx.AsyncClient, *, timeout: float = 15.0
) -> httpx.Response:
    """Fetch a reddit.com page, through the relay when one is configured.

    The response carries Reddit's own status either way, so callers keep
    treating 403 as Reddit refusing them. A 401 is the relay itself refusing,
    which means the shared secret is wrong, and that is raised rather than
    returned: handed back as a status it is indistinguishable from Reddit's
    own refusal, and every caller then reports a wrong key as a blocked
    address. Everything downstream of that reads correctly and is wrong, which
    is the expensive kind of wrong: the one place to look is the one place
    nobody is told about.
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

    Split from `get` because a /s/ share link is only ever asked one question:
    what does this token mean. The body behind it is never wanted, and through
    the relay it is never even fetched.

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
        # relay is broken, not Reddit. Reported as a 502 so it cannot be
        # mistaken for Reddit's own refusal.
        log.warning("relay returned unreadable JSON for %s: %s", url, exc)
        return 502, None

    final = payload.get("final")
    return int(payload.get("status") or 0), final if isinstance(final, str) else None
