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
from telegram import InputMediaPhoto, Message, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
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
from .access import is_allowed
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

            # Keep the original index: it's part of the cache key.
            playable = [
                (i, item) for i, item in enumerate(post.items) if item.kind != PHOTO
            ]
            for position, (index, item) in enumerate(playable, start=1):
                await self._deliver_video(
                    bot, job, item, index, status, limits, workdir,
                    total=len(playable), position=position,
                )

            photos = [item for item in post.items if item.kind == PHOTO]
            if photos:
                await self._deliver_photos(bot, job, photos, status, workdir)

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
    ) -> None:
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
            return

        # A cache hit skips download, mux, transcode and upload entirely.
        cached = await self.cache.get(key, index, selection.variant.url)
        if cached:
            await status.set("Sending from cache…", force=True)
            try:
                await self._send_by_file_id(bot, job, item, cached)
                return
            except BadRequest as exc:
                log.info("stale file_id for %s: %s", key, exc)
                await self.cache.forget(key)

        await status.set(
            f"Downloading{label}… {_human(selection.size_bytes)}", force=True
        )
        await bot.send_chat_action(job.chat_id, ChatAction.UPLOAD_VIDEO)

        src = workdir / f"{index}-source.mp4"
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
                return
            except transcode.TranscodeError as exc:
                log.warning("transcode failed for %s: %s", key, exc)
                await bot.send_message(
                    job.chat_id,
                    f"I couldn't compress that one.\n\n"
                    f"{self._elsewhere(job, item, selection)}",
                    disable_web_page_preview=True,
                )
                return

        await status.set(f"Uploading{label}…", force=True)
        sent = await self._send_file(bot, job, item, upload, meta)

        file_id = self._file_id_of(sent)
        if file_id:
            await self.cache.put(
                key, index, selection.variant.url, item.kind, file_id,
                width=meta.width, height=meta.height, duration=meta.duration_s,
            )

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

        url = job.payload.get("url")
        target = f"{SITE}/?url={quote(url, safe='')}" if url else SITE
        return f"Get it with sound here:\n{target}"

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

    async def _send_file(self, bot, job: Job, item: MediaItem, path: Path, meta):
        with path.open("rb") as fh:
            if item.kind == GIF:
                return await bot.send_animation(
                    job.chat_id, animation=fh,
                    width=meta.width, height=meta.height, duration=_int(meta.duration_s),
                )
            return await bot.send_video(
                job.chat_id, video=fh,
                width=meta.width, height=meta.height, duration=_int(meta.duration_s),
                supports_streaming=True,
            )

    async def _send_by_file_id(self, bot, job: Job, item: MediaItem, cached: dict):
        if item.kind == GIF:
            return await bot.send_animation(job.chat_id, animation=cached["file_id"])
        return await bot.send_video(
            job.chat_id, video=cached["file_id"], supports_streaming=True
        )

    async def _deliver_photos(
        self, bot, job: Job, photos, status: Status, workdir: Path
    ) -> None:
        await status.set(f"Sending {len(photos)} image(s)…", force=True)
        links = [p.variants[0].url for p in photos if p.variants]
        if not links:
            return

        try:
            await self._send_photos(bot, job, links)
        except BadRequest as exc:
            # Handing Telegram a URL makes *its* servers fetch the file, which
            # costs us nothing and is why it's the first thing tried. Its
            # fetcher is stricter than a browser though, and i.redd.it turns it
            # away often enough to be worth paying for the bytes ourselves.
            log.info("photo by URL rejected (%s); uploading the bytes instead", exc)
            await self._send_photos(bot, job, await self._localise(links, workdir))

    async def _send_photos(self, bot, job: Job, photos: list) -> None:
        """Send URLs or open files. Albums cap at 10 items, hence the batching."""
        if not photos:
            await bot.send_message(
                job.chat_id, "I couldn't fetch the images from that post."
            )
            return

        for start in range(0, len(photos), 10):
            batch = photos[start : start + 10]
            handles = [p.open("rb") if isinstance(p, Path) else p for p in batch]
            try:
                if len(handles) == 1:
                    await bot.send_photo(job.chat_id, photo=handles[0])
                else:
                    await bot.send_media_group(
                        job.chat_id, media=[InputMediaPhoto(h) for h in handles]
                    )
            finally:
                for handle in handles:
                    if not isinstance(handle, str):
                        handle.close()

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


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    cfg: Config = app.bot_data["cfg"]
    if not is_allowed(cfg, update.effective_user.id if update.effective_user else None):
        return
    await update.message.reply_text(app.bot_data["profile"].help)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    cfg: Config = app.bot_data["cfg"]
    runtime: Runtime = app.bot_data["runtime"]
    queue: JobQueue = app.bot_data["queue"]

    user = update.effective_user
    if not is_allowed(cfg, user.id if user else None):
        log.info("ignoring message from %s", user.id if user else "unknown")
        return

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

    status = await message.reply_text("Queued…")
    job = Job(
        user_id=user.id,
        chat_id=message.chat_id,
        post_id=post_id,
        # The original link is kept for the one case that needs it: pointing
        # someone at the same post on the site when the upload cap defeats us.
        payload={"bot": app.bot, "status": status, "url": _first_url(message.text)},
    )

    try:
        await queue.submit(job)
    except QueueFull as exc:
        await status.edit_text(str(exc))


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
