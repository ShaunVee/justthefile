"""JSON API + static front end.

One endpoint does the work: paste a link, get back every downloadable rendition
with a direct CDN URL. The browser fetches the bytes itself, so this process
never touches media, which is what keeps it cheap enough to run beside the bot
on the same small VPS.

Nothing here names a platform. Which links are accepted, and how they resolve,
is entirely `core.platforms`.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core import platforms

from . import download as download_mod
from .cache import TTLCache
from .config import WebConfig
from .ratelimit import RateLimiter
from .serialize import describe

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# What a visitor is told when the site turned this server away rather than
# failing to answer. One string for both places it can happen, because from
# where the visitor sits they are the same event: a wall, ours, not theirs.
# It offers no workaround on purpose. Every workaround worth suggesting goes
# to the same blocked host, and advice that cannot work is worse than none.
_BLOCKED = (
    "That site is refusing requests from this server at the moment. Your link "
    "is fine, and no other link will get through either. This one's on us."
)

# Said instead of _BLOCKED when it was our own relay that turned us away, not
# the site. A visitor can do nothing about either, so the copy differs only in
# what it does not claim: the operator reading this over someone's shoulder is
# the one person who can fix it, and _BLOCKED would send them auditing the
# server's address when the answer is a mismatched key. The log line beside
# this names the variable.
_MISCONFIGURED = (
    "Something's misconfigured on this server, so lookups can't go out at all. "
    "Nothing to do with your link. It's been logged."
)


def _client_ip(request: Request, trust_proxy: bool) -> str:
    """Caller identity for rate limiting.

    X-Forwarded-For is trusted only when configured, because anyone can send the
    header: honouring it unconditionally would let a single client mint a fresh
    bucket per request and walk straight through the limiter.
    """
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(cfg: WebConfig | None = None) -> FastAPI:
    cfg = cfg or WebConfig.from_env()
    limiter = RateLimiter(cfg.rate_burst, cfg.rate_per_minute)
    # Separate bucket for downloads: "download all" on a gallery is one resolve
    # and one request per file, and charging those to the resolve bucket would
    # 429 the back half of the run.
    download_limiter = RateLimiter(cfg.download_burst, cfg.download_per_minute)
    cache = TTLCache(cfg.resolve_ttl_s, cfg.resolve_cache_size)
    # Shared across requests so concurrent muxes queue rather than
    # all landing on the one vCPU at once.
    mux_gate = asyncio.Semaphore(max(1, cfg.max_concurrent_muxes))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = httpx.AsyncClient(
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; justthefile/1.0)"},
        )
        log.info("web front end ready on %s:%d", cfg.host, cfg.port)
        yield
        await app.state.client.aclose()

    app = FastAPI(
        title="justthefile",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/resolve")
    async def resolve(request: Request, url: str = Query(..., max_length=2048)):
        ip = _client_ip(request, cfg.trust_proxy)
        if not limiter.allow(ip):
            return JSONResponse(
                {"error": "Too many requests, give it a moment."},
                status_code=429,
                headers={"Retry-After": str(limiter.retry_after(ip))},
            )

        client: httpx.AsyncClient = request.app.state.client

        try:
            ref = await platforms.identify(url, client)
        except platforms.RelayMisconfigured as exc:
            log.error("%s", exc)
            return JSONResponse({"error": _MISCONFIGURED}, status_code=502)
        except platforms.LinkUnresolved as exc:
            # A share link we recognised and couldn't follow. 502, not 400:
            # nothing about the link needs changing.
            log.warning("could not resolve %s (%s)", exc.url, exc.reason)
            if exc.refused:
                # Offering the full link here would be advice that cannot work:
                # it resolves against the same host that just refused us.
                return JSONResponse({"error": _BLOCKED}, status_code=502)
            return JSONResponse(
                {
                    "error": "That share link wouldn't resolve just now. Try "
                    "again in a moment, or paste the post's full link."
                },
                status_code=502,
            )

        if ref is None:
            return JSONResponse(
                {
                    "error": f"That link isn't from a site we handle yet. "
                    f"Right now: {platforms.labels()}."
                },
                status_code=400,
            )

        cached = cache.get(ref.cache_key)
        if cached is not None:
            return JSONResponse(cached, headers={"X-Cache": "hit"})

        try:
            resolution = await ref.fetch(client)
        except platforms.RelayMisconfigured as exc:
            log.error("%s", exc)
            return JSONResponse({"error": _MISCONFIGURED}, status_code=502)
        except platforms.UpstreamRefused as exc:
            # Not "try again in a moment", and emphatically not the empty-post
            # answer below: the site refused us, and it will refuse the next
            # attempt and every other link with it.
            log.warning("refused %s (%d)", exc.post_id, exc.status)
            return JSONResponse({"error": _BLOCKED}, status_code=502)
        except Exception:
            log.exception("resolve failed for %s", ref.cache_key)
            return JSONResponse(
                {"error": "Couldn't reach that site just now. Try again in a moment."},
                status_code=502,
            )

        if not resolution.items:
            return JSONResponse(
                {
                    "error": "No video or images in that post. It may be deleted, "
                    "private, or age-restricted."
                },
                status_code=404,
            )

        payload = await describe(
            resolution, client,
            max_heads=cfg.max_head_requests, delivery=ref.delivery,
            item_delivery=ref.item_delivery,
        )
        if not payload["media"]:
            return JSONResponse(
                {"error": "Nothing downloadable in that post."}, status_code=404
            )

        cache.put(ref.cache_key, payload)
        return JSONResponse(payload, headers={"X-Cache": "miss"})

    @app.get("/api/download")
    async def download(
        request: Request,
        platform: str = Query(..., max_length=32),
        post_id: str = Query(..., max_length=64),
        item: int = Query(0, ge=0, le=64),
        variant: int = Query(0, ge=0, le=32),
    ):
        """Server-side delivery for media the browser cannot fetch itself.

        Takes indices rather than URLs on purpose: the URLs are re-derived from
        our own resolution of the post, so this cannot be pointed at an
        arbitrary host. See `web.download`.
        """
        ip = _client_ip(request, cfg.trust_proxy)
        if not download_limiter.allow(ip):
            return JSONResponse(
                {"error": "Too many requests, give it a moment."},
                status_code=429,
                headers={"Retry-After": str(download_limiter.retry_after(ip))},
            )

        handler = download_mod.platform_named(platform)
        if handler is None:
            return JSONResponse({"error": "Unknown platform."}, status_code=400)

        client: httpx.AsyncClient = request.app.state.client
        key = f"{platform}:{post_id}"

        # The resolve call almost always ran moments ago, so this is a cache hit
        # and the download costs no extra upstream request.
        payload = cache.get(key)
        if payload is None:
            ref = platforms.PostRef(platform=platform, post_id=post_id, handler=handler)
            try:
                resolution = await ref.fetch(client)
            except Exception:
                log.exception("download resolve failed for %s", key)
                return JSONResponse(
                    {"error": "Couldn't reach that site just now."}, status_code=502
                )
            if not resolution.items:
                return JSONResponse({"error": "Nothing to download."}, status_code=404)

            payload = await describe(
                resolution, client,
                max_heads=cfg.max_head_requests, delivery=ref.delivery,
                item_delivery=ref.item_delivery,
            )
            cache.put(key, payload)

        return await download_mod.deliver(
            payload, handler, item, variant, client,
            cap=cfg.max_proxy_mb * download_mod.MB,
            tmp_root=Path(cfg.tmp_dir),
            gate=mux_gate,
        )

    @app.get("/api/platforms")
    async def supported() -> dict[str, object]:
        """Drives the "works with" line, so the page updates itself when a
        platform is added to the registry."""
        return {"platforms": platforms.supported()}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def main() -> None:
    import uvicorn

    cfg = WebConfig.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, access_log=False)


if __name__ == "__main__":
    main()
