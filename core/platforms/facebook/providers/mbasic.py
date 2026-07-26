"""Fallback provider: the no-JS mbasic page.

mbasic.facebook.com is the stripped-down version Facebook serves to browsers
that cannot run scripts. It carries far less than the main page and, crucially,
renders the video as a plain link rather than a script blob, so it survives the
markup renames that break the primary provider. It earns its place as the second
opinion for exactly that reason, the same way Reddit's JSON API backs its HTML.

Two shapes hold the URL:

    /video_redirect/?src=<url-encoded fbcdn url>   an interstitial link
    <a href="https://video.xx.fbcdn.net/...">      a direct href

Both are HTML-entity escaped on the page and the first is URL-encoded on top,
so both are unescaped before use. The result is the same progressive mp4 the
primary provider hands back, so delivery stays flat PROXY.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from typing import Any
from urllib.parse import unquote

import httpx

from core.models import VIDEO, MediaItem, Variant

from ...base import UpstreamRefused
from .. import relay, urls

log = logging.getLogger(__name__)

# The path (watch vs reel) is decided by `urls.fetch_path` from the ID's shape.
HOST = "https://mbasic.facebook.com"

_REFUSALS = frozenset({401, 403, 429})

# The interstitial link, whose src param is a URL-encoded fbcdn address.
_REDIRECT_RE = re.compile(r"/video_redirect/\?src=([^\"'&]+)", re.IGNORECASE)
# A direct fbcdn href, when mbasic renders one instead of the interstitial.
_DIRECT_RE = re.compile(
    r"https?://[^\"'<>\\]+?\.fbcdn\.net/[^\"'<>\\ ]+", re.IGNORECASE
)


def _extract(html: str) -> list[Variant]:
    urls: list[str] = []

    for encoded in _REDIRECT_RE.findall(html):
        url = unquote(html_lib.unescape(encoded))
        if url:
            urls.append(url)

    if not urls:
        for raw in _DIRECT_RE.findall(html):
            urls.append(html_lib.unescape(raw))

    seen: set[str] = set()
    variants: list[Variant] = []
    for url in urls:
        if ".fbcdn.net" in url and url not in seen:
            seen.add(url)
            variants.append(Variant(url=url, content_type="video/mp4"))
    return variants


def parse(html: str, video_id: str) -> dict[str, Any]:
    """Map an mbasic page into the pieces the platform module needs. Pure."""
    variants = _extract(html)
    if not variants:
        return {}
    return {"items": [MediaItem(kind=VIDEO, variants=tuple(variants))]}


async def fetch(video_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Returns {} on any miss. A refusal raises, for the reason in mobilehtml."""
    try:
        response = await relay.get(HOST + urls.fetch_path(video_id), client)
    except httpx.HTTPError as exc:
        log.info("facebook mbasic fetch failed for %s: %s", video_id, exc)
        return {}

    if response.status_code in _REFUSALS:
        log.info(
            "facebook mbasic refused %s (%d)%s",
            video_id, response.status_code, " via relay" if relay.enabled() else "",
        )
        raise UpstreamRefused(video_id, response.status_code)

    if response.is_error:
        log.info("facebook mbasic answered %d for %s", response.status_code, video_id)
        return {}

    if relay.login_wall(response):
        raise UpstreamRefused(video_id, response.status_code)

    return parse(response.text, video_id)
