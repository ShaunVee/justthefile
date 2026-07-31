"""Telegram bot entrypoint.

Long-polling, not webhooks: polling is outbound-only, so there's no public
endpoint, no TLS certificate and no inbound firewall rule to maintain, which
removes most of the friction of running this on a cloud VM.

One process serves one platform, chosen by PLATFORM at startup. Nothing below
names a platform: the module out of `core.platforms` supplies the link parser,
the extraction and its own copy, so the Reddit bot is this same code started
with a different environment variable rather than a second implementation.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    Update,
)
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from core import mux, select
from core.platforms import (
    REGISTRY,
    LinkUnresolved,
    RelayMisconfigured,
    UpstreamRefused,
)

from . import profile, transcode
from .access import chat_permitted, is_allowed
from .cache import FileIdCache
from .config import Config
from .jobs import Job, JobQueue, Limits, QueueFull
from core.models import GIF, PHOTO, MediaItem

log = logging.getLogger(__name__)

MB = 1024 * 1024
STATUS_MIN_INTERVAL = 3.0  # seconds between status edits, to stay clear of flood limits

# Headroom left for the audio track when a platform serves it separately: the
# muxed file is what has to fit under Telegram's cap, not the video alone.
# Reddit audio runs well under a tenth of the video on anything but a talking
# head, so this is generous rather than tight.
MUX_AUDIO_ALLOWANCE = 0.10

# Where a user goes when Telegram's 50 MB cap makes the bot the wrong tool.
SITE = "https://justthefile.com"


def platform_named(name: str):
    """The platform module this bot serves. Unknown names are a config error."""
    for handler in REGISTRY:
        if handler.NAME == name:
            return handler
    raise SystemExit(
        f"PLATFORM={name!r} is not a registered platform. "
        f"Known: {', '.join(h.NAME for h in REGISTRY)}."
    )


class Status:
    """A single status message, edited in place and throttled."""

    def __init__(self, message: Optional[Message]) -> None:
        self._message = message
        self._last_text = ""
        self._last_edit = 0.0

    async def set(self, text: str, *, force: bool = False) -> None:
        if self._message is None or text == self._last_text:
            return
        now = time.monotonic()
        if not force and (now - self._last_edit) < STATUS_MIN_INTERVAL:
            return
        self._last_text = text
        self._last_edit = now
        try:
            await self._message.edit_text(text)
        except BadRequest:
            pass  # message deleted, or edited to identical text
        except TelegramError as exc:
            log.debug("status edit failed: %s", exc)

    async def done(self) -> None:
        if self._message is None:
            return
        try:
            await self._message.delete()
        except TelegramError:
            pass


def _human(size: Optional[int]) -> str:
    if not size:
        return "?"
    return f"{size / MB:.1f} MB"


class Runtime:
    """Holds the long-lived objects and does the actual work of a job."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.handler = platform_named(cfg.platform)
        self.profile = profile.for_platform(cfg.platform)
        self.cache = FileIdCache(cfg.cache_db)
        self.client = httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; justthefile/1.0)"},
        )
        self.tmp_root = Path(cfg.tmp_dir)

    async def aclose(self) -> None:
        await self.client.aclose()
        self.cache.close()

    def cache_key(self, post_id: str) -> str:
        """Namespaced, because post IDs are only unique within a platform."""
        return f"{self.cfg.platform}:{post_id}"

    async def handle_job(self, job: Job, limits: Limits) -> None:
        bot = job.payload["bot"]
        status = Status(job.payload.get("status"))
        workdir = Path(tempfile.mkdtemp(prefix=f"{job.post_id}-", dir=self._tmp_base()))

        try:
            await status.set("Fetching post…", force=True)
            try:
                post = await self.handler.fetch(job.post_id, self.client)
            except RelayMisconfigured as exc:
                # Ours, not the site's, and the only wall here with a fix.
                log.error("%s", exc)
                await status.done()
                await bot.send_message(job.chat_id, self.profile.relay_misconfigured)
                return
            except UpstreamRefused as exc:
                # Every source turned us away. This used to fall through to the
                # empty-post reply below, which blamed the post for a wall of
                # ours and sent people looking for a deletion that never was.
                log.warning("refused %s (%d)", exc.post_id, exc.status)
                await status.done()
                await bot.send_message(job.chat_id, self.profile.upstream_blocked)
                return

            if not post.items:
                await status.done()
                await bot.send_message(
                    job.chat_id,
                    "I couldn't find any video or images in that post. It may have "
                    "been deleted, be from a private account, or be age-restricted.",
                )
                return

            # The handle that posted it, shown as a caption under the media. One
            # caption for the whole post, so a multi-video post credits the same
            # account on each file rather than repeating the tweet's own text.
            caption = _caption(post.author)

            # Keep the original index: it's part of the cache key.
            playable = [
                (i, item) for i, item in enumerate(post.items) if item.kind != PHOTO
            ]
            delivered = True  # every piece landed; one failure turns this off
            for position, (index, item) in enumerate(playable, start=1):
                ok = await self._deliver_video(
                    bot, job, item, index, status, limits, workdir,
                    total=len(playable), position=position, caption=caption,
                )
                delivered = delivered and ok

            photos = [item for item in post.items if item.kind == PHOTO]
            if photos:
                delivered = (
                    await self._deliver_photos(
                        bot, job, photos, status, workdir, caption=caption
                    )
                    and delivered
                )

            # Only now, once the media is actually out, clear the link that
            # asked for it: in a group the bot is silent and a bare link gives
            # way to the file. Deleting earlier, before delivery, meant a mid-job
            # failure such as a dead CDN URL left the sender with neither their
            # link nor the video. On any failure the link stays, with the error
            # beside it, as decided. Needs the bot to be a group admin with
            # delete rights; a refusal there is named in the logs, not surfaced.
            source = job.payload.get("delete")
            if source is not None and delivered:
                try:
                    await source.delete()
                except TelegramError as exc:
                    if "not found" in str(exc).lower():
                        log.info("source message already gone: %s", exc)
                    else:
                        log.warning(
                            "couldn't delete the link message in chat %s (%s). "
                            'Make the bot a group admin with the "Delete '
                            'messages" right.',
                            job.chat_id, exc,
                        )

            # A heads-up that isn't the post's own text, such as an album we
            # could only fetch part of. Sent last so it trails the media.
            if post.notice:
                await bot.send_message(job.chat_id, post.notice)

            await status.done()

        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _tmp_base(self) -> Path:
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        return self.tmp_root

    async def _deliver_video(
        self,
        bot,
        job: Job,
        item: MediaItem,
        index: int,
        status: Status,
        limits: Limits,
        workdir: Path,
        *,
        total: int,
        position: int,
        caption: Optional[str] = None,
    ) -> bool:
        """Deliver one video. Returns True when the file reached the chat, False
        when a handled failure sent an explanation instead. Never raises for an
        expected miss such as a dead CDN URL: the caller reads the bool to decide
        whether the source link may be cleared, so a swallowed exception here
        would wrongly count as success."""
        label = f" ({position}/{total})" if total > 1 else ""
        cap = self.cfg.max_upload_bytes
        key = self.cache_key(job.post_id)

        # When audio arrives as its own file, the video has to leave room for it.
        budget = int(cap * (1 - MUX_AUDIO_ALLOWANCE)) if item.needs_mux else cap

        selection = await select.pick_variant(item, self.client, budget)
        if selection is None:
            await bot.send_message(
                job.chat_id, f"No downloadable mp4 in that post{label}."
            )
            return False

        # A cache hit skips download, mux, transcode and upload entirely.
        cached = await self.cache.get(key, index, selection.variant.url)
        if cached:
            await status.set("Sending from cache…", force=True)
            try:
                await self._send_by_file_id(bot, job, item, cached, caption=caption)
                return True
            except BadRequest as exc:
                log.info("stale file_id for %s: %s", key, exc)
                await self.cache.forget(key)

        await status.set(
            f"Downloading{label}… {_human(selection.size_bytes)}", force=True
        )
        await bot.send_chat_action(job.chat_id, ChatAction.UPLOAD_VIDEO)

        src = workdir / f"{index}-source.mp4"
        try:
            async with limits.downloads:
                async def progress(done: int, total_bytes: Optional[int]) -> None:
                    if total_bytes:
                        pct = done * 100 // total_bytes
                        await status.set(f"Downloading{label}… {pct}%")

                # Allow a generous margin over the cap so a transcodable file still lands.
                ceiling = budget if not selection.needs_transcode else cap * 20
                await select.download(
                    selection.variant.url, src, self.client,
                    max_bytes=ceiling, progress=progress,
                )
        except (httpx.HTTPError, select.DownloadTooLarge) as exc:
            # The CDN URL X handed us didn't answer (404s on removed or
            # region-gated clips are common) or the file overran the ceiling.
            # Report it and move on rather than take the worker down: offering
            # the same dead URL back would be no help, so this points at the
            # site, which resolves the post fresh.
            log.warning("download failed for %s: %s", key, exc)
            await status.done()
            await bot.send_message(
                job.chat_id,
                f"I couldn't fetch that video{label} just now. It may have been "
                f"removed, or be one X won't serve my way. Try the site:\n"
                f"{self._site_link(job)}",
                disable_web_page_preview=True,
            )
            return False

        if item.needs_mux:
            src = await self._attach_audio(
                bot, job, item, src, index, status, limits, workdir, label=label
            )

        meta = await transcode.probe(src)
        upload = src

        if src.stat().st_size > cap or selection.needs_transcode:
            await status.set(f"Compressing{label}…", force=True)
            out = workdir / f"{index}-fit.mp4"
            try:
                async with limits.transcodes:
                    meta = await transcode.fit_to_size(
                        src, out, int(cap * 0.98), meta=meta
                    )
                upload = out
            except transcode.TranscodeNotWorthIt:
                await status.done()
                await bot.send_message(
                    job.chat_id,
                    f"That video is too long to compress under Telegram's 50 MB "
                    f"limit without ruining it.\n\n{self._elsewhere(job, item, selection)}",
                    disable_web_page_preview=True,
                )
                return False
            except transcode.TranscodeError as exc:
                log.warning("transcode failed for %s: %s", key, exc)
                await bot.send_message(
                    job.chat_id,
                    f"I couldn't compress that one.\n\n"
                    f"{self._elsewhere(job, item, selection)}",
                    disable_web_page_preview=True,
                )
                return False

        await status.set(f"Uploading{label}…", force=True)
        sent = await self._send_file(bot, job, item, upload, meta, caption=caption)

        file_id = self._file_id_of(sent)
        if file_id:
            await self.cache.put(
                key, index, selection.variant.url, item.kind, file_id,
                width=meta.width, height=meta.height, duration=meta.duration_s,
            )
        return True

    @staticmethod
    def _elsewhere(job: Job, item: MediaItem, selection) -> str:
        """Where to send someone the 50 MB cap has just turned away.

        A muxed platform's variant URL points at the *silent* video, so offering
        it as a "direct download" would hand over a worse file than the one that
        just failed. Those go to the site, which joins the audio on and has no
        upload cap to work around.
        """
        if not item.needs_mux:
            return f"Direct download:\n{selection.variant.url}"

        return f"Get it with sound here:\n{Runtime._site_link(job)}"

    @staticmethod
    def _site_link(job: Job) -> str:
        """The post on the site, for when the bot itself couldn't deliver. The
        site resolves the post fresh, so it gets past a stale CDN URL the bot
        choked on. Falls back to the bare site when the link wasn't kept."""
        url = job.payload.get("url")
        return f"{SITE}/?url={quote(url, safe='')}" if url else SITE

    async def _attach_audio(
        self,
        bot,
        job: Job,
        item: MediaItem,
        video: Path,
        index: int,
        status: Status,
        limits: Limits,
        workdir: Path,
        *,
        label: str,
    ) -> Path:
        """Join the separately-served audio track on. Returns the file to send.

        Failure falls back to the silent video and says so. A mute clip is a
        poor answer but a better one than an error, and nobody should have to
        discover the difference on playback.
        """
        await status.set(f"Adding audio{label}…", force=True)

        audio = workdir / f"{index}-audio.mp4"
        out = workdir / f"{index}-muxed.mp4"

        try:
            async with limits.downloads:
                await select.download(
                    item.audio_url, audio, self.client,
                    max_bytes=self.cfg.max_upload_bytes,
                )
            # Stream copy, so this is I/O rather than CPU, but it still takes
            # the ffmpeg gate: two of these at once on one vCPU help nobody.
            async with limits.transcodes:
                await mux.mux(video, audio, out)
        except (httpx.HTTPError, select.DownloadTooLarge, mux.MuxError, OSError) as exc:
            log.warning("could not attach audio for %s: %s", job.post_id, exc)
            await bot.send_message(
                job.chat_id,
                f"I couldn't join the audio onto that video{label}, so it comes "
                f"back silent. The site can still give you the full file:\n{SITE}",
                disable_web_page_preview=True,
            )
            return video

        return out

    async def _send_file(
        self, bot, job: Job, item: MediaItem, path: Path, meta,
        *, caption: Optional[str] = None,
    ):
        with path.open("rb") as fh:
            if item.kind == GIF:
                return await bot.send_animation(
                    job.chat_id, animation=fh, caption=caption,
                    width=meta.width, height=meta.height, duration=_int(meta.duration_s),
                )
            return await bot.send_video(
                job.chat_id, video=fh, caption=caption,
                width=meta.width, height=meta.height, duration=_int(meta.duration_s),
                supports_streaming=True,
            )

    async def _send_by_file_id(
        self, bot, job: Job, item: MediaItem, cached: dict,
        *, caption: Optional[str] = None,
    ):
        if item.kind == GIF:
            return await bot.send_animation(
                job.chat_id, animation=cached["file_id"], caption=caption
            )
        return await bot.send_video(
            job.chat_id, video=cached["file_id"], caption=caption,
            supports_streaming=True,
        )

    async def _deliver_photos(
        self, bot, job: Job, photos, status: Status, workdir: Path,
        *, caption: Optional[str] = None,
    ) -> bool:
        """Deliver a post's images. Returns True when at least one was sent."""
        await status.set(f"Sending {len(photos)} image(s)…", force=True)
        links = [p.variants[0].url for p in photos if p.variants]
        if not links:
            return False

        try:
            return await self._send_photos(bot, job, links, caption=caption)
        except BadRequest as exc:
            # Handing Telegram a URL makes *its* servers fetch the file, which
            # costs us nothing and is why it's the first thing tried. Its
            # fetcher is stricter than a browser though, and i.redd.it turns it
            # away often enough to be worth paying for the bytes ourselves.
            log.info("photo by URL rejected (%s); uploading the bytes instead", exc)
            return await self._send_photos(
                bot, job, await self._localise(links, workdir), caption=caption
            )

    async def _send_photos(
        self, bot, job: Job, photos: list, *, caption: Optional[str] = None
    ) -> bool:
        """Send URLs or open files. Albums cap at 10 items, hence the batching.
        Returns True if anything was sent."""
        if not photos:
            await bot.send_message(
                job.chat_id, "I couldn't fetch the images from that post."
            )
            return False

        for start in range(0, len(photos), 10):
            batch = photos[start : start + 10]
            handles = [p.open("rb") if isinstance(p, Path) else p for p in batch]
            # The handle that posted it rides on the first item of the first
            # batch only: Telegram shows one caption per album, and repeating it
            # would stack duplicates across batches.
            first = start == 0
            try:
                if len(handles) == 1:
                    await bot.send_photo(
                        job.chat_id, photo=handles[0],
                        caption=caption if first else None,
                    )
                else:
                    media = [
                        InputMediaPhoto(h, caption=caption if first and i == 0 else None)
                        for i, h in enumerate(handles)
                    ]
                    await bot.send_media_group(job.chat_id, media=media)
            finally:
                for handle in handles:
                    if not isinstance(handle, str):
                        handle.close()
        return True

    async def _localise(self, links: list[str], workdir: Path) -> list[Path]:
        """Pull images onto disk. Skips the ones that won't come, rather than
        failing the whole album for one dead link."""
        paths: list[Path] = []
        for n, url in enumerate(links):
            dest = workdir / f"photo-{n}"
            try:
                await select.download(
                    url, dest, self.client, max_bytes=self.cfg.max_upload_bytes
                )
            except (httpx.HTTPError, select.DownloadTooLarge, OSError) as exc:
                log.warning("could not fetch photo %s: %s", url, exc)
                continue
            paths.append(dest)
        return paths

    @staticmethod
    def _file_id_of(message) -> Optional[str]:
        if message is None:
            return None
        if getattr(message, "video", None):
            return message.video.file_id
        if getattr(message, "animation", None):
            return message.animation.file_id
        return None


