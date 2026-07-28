"""Facebook.

The same split that keeps Reddit alive is the reason this platform fits. The
page lookup is IP-walled from the VPS and login-walled on top, so it routes
through a relay, exactly like Reddit. The media CDN is not walled: an fbcdn.net
URL is signed with an expiry, not a session, so once extracted it downloads
straight from the server at full speed. Only the small "what is in this post"
question needs an address Facebook will answer.

Delivery is flat PROXY, not Reddit's per-item PROXY_MUX. The endpoints here hand
back a progressive mp4 with audio already muxed, so there is nothing to join and
no second CDN that behaves differently. fbcdn.net sends no permissive CORS
header, so the browser cannot fetch it and the server streams it through. Pull
from Facebook's DASH manifest instead of the progressive URL and that stops
being true: audio splits into its own file and this becomes PROXY_MUX like
Reddit. v1 stays progressive-only to avoid that.

The extraction itself lives in `providers/` and `urls.py` beside this file.
This module is the platform contract over the top of them, and is the only
thing the registry or a bot needs to import.
"""

from __future__ import annotations

import httpx

from ..base import PROXY, Resolution
from . import providers, urls

NAME = "facebook"
LABEL = "Facebook"
HOSTS = ("facebook.com", "fb.watch", "fb.com")

# The handle of this platform's own bot, without the @. The site links it in
# the bot row and the README advertises it; a matching Profile lives in
# bot/profile.py and a bot-facebook service in docker-compose.yml runs it.
TELEGRAM_BOT = "fbook_downloader_bot"

# Hosts the download endpoint may fetch from. A safety net: every URL it handles
# already came from our own extraction, never from the caller. The allowlist
# check in web/download.py already matches subdomains by suffix, so the single
# apex entry covers video.xx.fbcdn.net, scontent-*.fbcdn.net and the rest.
MEDIA_HOSTS = ("fbcdn.net",)

# fbcdn.net does not reflect arbitrary origins in Access-Control-Allow-Origin,
# so a cross-origin fetch from the page is blocked and the server streams it
# through. Flat, because the progressive mp4 already carries its audio.
DELIVERY = PROXY


async def identify(url: str, client: httpx.AsyncClient) -> str | None:
    """Facebook video ID, or None if this link isn't Facebook's.

    Follows fb.watch and /share/ links, which is a network call on the way to
    deciding whether we even handle the URL: the same shape /s/ poses on Reddit
    and t.co on X, solved the same way.
    """
    return await urls.extract_video_id(url, client)


async def fetch(post_id: str, client: httpx.AsyncClient) -> Resolution:
    result = await providers.resolve(post_id, client)
    return Resolution(
        platform=NAME,
        post_id=post_id,
        items=tuple(result.get("items") or ()),
        author=result.get("author"),
        text=result.get("text"),
        source=result.get("source"),
        notice=result.get("notice"),
    )
