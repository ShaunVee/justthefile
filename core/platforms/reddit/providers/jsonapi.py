"""Fallback provider: Reddit's own post JSON.

Cleaner data than scraping, and unusable on its own: it returns 403 in bursts
from a single IP with no warning and no Retry-After, then recovers. That makes
it a good second opinion and a bad primary.

It earns its place on the cases the HTML is thin on, chiefly crossposts, where
the media belongs to the parent post and the child page only references it.

If this ever needs to become the primary source, the supported route is an
OAuth client-credentials token against oauth.reddit.com, which lifts the limit
to roughly 100 requests/minute. That needs a client id and secret in the
environment, so it is a deployment decision rather than a code one.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from core.models import PHOTO, VIDEO, MediaItem, Variant

from ...base import UpstreamRefused
from .. import dash, relay

log = logging.getLogger(__name__)

ENDPOINT = "https://www.reddit.com/comments/{post_id}/.json"

# See oldhtml: a refusal is not a miss, and must not fall through as one.
_REFUSALS = frozenset({401, 403, 429})


def _post_data(payload: Any) -> Optional[dict[str, Any]]:
    """The post itself, out of the [post listing, comments listing] envelope."""
    try:
        children = payload[0]["data"]["children"]
        return children[0]["data"]
    except (KeyError, IndexError, TypeError):
        return None


def _video_id(post: dict[str, Any]) -> Optional[str]:
    """v.redd.it ID, following a crosspost to the parent that owns the media."""
    for candidate in (post, *(post.get("crosspost_parent_list") or [])):
        media = (candidate.get("media") or {}).get("reddit_video") or {}
        fallback = media.get("fallback_url") or ""
        if "v.redd.it/" in fallback:
            return fallback.split("v.redd.it/")[1].split("/")[0]
        url = candidate.get("url") or ""
        if "v.redd.it/" in url:
            return url.split("v.redd.it/")[1].split("/")[0].split("?")[0]
    return None


def _gallery(post: dict[str, Any]) -> list[MediaItem]:
    items = (post.get("gallery_data") or {}).get("items") or []
    metadata = post.get("media_metadata") or {}
    out: list[MediaItem] = []

    for entry in items:
        meta = metadata.get(entry.get("media_id")) or {}
        source = meta.get("s") or {}
        # `u` is a signed preview URL; the media_id on i.redd.it is the original.
        mime = meta.get("m") or "image/jpeg"
        extension = mime.rsplit("/", 1)[-1].replace("jpeg", "jpg")
        url = f"https://i.redd.it/{entry.get('media_id')}.{extension}"
        out.append(
            MediaItem(
                kind=PHOTO,
                variants=(Variant(url=url, content_type=mime),),
                width=source.get("x"),
                height=source.get("y"),
                thumbnail=url,
            )
        )
    return out


async def parse(
    payload: Any, post_id: str, client: httpx.AsyncClient
) -> dict[str, Any]:
    post = _post_data(payload)
    if post is None:
        return {}

    items: list[MediaItem] = []
    video_id = _video_id(post)

    if video_id:
        variants, audio_url, duration = await dash.fetch(video_id, client)
        if variants:
            items.append(
                MediaItem(
                    kind=VIDEO,
                    variants=tuple(variants),
                    duration_s=duration,
                    width=variants[0].width,
                    height=variants[0].height,
                    thumbnail=post.get("thumbnail") if str(
                        post.get("thumbnail") or ""
                    ).startswith("http") else None,
                    audio_url=audio_url,
                )
            )
    elif post.get("is_gallery"):
        items.extend(_gallery(post))
    else:
        url = post.get("url") or ""
        if "i.redd.it/" in url:
            items.append(
                MediaItem(
                    kind=PHOTO,
                    variants=(Variant(url=url, content_type="image/jpeg"),),
                    thumbnail=url,
                )
            )

    return {
        "items": items,
        "author": post.get("author"),
        "text": post.get("title"),
    }


async def fetch(post_id: str, client: httpx.AsyncClient) -> dict[str, Any]:
    try:
        response = await relay.get(ENDPOINT.format(post_id=post_id), client)
    except httpx.HTTPError as exc:
        log.info("reddit json fetch failed for %s: %s", post_id, exc)
        return {}

    # 403 here was long treated as routine rate limiting and swallowed. It is
    # also exactly what a blocked address gets, forever, and swallowing it is
    # what turned a wall into "there's nothing in that post".
    if response.status_code in _REFUSALS:
        # INFO, not WARNING: this endpoint is refused in bursts as a matter of
        # course, and through a relay it may be refused permanently while the
        # HTML page sails through. The chain treats one refusal as survivable,
        # so a line per lookup here would be noise standing in front of the
        # WARNING that matters, which is every source refusing at once.
        log.info(
            "reddit json refused %s (%d)%s",
            post_id, response.status_code, " via relay" if relay.enabled() else "",
        )
        raise UpstreamRefused(post_id, response.status_code)

    if response.is_error:
        log.info("reddit json answered %d for %s", response.status_code, post_id)
        return {}

    try:
        payload = response.json()
    except ValueError as exc:
        # HTML where JSON was promised is Reddit's throttle or block page, which
        # it serves under a 200. Same wall as a 403 and it used to fall through
        # as a miss, which is how a walled lookup came back as an empty post.
        log.warning("reddit json returned non-JSON for %s: %s", post_id, exc)
        raise UpstreamRefused(post_id, response.status_code) from exc

    return await parse(payload, post_id, client)
