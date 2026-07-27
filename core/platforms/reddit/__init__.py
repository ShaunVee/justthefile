"""Reddit.

Delivery is the interesting part. Reddit uses two CDNs and they do not
behave the same way, which only showed up by measuring both:

    v.redd.it   Access-Control-Allow-Origin: *   browser may fetch it
    i.redd.it   no CORS header                   browser may not

So a silent video can go direct, a video with sound needs muxing because DASH
keeps audio in its own file, and an image needs proxying despite being the
simplest case of the three. Delivery is therefore decided per item, not per
platform.
"""

from __future__ import annotations

import httpx

from ..base import DIRECT, PROXY, PROXY_MUX, Resolution
from . import providers, urls

NAME = "reddit"
LABEL = "Reddit"
HOSTS = ("reddit.com", "redd.it")

# The Telegram bot serving this platform. One bot per platform rather than one
# bot for all of them: each gets its own name, photo and copy. The site links to
# it from the bot row and shows "coming soon" while this is unset.
TELEGRAM_BOT = "rddt_downloader_bot"

# Hosts the download endpoint may fetch from. A safety net: every URL it
# handles already came from our own extraction, never from the caller.
MEDIA_HOSTS = ("v.redd.it", "i.redd.it")

# The worst case a visitor can hit, which is what the listing should advertise.
# Per-item delivery is computed below and is what the front end actually uses.
DELIVERY = PROXY_MUX

# Shown behind the info button next to the Reddit chip and the Reddit bot's
# /start. Reddit has started refusing server-side requests, so a lookup fails
# sometimes for reasons nothing on our end can fix. The copy owns that with a
# straight face rather than pretending it never happens.
NOTE = (
    "Reddit has begun aggressively clamping down on server-originating requests "
    "due to scraping. 10% of the time, it works every time. We apologise if it "
    "does not."
)


def delivery_for(item) -> str:
    """How this particular item can reach the user.

    Reddit splits its media across two CDNs that behave differently, which was
    measured rather than assumed:

        v.redd.it   sends Access-Control-Allow-Origin: *
        i.redd.it   sends no CORS header at all

    So video can go straight to the browser when it has no separate audio
    track, and images never can, despite being the simpler case.
    """
    if item.needs_mux:
        return PROXY_MUX

    url = item.variants[0].url if item.variants else ""
    return DIRECT if "//v.redd.it/" in url else PROXY


async def identify(url: str, client: httpx.AsyncClient) -> str | None:
    return await urls.extract_post_id(url, client)


async def fetch(post_id: str, client: httpx.AsyncClient) -> Resolution:
    result = await providers.resolve(post_id, client)
    return Resolution(
        platform=NAME,
        post_id=post_id,
        items=tuple(result.get("items") or ()),
        author=result.get("author"),
        text=result.get("text"),
        source=result.get("source"),
    )