def _int(value: Optional[float]) -> Optional[int]:
    return int(value) if value else None


_URL_RE = re.compile(r"https?://\S+")


def _first_url(text: str) -> Optional[str]:
    """The link out of a message that may be a link plus a sentence.

    The platform parsers tolerate the surrounding words, but this link gets
    handed back to the user in a message, so it should be just the link.
    """
    match = _URL_RE.search(text or "")
    return match.group(0) if match else None


def _is_pure_link(text: str, url: Optional[str]) -> bool:
    """True when the message is the link and nothing else.

    Only these are cleared in a group. A message that is link-plus-commentary
    belongs to its sender: deleting it to post the media would eat their words.
    Whitespace around the link doesn't count as commentary.
    """
    return bool(url) and (text or "").strip() == url


def _caption(author: Optional[str]) -> Optional[str]:
    """The line shown under delivered media: the @handle that posted it, or None
    when the source didn't name an author. The handle comes from the platform
    already stripped of its @, so it's added back here."""
    author = (author or "").strip().lstrip("@")
    return f"@{author}" if author else None


def _is_group_chat(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def _in_permitted_group(cfg: Config, update: Update) -> bool:
    """Whether this update is from a group chat the bot is allowed to serve.

    Group access is by chat, not by member: an allowlisted group is open to
    everyone in it. A private chat is never a "group" here and takes the
    per-user path instead.
    """
    return _is_group_chat(update) and chat_permitted(cfg, update.effective_chat.id)


def _permitted(cfg: Config, update: Update) -> bool:
    """The single access gate cmd_start and on_message share, so the DM rule and
    the group rule can never drift apart.

    The two gates are separate, not combined: a group is judged by its chat ID
    alone, a DM by its user ID alone. An allowlisted user does NOT get the bot in
    an arbitrary group they happen to share with it: that path used to let an
    owner's own test message through in a non-allowlisted group, which both
    leaked the bot into groups it wasn't meant for and hid the misconfiguration,
    since the "ignoring …" log that carries the chat ID never fired. A blocked
    user is denied in either.
    """
    user = update.effective_user
    uid = user.id if user else None
    if uid is not None and uid in cfg.blocked_user_ids:
        return False
    if _is_group_chat(update):
        return chat_permitted(cfg, update.effective_chat.id)
    return is_allowed(cfg, uid)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

# Ties the info button to its handler. The value is opaque to Telegram; it only
# has to match on the way back.
NOTE_CALLBACK = "note"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    cfg: Config = app.bot_data["cfg"]
    if not _permitted(cfg, update):
        return
    profile = app.bot_data["profile"]
    # A platform with a standing caveat gets an info button under /start; the
    # note itself lands in a popup so it doesn't crowd the help text every time.
    markup = (
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("ℹ️ Does it always work?", callback_data=NOTE_CALLBACK)]]
        )
        if profile.note
        else None
    )
    await update.message.reply_text(profile.help, reply_markup=markup)


