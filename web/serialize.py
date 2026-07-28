"""Resolution -> the JSON shape the front end consumes.

The bot picks one variant on the user's behalf, because Telegram only accepts
one and it has to fit under 50 MB. A browser has neither constraint, so this
hands over the whole ladder and lets the user choose, which means every
rendition needs a readable label and a size to choose between.

Nothing here is platform-specific except `_RESOLUTION_RE`, which is a best
effort at reading a size out of a URL and simply finds nothing when a future
platform doesn't put one there.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

import httpx

from core.models import PHOTO, MediaItem, Variant
from core.platforms.base import DIRECT, Resolution
from core.select import head_size

# X encodes the rendition in the path: /vid/1280x720/ and /vid/avc1/720x1280/.
_RESOLUTION_RE = re.compile(r"/(\d{2,5})x(\d{2,5})/")

# X screen names are [A-Za-z0-9_] only, so anything else in the author field is
# either mangled or hostile. Dots are excluded deliberately: keeping them would
# let an author of "../.." survive into a filename the browser then saves.
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _dimensions(url: str) -> tuple[Optional[int], Optional[int]]:
    match = _RESOLUTION_RE.search(url)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _quality_label(variant: Variant, url_dims: tuple[Optional[int], Optional[int]]) -> str:
    """A rung's name, in the terms people already use for video quality.

    Quality is the *short* side: a 720x1280 portrait clip is 720p, same as a
    1280x720 landscape one. Falls back to bitrate when the URL carries no
    dimensions, which is every variant resolved through fxtwitter.
    """
    width = variant.width or url_dims[0]
    height = variant.height or url_dims[1]

    if width and height:
        return f"{min(width, height)}p"
    if variant.bitrate:
        return f"{round(variant.bitrate / 1000):d}k"
    return "Original"


def _stem(post: Resolution) -> str:
    """Filename base. Falls back to the platform slug when there's no author."""
    author = _UNSAFE_FILENAME_RE.sub("", post.author or "") or post.platform
    return f"{author}-{post.post_id}"


def _extension(url: str, content_type: str) -> str:
    path = url.split("?", 1)[0]
    _, _, tail = path.rpartition("/")
    if "." in tail:
        return tail.rsplit(".", 1)[1].lower()
    return "jpg" if content_type.startswith("image/") else "mp4"


async def _sized_variants(
    item: MediaItem, client: httpx.AsyncClient, budget: asyncio.Semaphore
) -> list[dict[str, Any]]:
    """The ladder, best first, each rung labelled and sized.

    Sizes come from concurrent HEADs: the same trick the bot uses to avoid
    committing to a download, except here it only has to populate a button.
    A rung whose size the CDN won't disclose is still offered, unlabelled.
    """
    ladder = item.mp4_variants_best_first or list(item.variants)

    async def describe(variant: Variant) -> dict[str, Any]:
        async with budget:
            size = await head_size(variant.url, client)
        if size is None:
            size = variant.estimated_bytes(item.duration_s)
        dims = _dimensions(variant.url)
        return {
            "url": variant.url,
            "label": _quality_label(variant, dims),
            "width": variant.width or dims[0],
            "height": variant.height or dims[1],
            "bitrate": variant.bitrate,
            "size_bytes": size,
            "content_type": variant.content_type,
        }

    rungs = list(await asyncio.gather(*(describe(v) for v in ladder)))
    for position, rung in enumerate(rungs):
        rung["index"] = position
    return rungs


async def describe(
    post: Resolution,
    client: httpx.AsyncClient,
    *,
    max_heads: int,
    delivery: str = DIRECT,
    item_delivery=lambda item: DIRECT,
) -> dict[str, Any]:
    """Full API payload for a resolved post.

    `delivery` tells the front end whether it may fetch the media itself or has
    to come back through us. Today every platform is DIRECT, so the browser
    ignores it, but it is in the payload from the start so adding a proxied
    platform is a server change rather than a protocol change.
    """
    budget = asyncio.Semaphore(max(1, max_heads))
    stem = _stem(post)
    multiple = len(post.items) > 1

    async def one(index: int, item: MediaItem) -> dict[str, Any]:
        variants = await _sized_variants(item, client, budget)
        suffix = f"-{index + 1}" if multiple else ""
        ext = _extension(variants[0]["url"], variants[0]["content_type"]) if variants else "mp4"
        return {
            "index": index,
            "kind": item.kind,
            "duration_s": item.duration_s,
            "width": item.width,
            "height": item.height,
            "thumbnail": item.thumbnail,
            "filename": f"{stem}{suffix}.{ext}",
            # Per item, not per platform: a Reddit clip with no audio track is
            # already a complete file and can be fetched direct, while the one
            # beside it in the same post may need muxing.
            "delivery": item_delivery(item),
            "needs_mux": item.needs_mux,
            "audio_url": item.audio_url,
            "variants": variants,
        }

    media = list(
        await asyncio.gather(*(one(i, item) for i, item in enumerate(post.items)))
    )

    return {
        "platform": post.platform,
        "post_id": post.post_id,
        "delivery": delivery,
        "author": post.author,
        "text": post.text,
        "notice": post.notice,
        "source": post.source,
        "media": [m for m in media if m["variants"]],
    }
