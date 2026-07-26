"""URL -> Facebook video ID.

Facebook hands out even more link shapes than Reddit, and people paste all of
them:

    facebook.com/watch/?v=<id>                the canonical watch link
    facebook.com/watch?v=<id>                 the same without the trailing slash
    facebook.com/<user>/videos/<id>/          a video on a page or profile
    facebook.com/<user>/videos/<slug>/<id>/   the same with a slug in the path
    facebook.com/reel/<id>                     a reel
    facebook.com/video.php?v=<id>              the old permalink shape
    m.facebook.com/story.php?story_fbid=<id>&id=<page>   a video inside a post
    fb.watch/<token>/                          the share sheet's shortener
    facebook.com/share/v/<token>/              the newer share link for a video
    facebook.com/share/r/<token>/              the same for a reel

The fb.watch and /share/ forms carry no video ID at all, only an opaque token,
so they have to be followed. That is the same problem /s/ poses on Reddit and
t.co on X, and it is solved the same way, through the relay when one is set,
because these hops ask facebook.com itself and a walled address fails them.

Reels are a known gap: the ID resolves but the watch URL the providers build
from it does not always serve a reel. Tracked rather than hidden; see the note
in `providers.resolve`.

The redirect is the only part of this module that can fail for reasons that
have nothing to do with the link. It says so, loudly, rather than returning
None: None means "not a Facebook link", and answering that to a good share link
sends someone off editing a URL that was never the problem.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from ..base import REFUSED, UNAVAILABLE, LinkUnresolved
from . import relay

log = logging.getLogger(__name__)

# Facebook turning us away rather than failing to answer. A login redirect is
# the usual shape and arrives as a 200, so it is caught in the providers by
# `relay.login_wall` rather than here; these are the honest refusal statuses.
_REFUSALS = frozenset({401, 403, 429})

_HOSTS = {
    "facebook.com",
    "m.facebook.com",
    "web.facebook.com",
    "mobile.facebook.com",
    "mbasic.facebook.com",
    "fb.watch",
    "fb.com",
}

# Facebook IDs are long integers. Five as a lower bound stops a short path
# segment being mistaken for one while accepting everything Facebook issues.
_ID = r"\d{5,}"
# Share tokens are opaque and alphanumeric, sometimes with dashes.
_TOKEN = r"[A-Za-z0-9_-]+"

_REEL_RE = re.compile(rf"^/reel/(?P<id>{_ID})", re.IGNORECASE)
_VIDEOS_RE = re.compile(
    rf"^/[^/]+/videos/(?:[^/]+/)?(?P<id>{_ID})", re.IGNORECASE
)
_SHARE_RE = re.compile(rf"^/share/(?:[a-z]/)?{_TOKEN}", re.IGNORECASE)
_WATCH_TOKEN_RE = re.compile(rf"^/(?P<token>{_TOKEN})/?$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


def find_url(text: str) -> Optional[str]:
    match = _URL_RE.search(text or "")
    return match.group(0) if match else None


def _host_of(url: str) -> tuple[str, httpx.URL]:
    raw = url.strip()
    if "//" not in raw:
        raw = "https://" + raw
    parsed = httpx.URL(raw)
    host = (parsed.host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host, parsed


def is_share_link(url: str) -> bool:
    """A link that hides its video ID behind a redirect that has to be followed.

    Two shapes: fb.watch/<token>, where the whole host is the shortener, and
    facebook.com/share/..., the share sheet's newer form. A plain fb.watch host
    with an empty path is not one: there is nothing to follow.
    """
    try:
        host, parsed = _host_of(url or "")
    except (httpx.InvalidURL, ValueError, TypeError, UnicodeError):
        return False
    path = parsed.path or ""
    if host == "fb.watch":
        return bool(_WATCH_TOKEN_RE.match(path))
    return host in _HOSTS and bool(_SHARE_RE.match(path))


def video_id_from_url(url: str) -> Optional[str]:
    """Extract a video ID, or None if this isn't a recognizable Facebook video."""
    if not url:
        return None

    try:
        host, parsed = _host_of(url)
    except (httpx.InvalidURL, ValueError, TypeError, UnicodeError):
        return None

    if host not in _HOSTS:
        return None

    # The ID rides in the query on the watch, video.php and story.php shapes.
    params = parsed.params
    for key in ("v", "story_fbid"):
        value = params.get(key)
        if value and value.isdigit():
            return value

    path = parsed.path or ""
    match = _REEL_RE.match(path) or _VIDEOS_RE.match(path)
    return match.group("id") if match else None


async def resolve_share_link(url: str, client: httpx.AsyncClient) -> str:
    """Follow an fb.watch or /share/ link to whatever it really points at.

    Sent through the relay when one is configured: this hop asks facebook.com
    itself, so a blocked address fails it as surely as it fails the providers.

    Raises LinkUnresolved rather than handing back the URL it was given: that
    return value parsed as no video at all and was indistinguishable from a link
    that was never Facebook's. The reason rides along, because "Facebook refused
    us" and "Facebook didn't answer in time" deserve opposite advice.
    """
    try:
        _, parsed = _host_of(url)
    except (httpx.InvalidURL, ValueError, TypeError, UnicodeError) as exc:
        raise LinkUnresolved(url) from exc

    target = str(parsed)
    try:
        status, final = await relay.redirect_of(target, client)
    except httpx.HTTPError as exc:
        log.warning("share link %s could not be followed: %s", target, exc)
        raise LinkUnresolved(url, UNAVAILABLE) from exc

    if final:
        log.info("share link %s -> %s", target, final)
        return final

    reason = REFUSED if status in _REFUSALS else UNAVAILABLE
    log.warning("share link %s was not followed: %d (%s)", target, status, reason)
    raise LinkUnresolved(url, reason)


async def extract_video_id(text: str, client: httpx.AsyncClient) -> Optional[str]:
    """Full path from pasted text to a video ID, following shortlinks if needed.

    None means "not a Facebook video link". A share link that Facebook wouldn't
    follow raises LinkUnresolved instead: a different problem, and one the person
    who sent the link can do nothing about.
    """
    if not text:
        return None

    candidate = find_url(text) or text.strip()

    video_id = video_id_from_url(candidate)
    if video_id:
        return video_id

    # Only pay for a redirect on shapes that actually need one.
    if is_share_link(candidate):
        return video_id_from_url(await resolve_share_link(candidate, client))

    return None
