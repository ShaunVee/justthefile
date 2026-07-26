"""Primary provider: the video's own page JSON.

Facebook embeds the media in a script blob on the page it serves a logged-out
browser. Across the shapes that matter the keys are stable enough to lead with,
best first:

    playable_url_quality_hd    HD progressive mp4
    playable_url               SD progressive mp4
    browser_native_hd_url      the same on older story pages
    browser_native_sd_url

All four are progressive mp4 with audio already muxed, which is why this
platform stays flat PROXY. The values arrive JSON-escaped (\\/, \\u0025), so
they are decoded through json rather than by unescaping in a regex, which is
what keeps a signed fbcdn query intact.

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

from core.models import VIDEO, MediaItem, Variant

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
    """Map a video page into the pieces the platform module needs. Pure."""
    variants = _extract(html)
    if not variants:
        return {}
    return {
        "items": [MediaItem(kind=VIDEO, variants=tuple(variants))],
        "author": _author(html),
        "text": _text(html),
    }


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