async def on_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Answer the info button with the platform note as a popup alert."""
    query = update.callback_query
    if query is None:
        return
    note = context.application.bot_data["profile"].note
    # show_alert makes it a dismissable dialog rather than a toast that fades
    # before it's read. answer() with no text still clears the button's spinner.
    await query.answer(text=note or "", show_alert=bool(note))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    cfg: Config = app.bot_data["cfg"]
    runtime: Runtime = app.bot_data["runtime"]
    queue: JobQueue = app.bot_data["queue"]

    user = update.effective_user
    if not _permitted(cfg, update):
        # The chat id is here on purpose: it's the value that goes in
        # ALLOWED_CHAT_IDS, and this line is the easiest way to read it off a
        # group the bot has been added to but not yet allowlisted.
        chat = update.effective_chat
        log.info(
            "ignoring message from %s in chat %s",
            user.id if user else "unknown",
            chat.id if chat else "unknown",
        )
        return

    # An allowlisted group runs the bot silently, DMs keep the running status
    # they've always had. group_ok gates both the quiet mode and the link
    # deletion below.
    group_ok = _in_permitted_group(cfg, update)

    message = update.message
    if message is None or not message.text:
        return

    try:
        post_id = await runtime.handler.identify(message.text, runtime.client)
    except RelayMisconfigured as exc:
        log.error("%s", exc)
        await message.reply_text(runtime.profile.relay_misconfigured)
        return
    except LinkUnresolved as exc:
        # The link is one we handle; the site it hides behind wouldn't answer.
        # Telling someone their link is unrecognisable here is a wrong answer
        # that costs them a round of pointless link-fixing. Being refused and
        # being ignored are told apart, because only one of them is worth
        # retrying and the wrong advice wastes the sender's time twice.
        log.warning("could not resolve %s (%s)", exc.url, exc.reason)
        await message.reply_text(
            runtime.profile.upstream_blocked
            if exc.refused
            else runtime.profile.link_unresolved
        )
        return

    if not post_id:
        await message.reply_text(runtime.profile.unknown_link)
        return

    # No "Queued…" line and no progress edits in a group: the result there is
    # just the link giving way to the media, nothing else.
    status = None if group_ok else await message.reply_text("Queued…")

    url = _first_url(message.text)
    # Clear the request only in a group, and only when it's a bare link with no
    # words of the sender's own to lose. The deletion itself happens in the
    # worker, once the post is known to be real, so an unfetchable link is left
    # in place with an error beside it.
    delete_source = message if (group_ok and _is_pure_link(message.text, url)) else None

    job = Job(
        user_id=user.id if user else message.chat_id,
        chat_id=message.chat_id,
        post_id=post_id,
        # url: kept for the one case that needs it, pointing someone at the same
        # post on the site when the upload cap defeats us. delete: the message to
        # remove on success, or None to leave it.
        payload={
            "bot": app.bot,
            "status": status,
            "url": url,
            "delete": delete_source,
        },
    )

    try:
        await queue.submit(job)
    except QueueFull as exc:
        if status is not None:
            await status.edit_text(str(exc))
        else:
            await message.reply_text(str(exc))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("unhandled error", exc_info=context.error)


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #

def build_application(cfg: Config) -> Application:
    runtime = Runtime(cfg)
    runtime.cache.connect()

    limits = Limits.create(cfg.max_concurrent_downloads, cfg.max_concurrent_transcodes)
    queue = JobQueue(
        runtime.handle_job,
        limits,
        workers=max(cfg.max_concurrent_downloads, 1),
        max_per_user=cfg.max_queue_per_user,
    )

    async def post_init(app: Application) -> None:
        queue.start()
        await profile.apply(app.bot, runtime.profile)
        me = await app.bot.get_me()
        log.info(
            "running as @%s for %s in %s mode",
            me.username, cfg.platform, cfg.access_mode,
        )

    async def post_shutdown(app: Application) -> None:
        await queue.stop()
        await runtime.aclose()

    app = (
        Application.builder()
        .token(cfg.bot_token)
        .rate_limiter(AIORateLimiter())
        # Uploads near 50 MB need far more than the default write timeout.
        .write_timeout(300)
        .read_timeout(120)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.bot_data.update(
        {"cfg": cfg, "runtime": runtime, "queue": queue, "profile": runtime.profile}
    )
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CallbackQueryHandler(on_note, pattern=f"^{NOTE_CALLBACK}$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(on_error)
    return app


def check_scratch(cfg: Config) -> None:
    """Refuse to start if downloads have nowhere to land.

    Every job begins by making a directory under TMP_DIR, so an unwritable one
    means no job can ever succeed. Without this the bot starts happily, accepts
    work, says "Queued…" and only then fails, once per job, for as long as it
    stays up. A mounted volume that the image never created is owned by root
    while this process is not, which is the way this actually happens.
    """
    path = Path(cfg.tmp_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = tempfile.mkdtemp(prefix="startup-", dir=path)
    except OSError as exc:
        raise SystemExit(
            f"Scratch directory {cfg.tmp_dir} is not usable: {exc}\n"
            f"Every download lands there first, so nothing can run without it. "
            f"Running as uid {os.getuid()}. If it's a Docker volume, it is "
            f"owned by root: `docker compose down`, `docker volume rm` the "
            f"scratch volume, then rebuild so the image creates the path."
        ) from exc
    os.rmdir(probe)


def main() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    check_scratch(cfg)

    if not transcode.available():
        log.warning("ffmpeg/ffprobe not found: oversized videos cannot be compressed")
    elif not mux.available():
        log.warning("ffmpeg not found: video with separate audio will arrive silent")

    build_application(cfg).run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
