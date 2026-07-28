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

A reel resolves to an ID tagged with REEL_PREFIX, so the providers fetch it
from the /reel/ path rather than the watch path, which does not reliably serve
one. See `fetch_path`.

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

# A reel is the same kind of page as a watch video, carrying the same
# playable_url blob, but it lives at a different path and the watch path does
# not reliably serve one. So the ID a reel resolves to is tagged with this
# prefix, which `fetch_path` reads back to ask the right page. The rest of the
# system treats the ID as opaque, so the prefix rides through the cache key and
# everywhere else untouched.
REEL_PREFIX = "reel:"

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
    reel = _REEL_RE.match(path)
    if reel:
        return f"{REEL_PREFIX}{reel.group('id')}"
    videos = _VIDEOS_RE.match(path)
    return videos.group("id") if videos else None


def fetch_path(post_id: str) -> str:
    """The page path to fetch for a resolved ID, reel or plain video.

    A reel and a watch video are the same kind of page carrying the same media,
    but they live at different paths and the watch path does not reliably serve
    a reel. The REEL_PREFIX set by `video_id_from_url` is what survives this far
    to tell them apart; the providers prepend their own host to what this
    returns.
    """
    if post_id.startswith(REEL_PREFIX):
        return f"/reel/{post_id[len(REEL_PREFIX):]}"
    return f"/watch/?v={post_id}"


async def resolve_share_link(url: str, client: httpx.AsyncClient) -> str:
    """Follow an fb.watch or /share/ link to whatever it really points at.

    Fetched as a page, following the whole redirect chain, and the URL it lands
    on is read back from `relay.landed_on`. Facebook now answers the *first* hop
    of a share link with a 302 to /login and only reveals the canonical /reel/ or
    /watch address further down the chain, so reading a single hop's Location,
    which is what this did, saw the wall and gave up on a link that resolves fine
    when followed to the end. That is why a shared video read as refused while the
    reel it pointed at, pasted directly, played.

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
        response = await relay.get(target, client)
    except httpx.HTTPError as exc:
        log.warning("share link %s could not be followed: %s", target, exc)
        raise LinkUnresolved(url, UNAVAILABLE) from exc

    # A wall arrives two ways: an honest refusal status, or a 200 whose chain
    # ended on the login page. Both mean this address was turned away, not that
    # the link is bad, so both are reported as a refusal rather than left to
    # re-parse as "not a Facebook link".
    if response.status_code in _REFUSALS:
        log.warning("share link %s was refused: %d", target, response.status_code)
        raise LinkUnresolved(url, REFUSED)
    if relay.login_wall(response):
        log.warning("share link %s bounced to a login wall", target)
        raise LinkUnresolved(url, REFUSED)
    if response.is_error:
        log.warning("share link %s answered %d", target, response.status_code)
        raise LinkUnresolved(url, UNAVAILABLE)

    final = relay.landed_on(response)
    # Landing back on a share link is the chain not resolving: Facebook served
    # the token page without ever redirecting to the video. Reported as
    # unresolved rather than returned, for the same reason a wall is.
    if final and not is_share_link(final):
        log.info("share link %s -> %s", target, final)
        return final

    log.warning("share link %s did not resolve to a video", target)
    raise LinkUnresolved(url, UNAVAILABLE)


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
