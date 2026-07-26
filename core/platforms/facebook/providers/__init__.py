"""Provider chain for Facebook.

Reliability first, richness second, the same ordering as Reddit: the primary
page carries the caption and the best URLs but is the one Facebook renames out
from under us, so the no-JS mbasic page backs it. A provider that was refused
does not end the chain; only every source refusing at once is reported as a
refusal, because one wall proves nothing about the address.

The wall-in-windows behaviour is Reddit's too: Facebook does not refuse this
address per request, it refuses it for a while, so the first total refusal is
remembered and the rest of the window is answered from that memory at no cost.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from ...base import UpstreamRefused
from . import mbasic, mobilehtml

log = logging.getLogger(__name__)

CHAIN = (mobilehtml, mbasic)

# How long a total refusal is believed, in seconds. Sixty is the short end to
# err towards: guessing low costs one wasted lookup, guessing high withholds a
# service that had already come back. See the Reddit chain for the reasoning in
# full; the shape of Facebook's login wall is the same.
WALL_S = 60.0


class _Wall:
    """The last time every source refused at once, and for how long that holds.

    Armed only by `resolve`, never by a single provider being turned away: one
    of the two pages can wall while the other answers, and treating that as a
    block would take the platform down for a minute at a time on no evidence.
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


async def resolve(video_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Try each provider until one returns media.

    Reels and plain videos both arrive here as an ID and are handled the same
    way: the reel-vs-watch distinction rode in on the ID's shape and was spent
    by `urls.fetch_path` when each provider built its URL, so nothing below this
    line has to know which it is holding.
    """
    held = WALL.holding()
    if held is not None:
        log.info("facebook is still walling us, not asking again for %s", video_id)
        raise UpstreamRefused(video_id, held)

    last: dict[str, Any] = {}
    refusal: UpstreamRefused | None = None
    refusals = 0

    for provider in CHAIN:
        try:
            result = await provider.fetch(video_id, client)
        except UpstreamRefused as exc:
            refusal = exc
            refusals += 1
            continue

        if result.get("items"):
            log.info(
                "resolved %s via %s (%d item(s))",
                video_id, provider.__name__, len(result["items"]),
            )
            WALL.clear()
            return {**result, "source": provider.__name__.rsplit(".", 1)[-1]}
        last = result or last
        log.info("%s had no media for %s, trying next", provider.__name__, video_id)

    if refusal is not None and refusals == len(CHAIN):
        log.warning(
            "every facebook source refused %s (%d); holding that answer for %.0fs",
            video_id, refusal.status, WALL.seconds,
        )
        WALL.hit(refusal.status)
        raise refusal

    # Something answered, so whatever the last refusal was, it is over.
    WALL.clear()
    return last
