"""The bot's public identity: the blurbs Telegram shows and the command menu.

All user-facing copy lives here rather than in whatever was last typed into
BotFather, and gets pushed on every startup, so the profile is reviewable,
diffable and survives a rebuild on a fresh host.

One bot serves one platform, so the copy is per-platform too: a Reddit user
should be told about `/s/` share links, not t.co. Each profile is a `Profile`
instance in PROFILES, keyed by the platform NAME in `core.platforms`, and the
bot picks its own out at startup. Adding a platform's bot is an entry here plus
`TELEGRAM_BOT` on the platform module.

Two things the Bot API deliberately can't set, so BotFather keeps them:
the display name (rate-limited, changed rarely) and the profile photo (no
API method at all). Both are set by hand in BotFather, and both are recorded
on the profile anyway so the intended values live in the repo.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import BotCommand
from telegram.error import TelegramError

log = logging.getLogger(__name__)

# Telegram's own limits. Enforced by tests so a copy edit fails locally
# instead of silently at startup.
SHORT_DESCRIPTION_LIMIT = 120
DESCRIPTION_LIMIT = 512
COMMAND_DESCRIPTION_LIMIT = 256
# A callback-query answer shown as an alert. The note behind the /start info
# button is delivered this way, so it has to fit.
CALLBACK_ALERT_LIMIT = 200

# The menu behind the "/" button. Shared: both bots answer the same two
# commands. Only list commands that actually exist, or a menu entry becomes a
# dead end for the user. There's a test for that.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "What this bot does"),
    ("help", "How to use it"),
)


@dataclass(frozen=True)
class Profile:
    """Everything a user sees before the bot does any work."""

    # BotFather /setname. Kept here so the intended value is recorded somewhere.
    name: str
    # BotFather /setuserpic, which has no API method at all. Repo-relative.
    # One mark per bot, never assets/logo.png: that one is the site's, and a
    # bot wearing it would claim the whole project as one platform's.
    photo: str
    # Profile page, under the photo. Also what Telegram shows when the bot is
    # forwarded or shared as a link.
    short_description: str
    # The empty-chat screen, shown above the Start button before anyone taps it.
    description: str
    # In-chat reply to /start and /help.
    help: str
    # Reply when someone sends text that isn't a link this bot handles. Naming
    # the shape of a good link is the whole job of this string.
    unknown_link: str
    # Reply when the link was ours but the redirect behind it wouldn't answer.
    # Separate from unknown_link because the two ask for opposite things: one
    # wants a better link, this one wants the same link again in a minute.
    # Defaulted because it blames nothing the sender did, so it names no
    # platform and no link shape; override it where there's more to say.
    link_unresolved: str = (
        "I couldn't follow that link just now. That's on the site, not on you: "
        "send the same link again in a moment."
    )
    # Reply when the site turned us away: a 403 on our own address rather than
    # a request that failed. Separate from link_unresolved because retrying is
    # the one thing that cannot work, and because no other link shape gets
    # round it either. Whatever this says, it must not send someone off editing
    # a link or pressing send again to no effect.
    upstream_blocked: str = (
        "I can't reach that site from my server right now: it's turning my "
        "requests away. Nothing wrong with your link, and no other link will "
        "get through either. This one's mine to fix, so try again later."
    )
    # Reply when the wall was one of ours: a relay that rejected our own key
    # rather than the site rejecting our address. Identical from where the
    # sender sits and the opposite thing to fix, so it does not say "the site
    # is blocking me" and send whoever runs this looking at the wrong end.
    # Defaulted for the same reason as link_unresolved: it names nothing the
    # sender did and nothing platform-specific.
    relay_misconfigured: str = (
        "Something's misconfigured on my server, I've informed the highly trained monkey that runs me, Check back in a bit!"
    )
    # A standing caveat about the platform, shown behind an info button on
    # /start rather than buried in the help text. None where there's nothing to
    # warn about. Kept under Telegram's 200-char callback-alert limit, which a
    # test enforces. Mirrors the web note in `core.platforms`.
    note: str | None = None
    commands: tuple[tuple[str, str], ...] = COMMANDS


X = Profile(
    name="X Media Downloader",
    photo="assets/bot-x.png",
    short_description=(
        "Send an X (Twitter) post link, get the video back as an mp4. "
        "Handles multi-video posts, GIFs, images and t.co links."
    ),
    description=(
        "Send me a link to an X (Twitter) post and I'll send the video back as "
        "an mp4, playable inline and saveable.\n\n"
        "• posts with several videos, in order\n"
        "• GIFs and images\n"
        "• t.co short links\n\n"
        "Videos over Telegram's 50 MB limit are compressed to fit automatically. "
        "Anything too long to compress without ruining it comes back as a direct "
        "download link instead. Links you've sent before return instantly."
    ),
    help=(
        "Send me a link to an X (Twitter) post and I'll send the video back as "
        "an mp4, playable inline and saveable.\n\n"
        "I also handle posts with several videos, GIFs, images and t.co short "
        "links.\n\n"
        "Videos over Telegram's 50 MB limit are compressed automatically; "
        "anything too long to compress comes back as a direct download link."
    ),
    unknown_link=(
        "That doesn't look like an X post link. Send something like\n"
        "https://x.com/user/status/1234567890"
    ),
)

# Sound is the headline, not a footnote: Reddit keeps audio in a separate DASH
# file, so the mute clip is what people are used to getting and the reason
# they're looking for another downloader.
REDDIT = Profile(
    name="Reddit Media Downloader",
    photo="assets/bot-reddit.png",
    short_description=(
        "Send a Reddit post link, get the video back as an mp4 — with sound. "
        "Galleries and share links work too."
    ),
    description=(
        "Send me a link to a Reddit post and I'll send the video back as an "
        "mp4, playable inline and saveable.\n\n"
        "• video with its audio track joined back on\n"
        "• image posts and galleries at full resolution\n"
        "• redd.it and /s/ share links from the app\n\n"
        "Reddit splits audio into a separate file, which is why so many "
        "downloaders hand back silent clips. Videos over Telegram's 50 MB limit "
        "are compressed to fit automatically. Links you've sent before return "
        "instantly."
    ),
    help=(
        "Send me a link to a Reddit post and I'll send the video back as an "
        "mp4, with the audio joined back on.\n\n"
        "Share links from the app (/s/…), redd.it short links, image posts and "
        "galleries all work.\n\n"
        "Videos over Telegram's 50 MB limit are compressed automatically; "
        "anything too long to compress comes back as a direct download link."
    ),
    unknown_link=(
        "That doesn't look like a Reddit post link. Send something like\n"
        "https://reddit.com/r/videos/comments/abc123/title\n"
        "A share link from the app (/s/…) or a redd.it link works too."
    ),
    # A /s/ link is a token, not a post: it means nothing until Reddit says
    # what it points at. When Reddit won't say, the link is still fine, so the
    # copy has to keep the blame off it.
    #
    # The full-link suggestion is only offered here, on the retryable case.
    # It used to be offered on every failure, including the one where Reddit
    # was blocking the server outright, and there it was worse than useless:
    # the canonical link goes to the same blocked host and fails too, one
    # message later. Advice that cannot work reads as a brush-off.
    link_unresolved=(
        "Reddit didn't answer when I asked where that share link goes. The "
        "link is fine: send it again in a moment, or paste the post's full "
        "reddit.com/r/…/comments/… link, which needs no lookup."
    ),
    upstream_blocked=(
        "Reddit is refusing requests from my server at the moment, so I can't "
        "look up posts at all. Your link is fine and a full reddit.com link "
        "won't help: it hits the same wall. Mine to fix, so do try later."
    ),
    note=(
        "Reddit has begun aggressively clamping down on server-originating "
        "requests due to scraping. 10% of the time, it works every time. We "
        "apologise if it does not."
    ),
)

# The wall is the headline: most Facebook downloaders bounce off a "log in to
# continue" screen, so "no login" is the reason someone is looking for this one.
# Sound is not a selling point here the way it is on Reddit: Facebook's
# progressive mp4 already carries its audio, so the copy doesn't make a promise
# out of something every tool gets right.
FACEBOOK = Profile(
    name="Facebook Video Downloader",
    photo="assets/bot-facebook.png",
    short_description=(
        "Send a Facebook video, Reel or Watch link and get it back as an mp4. "
        "fb.watch and share links work too."
    ),
    description=(
        "Send me a link to a Facebook video and I'll send it back as an mp4, "
        "playable inline and saveable.\n\n"
        "• Reels, Watch clips and posts with video\n"
        "• fb.watch short links and /share/ links\n"
        "• sound already on the file, nothing to unmute\n\n"
        "No login, and no \"log in to continue\" wall. Videos over Telegram's "
        "50 MB limit are compressed to fit automatically. Links you've sent "
        "before return instantly."
    ),
    help=(
        "Send me a link to a Facebook video and I'll send it back as an mp4, "
        "playable inline and saveable.\n\n"
        "Reels, Watch clips, fb.watch short links and /share/ links all work, "
        "with the sound already on the file.\n\n"
        "Videos over Telegram's 50 MB limit are compressed automatically; "
        "anything too long to compress comes back as a direct download link."
    ),
    unknown_link=(
        "That doesn't look like a Facebook video link. Send something like\n"
        "https://www.facebook.com/watch?v=1234567890\n"
        "A Reel, an fb.watch link or a /share/ link works too."
    ),
    # fb.watch and /share/ links are tokens, not posts: they mean nothing until
    # Facebook says what they point at. When Facebook won't answer, the link is
    # still fine, so the copy keeps the blame off it and offers the canonical
    # link, which needs no lookup. Same shape as Reddit's /s/ case.
    link_unresolved=(
        "Facebook didn't answer when I asked where that link goes. The link is "
        "fine: send it again in a moment, or paste the full "
        "facebook.com/watch?v=… or /reel/… link, which needs no lookup."
    ),
    upstream_blocked=(
        "Facebook is refusing requests from my server at the moment, so I can't "
        "look up videos right now. Your link is fine and a different Facebook "
        "link won't help: it hits the same wall. Mine to fix, so do try later."
    ),
)

PROFILES: dict[str, Profile] = {"x": X, "reddit": REDDIT, "facebook": FACEBOOK}


def for_platform(name: str) -> Profile:
    """The profile for a platform. Unknown platforms are a config error."""
    try:
        return PROFILES[name]
    except KeyError:
        raise SystemExit(
            f"No bot profile for platform {name!r}. Add one to bot/profile.py."
        ) from None


async def apply(bot, profile: Profile) -> None:
    """Push the name-independent profile fields to Telegram.

    Never fatal: a transient API failure here shouldn't stop a bot that is
    otherwise ready to serve.
    """
    try:
        await bot.set_my_commands(
            [BotCommand(name, desc) for name, desc in profile.commands]
        )
        await bot.set_my_short_description(profile.short_description)
        await bot.set_my_description(profile.description)
        log.info("bot profile synced (%d commands)", len(profile.commands))
    except TelegramError as exc:
        log.warning("could not sync bot profile: %s", exc)
