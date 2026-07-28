"""Primary provider: the post's own page JSON, for a video or its photos.

Facebook embeds the media in a script blob on the page it serves a logged-out
browser. For a video, across the shapes that matter the keys are stable enough
to lead with, best first:

    playable_url_quality_hd    HD progressive mp4
    playable_url               SD progressive mp4
    browser_native_hd_url      the same on older story pages
    browser_native_sd_url

All four are progressive mp4 with audio already muxed, which is why this
platform stays flat PROXY. The values arrive JSON-escaped (\\/, \\u0025), so
they are decoded through json rather than by unescaping in a regex, which is
what keeps a signed fbcdn query intact.

A photo post carries its pictures in the same blob, as signed scontent.*.fbcdn
URLs the CDN also serves without a session, so they proxy exactly as the video
does. See `_extract_photos`. Which a page holds, a video or photos, is the
page's to say, so `parse` reads for both and returns whatever is there.

Fetched through the relay, because facebook.com walls the VPS and, worse,
answers a wall with a 200 login page rather than a refusal: see
`relay.login_wall`, which is why a login redirect raises here rather than
falling through as an empty post.

The exact key names are Facebook's private markup and it renames them
periodically. That churn is the tax this provider pays, and it is the reason the
chain falls through to `mbasic`, which reads a far simpler page.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx

from core.models import PHOTO, VIDEO, MediaItem, Variant

from ...base import UpstreamRefused
from .. import relay, urls

log = logging.getLogger(__name__)

# www carries the JSON blob reliably; m. increasingly serves a JS shell instead.
# The path (watch vs reel) is decided by `urls.fetch_path` from the ID's shape.
HOST = "https://www.facebook.com"

_REFUSALS = frozenset({401, 403, 429})

# Ordered best-first: the first key that matches wins the top of the ladder.
_URL_KEYS = (
    "playable_url_quality_hd",
    "playable_url",
    "browser_native_hd_url",
    "browser_native_sd_url",
)

_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property="og:title"[^>]+content="(?P<t>[^"]*)"', re.IGNORECASE
)
# Best-effort author: the first actor named in the post's JSON. None when the
# markup does not carry it, which the model tolerates.
_OWNER_RE = re.compile(r'"owner":\{[^}]*?"name":"(?P<name>[^"]+)"', re.IGNORECASE)


def _decode(raw: str) -> str:
    """Turn a JSON-escaped string value back into a real URL.

    The page ships these as the inside of a JSON string, so wrapping it in
    quotes and letting json do the unescaping is exact where a hand-rolled
    replace would miss a case and corrupt the signature.
    """
    try:
        return json.loads(f'"{raw}"')
    except (ValueError, TypeError):
        return ""


def _extract(html: str) -> list[Variant]:
    variants: list[Variant] = []
    seen: set[str] = set()
    for key in _URL_KEYS:
        for raw in re.findall(rf'"{key}":"([^"]+)"', html):
            url = _decode(raw)
            if url and url not in seen:
                seen.add(url)
                variants.append(Variant(url=url, content_type="video/mp4"))
    return variants


# A photo post embeds its pictures the same way a video does, in the page's
# script JSON, as signed scontent.*.fbcdn.net URLs the CDN serves without a
# session. Two shapes hold them:
#
#     "photo_image":{"uri":"<url>"}                      a single photo
#     "all_subattachments":{"count":N,"nodes":[ ... ]}   an album, each node a
#         Photo whose "image":{"uri":"<url>"} is one picture
#
# Only the first few of a long album are on this page; Facebook lazy-loads the
# rest behind a logged-in GraphQL call this project will not make. `_album_count`
# reads the declared total so the caller can say "5 of 10" rather than quietly
# pass off half the post as the whole of it. See the platform docstring.
_ALBUM_IMAGE_RE = re.compile(r'"__typename":"Photo"[^}]*?"image":\{"uri":"([^"]+)"')
_PHOTO_IMAGE_RE = re.compile(r'"photo_image":\{"uri":"([^"]+)"')
_ALBUM_COUNT_RE = re.compile(r'"all_subattachments":\{"count":(\d+)')
# The stable id in an fbcdn filename (<a>_<b>_<c>_n.jpg). The same picture is
# served at several sizes and repeated across the page's two data blobs; this is
# what dedupes it to one entry while keeping first-seen order.
_IMG_ID_RE = re.compile(r"/(\d+_\d+_\d+)_n\.")


def _image_id(url: str) -> str:
    match = _IMG_ID_RE.search(url)
    return match.group(1) if match else url


def _extract_photos(html: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    # Album nodes first, then a single/primary photo_image. In practice a page is
    # one or the other, so only one pattern matches and the order is the post's.
    for raw in _ALBUM_IMAGE_RE.findall(html) + _PHOTO_IMAGE_RE.findall(html):
        url = _decode(raw)
        if not url:
            continue
        key = _image_id(url)
        if key not in seen:
            seen.add(key)
            urls.append(url)
    return urls


def _album_count(html: str) -> int:
    match = _ALBUM_COUNT_RE.search(html)
    return int(match.group(1)) if match else 0


def _text(html: str) -> Optional[str]:
    match = _OG_TITLE_RE.search(html)
    if not match:
        return None
    # Facebook suffixes the site name; the caption is what a user wants.
    return match.group("t").rsplit(" | Facebook", 1)[0] or None


def _author(html: str) -> Optional[str]:
    match = _OWNER_RE.search(html)
    return match.group("name") if match else None


def parse(html: str, video_id: str) -> dict[str, Any]:
    """Map a page into the pieces the platform module needs. Pure.

    A page holds a video, or photos, or in principle both; whichever it holds is
    what comes back. Video leads the item list, since a post with a video is a
    video post, and the photos follow one item each, the shape the bot and web
    already expect from Reddit and X.
    """
    variants = _extract(html)
    photos = _extract_photos(html)
    if not variants and not photos:
        return {}

    items: list[MediaItem] = []
    if variants:
        items.append(MediaItem(kind=VIDEO, variants=tuple(variants)))
    for url in photos:
        items.append(
            MediaItem(kind=PHOTO, variants=(Variant(url=url, content_type="image/jpeg"),))
        )

    result: dict[str, Any] = {
        "items": items,
        "author": _author(html),
        "text": _text(html),
    }
    # Only part of a long album is on the page, so say five of ten rather than
    # hand over five and let it read as the whole post. Carried as a notice, not
    # folded into the caption, so the bot and web can show it as what it is.
    total = _album_count(html)
    if photos and total > len(photos):
        result["notice"] = (
            f"Showing {len(photos)} of {total} images. Facebook only serves the "
            "rest to people who are logged in."
        )
    return result


async def fetch(video_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Returns {} on any miss, so the caller can fall through.

    Except a refusal, which raises: a wall falling through as an empty result is
    indistinguishable from a video that really holds nothing, and that answer is
    the one this whole platform is built to avoid giving for a wall of ours.
    """
    try:
        response = await relay.get(HOST + urls.fetch_path(video_id), client)
    except httpx.HTTPError as exc:
        log.warning("facebook html fetch failed for %s: %s", video_id, exc)
        return {}

    if response.status_code in _REFUSALS:
        log.warning(
            "facebook refused %s (%d)%s",
            video_id, response.status_code, " via relay" if relay.enabled() else "",
        )
        raise UpstreamRefused(video_id, response.status_code)

    if response.is_error:
        # A 404 is the honest answer for a video that is gone, so a miss here
        # really is a miss and falling through to mbasic is right.
        log.warning("facebook answered %d for %s", response.status_code, video_id)
        return {}

    if relay.login_wall(response):
        # A rate limit on the address the lookups go out from, served as a 200
        # login page. It lifts on its own and no link gets round it meanwhile,
        # so it is named rather than lumped in with a plain miss.
        log.warning(
            "facebook bounced %s to a login wall%s",
            video_id, " via relay" if relay.enabled() else "",
        )
        raise UpstreamRefused(video_id, response.status_code)

    return parse(response.text, video_id)
