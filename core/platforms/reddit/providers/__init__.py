"""Provider chain for Reddit.

Reliability first, richness second: the HTML page answered every request across
testing while the JSON API returned 403 in bursts, so the scrape leads and the
API is the fallback. That is the opposite ordering to X, where the structured
endpoint is the dependable one.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from ...base import UpstreamRefused
from . import jsonapi, oldhtml

log = logging.getLogger(__name__)

CHAIN = (oldhtml, jsonapi)

# How long a total refusal is believed, in seconds.
#
# Reddit does not wall this address per request, it walls it for a while.
# Measured against the live site: during a window every lookup fails, retries
# seconds apart fail with it, and then the whole thing recovers on its own.
# Requests sent into that window buy nothing. They cost an upstream request
# each, against the same budget that has to run dry before the window closes,
# and they arrive fastest exactly when someone is pressing the button again.
#
# So the first total refusal is remembered and the rest of the window is
# answered from that memory: same answer, immediately, without spending
# anything. Sixty seconds is the short end of the windows measured, which is
# the right end to err towards: guessing low costs one wasted lookup, guessing
# high withholds a service that had already come back.
WALL_S = 60.0


class _Wall:
    """The last time every source refused at once, and for how long that holds.

    Deliberately armed only by `resolve`, never by a single provider being
    turned away. The JSON endpoint 403s in bursts from addresses Reddit is
    perfectly happy with, and treating one of those as a wall would take the
    site down for a minute at a time on no evidence at all.
    """

    def __init__(self, seconds: float = WALL_S) -> None:
        self.seconds = seconds
        self._until = 0.0
        self._status = 0

    def hit(self, status: int) -> None:
        self._until = time.monotonic() + self.seconds
        self._status = status

    def holding(self) -> Optional[int]:
        """The remembered status while the window lasts, else None."""
        return self._status if time.monotonic() < self._until else None

    def clear(self) -> None:
        self._until = 0.0
        self._status = 0


WALL = _Wall()


async def resolve(post_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Try each provider until one returns media.

    A provider that was refused does not end the chain: the other one may be
    reachable, and while Reddit rate-limits the JSON in bursts that is the
    normal case rather than the exception.

    Only every source refusing is reported as a refusal. One of them being
    turned away proves nothing about the address: measured from an IP Reddit
    was perfectly happy with, the JSON endpoint 403'd and then served the same
    post a minute later. If any provider got a real answer, we were not walled,
    and an empty result is the post's own emptiness. Claiming a block there
    would trade one wrong reply for its mirror image: telling someone the site
    is blocking us when their post really is deleted.
    """
    held = WALL.holding()
    if held is not None:
        # Same answer the last lookup earned, at no cost. See `_Wall`.
        log.info("reddit is still refusing us, not asking again for %s", post_id)
        raise UpstreamRefused(post_id, held)

    last: dict[str, Any] = {}
    refusal: UpstreamRefused | None = None
    refusals = 0

    for provider in CHAIN:
        try:
            result = await provider.fetch(post_id, client)
        except UpstreamRefused as exc:
            refusal = exc
            refusals += 1
            continue

        if result.get("items"):
            log.info(
                "resolved %s via %s (%d item(s))",
                post_id, provider.__name__, len(result["items"]),
            )
            WALL.clear()
            return {**result, "source": provider.__name__.rsplit(".", 1)[-1]}
        last = result or last
        log.info("%s had no media for %s, trying next", provider.__name__, post_id)

    if refusal is not None and refusals == len(CHAIN):
        log.warning(
            "every reddit source refused %s (%d); holding that answer for %.0fs",
            post_id, refusal.status, WALL.seconds,
        )
        WALL.hit(refusal.status)
        raise refusal

    # Something answered, so whatever the last refusal was, it is over.
    WALL.clear()
    return last
