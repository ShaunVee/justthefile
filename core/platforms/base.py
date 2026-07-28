"""The contract every platform implements.

Lives in its own module so a handler can import `Resolution` without importing
the registry that imports the handler.

A platform is a plain module exposing:

    NAME     stable slug, used in cache keys and the API payload
    LABEL    what a human is told is supported
    HOSTS    domains for the "works with…" hint on the page
    DELIVERY how the bytes can reach the user (see below)
    identify(url, client)   -> post id, or None if this isn't ours
    fetch(post_id, client)  -> Resolution

`identify` is async because recognising a link can need the network: a t.co
shortlink says nothing about its destination until it is followed.

Returning None from `identify` means "not my platform" and lets the registry
try the next one. A `Resolution` with no items means "mine, but there's nothing
in it": a different answer, and a different HTTP status.

Raising `LinkUnresolved` from `identify` is the third answer: "mine, but the
network hop that resolves it failed". Folding that into None told people their
link was unrecognisable when the link was fine and the site was not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.models import MediaItem

# How a platform's bytes can get from its CDN to the user's disk. This is a
# property of the CDN, not a preference, and it was measured rather than
# assumed: see the probe results in the README.
#
#   DIRECT     The CDN sets Access-Control-Allow-Origin, so the browser fetches
#              it and the server never sees the bytes. X only. Costs nothing.
#   PROXY      No CORS header, so a browser cannot read the response at all and
#              the server has to stream it through. TikTok.
#   PROXY_MUX  As PROXY, and audio and video are separate files that ffmpeg has
#              to join before the user gets something playable. Reddit.
#
# Telegram bots ignore this entirely: they always download server-side and
# upload to Telegram, so every platform is deliverable that way.
DIRECT = "direct"
PROXY = "proxy"
PROXY_MUX = "proxy_mux"


# Why an upstream lookup failed. The difference decides what the user is told,
# so it is carried rather than inferred:
#
#   REFUSED      the site turned us away and will keep doing so. Reddit blocks
#                by IP address, and a blocked address is blocked for every link
#                and every retry. Telling someone to try again is a lie, and
#                telling them to send a different link shape is a worse one.
#   UNAVAILABLE  a timeout, a connection error, a 5xx. Genuinely worth retrying.
REFUSED = "refused"
UNAVAILABLE = "unavailable"


class LinkUnresolved(Exception):
    """A link this platform owns, whose shortlink could not be followed.

    Only shapes that carry no ID of their own can raise this: a /s/ share link
    or a t.co, where the ID exists nowhere but at the other end of a redirect.
    Anything the parser can read on its own never touches the network and so
    never fails this way.
    """

    def __init__(self, url: str, reason: str = UNAVAILABLE) -> None:
        super().__init__(f"could not resolve {url} ({reason})")
        self.url = url
        self.reason = reason

    @property
    def refused(self) -> bool:
        return self.reason == REFUSED


class RelayMisconfigured(Exception):
    """A hop this deployment owns turned us away, rather than the site.

    Deliberately not an `UpstreamRefused`. They arrive as the same status from
    the same request and mean opposite things: one is the site walling off this
    server, which no amount of fixing here undoes, and the other is a shared
    secret that doesn't match, which nothing but fixing here undoes. Folding
    the second into the first is what makes a one-line config error read as
    "Reddit is blocking us" in every log and every reply.
    """


class UpstreamRefused(Exception):
    """Every source for a post turned us away, rather than answering emptily.

    The distinction is the whole point. A post with no media and a post we were
    not allowed to look at both used to arrive as a `Resolution` with no items,
    and the user was told the post was "deleted, private, or age-restricted"
    for a post that was none of those things and a wall that was ours.
    """

    def __init__(self, post_id: str, status: int = 0) -> None:
        super().__init__(f"every source refused {post_id} (last status {status})")
        self.post_id = post_id
        self.status = status


@dataclass(frozen=True)
class Resolution:
    """One post's media, normalized and platform-neutral.

    Deliberately not `core.models.TweetMedia`: that type is named for the one
    platform that shipped first, and the whole point of this layer is that
    nothing above it knows where a file came from. `MediaItem` and `Variant`
    are reused as-is: they were already neutral.
    """

    platform: str
    post_id: str
    items: tuple[MediaItem, ...] = field(default_factory=tuple)
    author: Optional[str] = None
    text: Optional[str] = None
    source: Optional[str] = None  # which upstream endpoint answered
    # A one-line heads-up that is not the post's own caption: set when something
    # about the result needs saying, such as an album we could only fetch part
    # of. Platforms that never have one leave it None, and both the bot and the
    # web surface it when it is present.
    notice: Optional[str] = None

    def __bool__(self) -> bool:
        return bool(self.items)
