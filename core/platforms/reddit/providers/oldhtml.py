"""Primary provider: the old.reddit.com post page.

Reddit's own JSON is the obvious source and it is not dependable. Across
testing it returned 403 in bursts after a handful of requests and then
recovered, which for a site serving many visitors from one VPS IP means
failures that look random. old.reddit.com answered every request in the same
runs, so the *reliable* source is the HTML and the *rich* source is the JSON.
This is the same split as X's syndication and fxtwitter pair.

The page is server-rendered and every field worth having sits in `data-`
attributes on the post's own div, so this is attribute lookup rather than
real HTML parsing. Those attributes have been stable for the decade old.reddit
has existed, but it is still scraping: `providers.resolve` falls through to the
JSON provider when anything here comes up empty.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

import httpx

from core.models import PHOTO, VIDEO, MediaItem, Variant

from ...base import UpstreamRefused
from .. import dash, relay

log = logging.getLogger(__name__)

ENDPOINT = "https://old.reddit.com/comments/{post_id}/"

# Statuses that mean "not you, and not now either": Reddit turning the caller
# away rather than failing to answer. See `...base.REFUSED`.
_REFUSALS = frozenset({401, 403, 429})

_THING_RE = re.compile(r'<div[^>]*\bid="thing_t3_[a-z0-9]+"[^>]*>', re.IGNORECASE)
_ATTR_RE = re.compile(r'data-([a-z-]+)="([^"]*)"', re.IGNORECASE)
_VREDDIT_RE = re.compile(r"^https?://v\.redd\.it/([a-z0-9]+)", re.IGNORECASE)
_IREDDIT_RE = re.compile(r"^https?://i\.redd\.it/([a-z0-9]+\.[a-z0-9]+)", re.IGNORECASE)
# Gallery images are rendered from preview.redd.it, but the same basename on
# i.redd.it is the untouched original rather than a resized, signed preview.
_PREVIEW_RE = re.compile(
    r"https://preview\.redd\.it/([a-z0-9]+)\.(jpg|jpeg|png|webp)", re.IGNORECASE
)

# One tile of the gallery's thumbnail strip:
#
#     <div class="gallery-tile gallery-navigation"
#          id="media-tile-<post id36>-<media id>" data-media-id="<media id>" …>
#
# The post's own ID is in there, which is what makes this the gallery rather
# than "every image on the page". Comments carry preview.redd.it images too,
# and sweeping the whole page for them is what turned a four-image post into
# sixty-nine: every image anyone had replied with got counted as a gallery
# item, in whatever order the comment thread happened to put them.
_TILE_TEMPLATE = r'id="media-tile-{post_id}-([a-z0-9]+)"'


def _attributes(html: str) -> dict[str, str]:
    match = _THING_RE.search(html)
    if not match:
        return {}
    return {k.lower(): v for k, v in _ATTR_RE.findall(match.group(0))}


def _photo(url: str) -> MediaItem:
    return MediaItem(
        kind=PHOTO,
        variants=(Variant(url=url, content_type="image/jpeg"),),
        thumbnail=url,
    )


def _extensions(html: str) -> dict[str, str]:
    """media id -> file extension, read off the preview URLs on the page.

    The tile markup names the media and not the file, and the extension is not
    cosmetic: i.redd.it serves `<id>.jpeg` and 404s the same id as `.jpg`.
    Reading it page-wide is safe here because only ids the gallery already
    claimed are ever looked up.
    """
    found: dict[str, str] = {}
    for name, extension in _PREVIEW_RE.findall(html):
        found.setdefault(name.lower(), extension.lower())
    return found


def _gallery_items(html: str, post_id: str) -> list[MediaItem]:
    """This post's gallery images, in the order the tile strip renders them.

    Empty when the strip isn't there, which sends the caller to the JSON
    provider and its `gallery_data` rather than guessing from stray images.
    """
    tiles = re.compile(_TILE_TEMPLATE.format(post_id=re.escape(post_id)), re.IGNORECASE)
    extensions = _extensions(html)

    seen: list[str] = []
    for media_id in tiles.findall(html):
        media_id = media_id.lower()
        if media_id not in seen:
            seen.append(media_id)

    return [
        _photo(f"https://i.redd.it/{media_id}.{extensions.get(media_id, 'jpg')}")
        for media_id in seen
    ]


async def _video_item(
    video_id: str, client: httpx.AsyncClient, thumbnail: Optional[str]
) -> Optional[MediaItem]:
    variants, audio_url, duration = await dash.fetch(video_id, client)
    if not variants:
        return None

    return MediaItem(
        kind=VIDEO,
        variants=tuple(variants),
        duration_s=duration,
        width=variants[0].width,
        height=variants[0].height,
        thumbnail=thumbnail,
        # None when the manifest carries no audio track, which is common on
        # Reddit and means the video file alone is already complete.
        audio_url=audio_url,
    )


async def parse(
    html: str, post_id: str, client: httpx.AsyncClient
) -> dict[str, Any]:
    """Map a post page into the pieces the platform module needs."""
    attrs = _attributes(html)
    if not attrs:
        log.info("no post div found for %s", post_id)
        return {}

    url = attrs.get("url") or ""
    author = attrs.get("author") or None
    items: list[MediaItem] = []

    video = _VREDDIT_RE.match(url)
    if video:
        item = await _video_item(video.group(1), client, _thumbnail(html))
        if item:
            items.append(item)
    elif attrs.get("is-gallery") == "true":
        items.extend(_gallery_items(html, post_id))
    else:
        photo = _IREDDIT_RE.match(url)
        if photo:
            items.append(_photo(f"https://i.redd.it/{photo.group(1)}"))

    return {"items": items, "author": author, "text": attrs.get("title") or None}


def _thumbnail(html: str) -> Optional[str]:
    match = _PREVIEW_RE.search(html)
    return f"https://preview.redd.it/{match.group(1)}.{match.group(2)}" if match else None


async def fetch(post_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Returns {} on any miss, so the caller can fall through.

    Except a refusal, which raises: falling through from one blocked source to
    another produces an empty result indistinguishable from a post that really
    holds nothing, and that answer sent people looking for a deleted post that
    was never deleted.
    """
    try:
        response = await relay.get(ENDPOINT.format(post_id=post_id), client)
    except httpx.HTTPError as exc:
        log.warning("old.reddit fetch failed for %s: %s", post_id, exc)
        return {}

    if response.status_code in _REFUSALS:
        log.warning(
            "old.reddit refused %s (%d)%s",
            post_id, response.status_code, " via relay" if relay.enabled() else "",
        )
        raise UpstreamRefused(post_id, response.status_code)

    if response.is_error:
        # 404 included, and that one is the honest answer: a post that does not
        # exist answers 404 with a page titled "page not found". So a miss here
        # really is a miss, and falling through to the JSON API is right.
        log.warning("old.reddit answered %d for %s", response.status_code, post_id)
        return {}

    html = response.text
    if not _attributes(html):
        # A 200 without the post's own div is not a thin post, it is not our
        # page at all: Reddit's throttle and block interstitials come back 200,
        # and a deleted post comes back 404 above. Taken at face value this
        # parsed as "the post holds nothing", which is the one answer this
        # module exists to avoid giving for a wall of ours. It is what a
        # throttled lookup reported to every visitor: "No video or images in
        # that post", about posts that were fine.
        log.warning(
            "old.reddit answered 200 with no post page for %s%s",
            post_id, " via relay" if relay.enabled() else "",
        )
        raise UpstreamRefused(post_id, response.status_code)

    return await parse(html, post_id, client)
