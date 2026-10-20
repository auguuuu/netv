#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn[standard]", "jinja2", "python-multipart", "cryptography", "defusedxml"]
# ///
"""IPTV Web App.

Usage:
    ./main.py [--port PORT] [--https] [--cert FILE --key FILE]

Options:
    --port PORT     Port to listen on (default: 8000)
    --https         Enable HTTPS using Let's Encrypt certs (auto-detect domain)
    --cert FILE     SSL certificate file (overrides --https)
    --key FILE      SSL private key file (overrides --https)

Examples:
    ./main.py                              # HTTP on port 8000
    ./main.py --https                      # HTTPS with auto-detected Let's Encrypt certs
    ./main.py --cert c.pem --key k.pem     # HTTPS with custom certs
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from xml.sax.saxutils import escape as xml_escape

import asyncio
import concurrent.futures
import contextlib
import json
import logging
import os
import pathlib
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import StreamingResponse

from auth import create_token, verify_password, verify_token
from cache import (
    AVAILABLE_ENCODERS,
    CACHE_DIR,
    LOGO_BROWSER_TTL,
    LOGO_MAX_SIZE,
    Source,
    clear_all_caches,
    clear_all_file_caches,
    get_cache,
    get_cache_lock,
    get_cached_info,
    get_cached_logo,
    get_sources,
    get_watch_position,
    load_file_cache,
    load_server_settings,
    load_user_settings,
    refresh_encoders,
    save_file_cache,
    save_logo,
    save_server_settings,
    save_user_settings,
    save_watch_position,
    update_source_epg_url,
)
from epg import fetch_epg
from fast_start import dated_playlist
from m3u import (
    fetch_m3u,
    fetch_source_live_data,
    fetch_source_vod_data,
    get_first_xtream_client,
    get_refresh_in_progress,
    get_xtream_client_by_source,
    load_all_live_data,
    load_series_data,
    load_vod_data,
    parse_epg_urls,
)
from playback_policy import PlaybackHealth
from xtream import XtreamClient

import auth
import catalog_lang
import downloads
import epg
import ffmpeg_command
import ffmpeg_session
import line_status
import local_media
import original_lang
import partial_play


log = logging.getLogger()

# SSE subscribers for EPG ready notifications (limit to prevent DoS)
_epg_subscribers: set[asyncio.Queue[str]] = set()
_shutdown_event: asyncio.Event | None = None  # Set during shutdown to close SSE
_MAX_SSE_SUBSCRIBERS = 100

# Login rate limiting: track failed attempts per IP
_login_attempts: dict[str, list[float]] = {}
_LOGIN_WINDOW = 300  # 5 minutes
_LOGIN_MAX_ATTEMPTS = 10

# Category filter limits
_MAX_FILTER_CATEGORIES = 10000


# =============================================================================
# App Setup
# =============================================================================

APP_DIR = pathlib.Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=APP_DIR / "templates")
TEMPLATES.env.auto_reload = True

# Super-resolution engine directory (TensorRT engines for different resolutions)
SR_ENGINE_DIR = pathlib.Path(
    os.environ.get("SR_ENGINE_DIR", pathlib.Path.home() / "ffmpeg_build/models")
)


def get_sr_models() -> list[str]:
    """Get available AI Upscale models (unique model names from engine files)."""
    if not SR_ENGINE_DIR.exists():
        return []
    # Engine files are named: {model}_{height}p_fp16.engine
    # e.g., 4x-compact_720p_fp16.engine, 2x-nomosuni-compact_1080p_fp16.engine
    models = set()
    for engine in SR_ENGINE_DIR.glob("*_*p_fp16.engine"):
        # Extract model name by removing _{height}p_fp16.engine suffix
        name = engine.stem  # e.g., "2x-nomosuni-compact_1080p_fp16"
        # Remove _fp16 and _{height}p
        parts = name.rsplit("_", 2)  # ["2x-nomosuni-compact", "1080p", "fp16"]
        if len(parts) >= 3:
            models.add(parts[0])

    # Put the recommended 1080p and 720p models first.
    def sort_key(m: str) -> tuple[int, str]:
        if m == "2x-nomosuni-compact":
            return (0, m)
        if m == "4x-compact":
            return (1, m)
        return (2, m)

    return sorted(models, key=sort_key)


def is_sr_available() -> bool:
    """Check if AI Upscale is available (at least one TensorRT engine exists)."""
    return len(get_sr_models()) > 0


def _logo_url_filter(url: str) -> str:
    """Wrap external logo URLs through /api/logo proxy."""
    if not url or url.startswith("/") or url.startswith("data:"):
        return url  # Already local or data URL
    # Use hostname as source for organization
    parsed = urllib.parse.urlparse(url)
    source = parsed.netloc.split(":")[0] if parsed.netloc else "external"
    return f"/api/logo?source={urllib.parse.quote(source)}&url={urllib.parse.quote(url)}"


TEMPLATES.env.filters["logo_url"] = _logo_url_filter


PAGE_SIZE = 60


def _page_slice(items: list, offset: int, request: Request) -> tuple[list, str]:
    """Return (items[offset:offset+PAGE_SIZE], url of the next fragment or "")."""
    offset = max(0, offset)
    page = items[offset : offset + PAGE_SIZE]
    nxt = offset + PAGE_SIZE
    if nxt >= len(items):
        return page, ""
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "offset"]
    params.append(("offset", str(nxt)))
    return page, f"{request.url.path}?{urllib.parse.urlencode(params)}"


def _safe_float(value: float | str | None, default: float = 0.0) -> float:
    """Safely convert value to float, returning default on failure."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


_epg_fetch_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Clean up orphaned transcodes and preload data on startup."""
    # Initialize EPG database
    epg.init(CACHE_DIR)

    # Prune expired EPG data (keep 24h buffer for "what was just on")
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    pruned = epg.prune_old_programs(cutoff)
    if pruned:
        log.info("Pruned %d expired EPG programs", pruned)

    # Initialize transcoding module with settings callback
    ffmpeg_command.init(
        load_server_settings,
        sr_engine_dir=str(SR_ENGINE_DIR) if is_sr_available() else "",
    )

    # Kill orphaned ffmpeg processes
    try:
        result = subprocess.run(
            ["pgrep", "-f", "ffmpeg.*iptv_transcode"],
            check=False,
            capture_output=True,
            text=True,
        )
        for pid in result.stdout.strip().split("\n"):
            if pid:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    log.info("Killed orphaned ffmpeg pid %s", pid)
                except (ProcessLookupError, ValueError):
                    pass
    except Exception:
        pass
    # Clean up orphaned dirs and recover valid VOD sessions
    ffmpeg_session.cleanup_and_recover_sessions()

    # Offline downloads: load the index (running ones come back paused) and start the queue
    downloads.vo_resolver = lambda e, src: _resolve_original_lang(
        "episode" if e.kind == "episode" else "movie",
        e.item_id,
        e.source_id,
        e.series_id if e.kind == "episode" else None,
        src,
        True,
    )[0]
    partial_play.cleanup_leftovers()
    try:
        downloads.startup()
    except Exception:
        log.exception("Downloads could not start")

    # Preload all data in background threads (parallel)
    def load_live():
        get_refresh_in_progress().add("guide_load")
        try:
            log.info("Preloading live data")
            cats, streams, epg_urls = load_all_live_data()
            with get_cache_lock():
                get_cache()["live_categories"] = cats
                get_cache()["live_streams"] = streams
                get_cache()["epg_urls"] = epg_urls
            log.info("Live data loaded")
        except Exception as e:
            log.warning("Live preload failed (offline?): %s", e)
        finally:
            get_refresh_in_progress().discard("guide_load")

    def load_epg_data():
        try:
            epg_urls = get_cache().get("epg_urls", [])
            if epg_urls:
                load_all_epg(epg_urls)
                log.info("EPG data loaded: %d programs", epg.get_program_count())
                # Notify SSE subscribers
                for q in list(_epg_subscribers):
                    with contextlib.suppress(Exception):
                        q.put_nowait("epg_ready")
        except Exception as e:
            log.error("EPG load error: %s", e)

    def load_vod():
        try:
            vod_cats, vod_streams = load_vod_data()
            with get_cache_lock():
                get_cache()["vod_categories"] = vod_cats
                get_cache()["vod_streams"] = vod_streams
            log.info("VOD data loaded")
        except Exception as e:
            log.warning("VOD preload failed (offline?): %s", e)

    def load_series():
        try:
            series_cats, series_list = load_series_data()
            with get_cache_lock():
                get_cache()["series_categories"] = series_cats
                get_cache()["series"] = series_list
            log.info("Series data loaded")
        except Exception as e:
            log.warning("Series preload failed (offline?): %s", e)

    # Start all preloads in parallel (EPG waits for live data internally)
    def load_all():
        load_live()
        # EPG needs epg_urls from live data, so run after
        load_epg_data()

    threading.Thread(target=load_all, daemon=True).start()
    threading.Thread(target=load_vod, daemon=True).start()
    threading.Thread(target=load_series, daemon=True).start()
    log.info("Preload started: live+EPG, VOD, series loading in parallel")

    # Periodic cleanup of expired sessions (VOD and live)
    cleanup_stop = threading.Event()

    def cleanup_loop():
        while not cleanup_stop.wait(60):  # Check every minute
            ffmpeg_session.cleanup_expired_sessions()

    cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
    cleanup_thread.start()

    # EPG scheduler
    scheduler_stop = threading.Event()
    _last_triggered: dict[str, str] = {}  # source_id -> last triggered time

    def scheduler_loop():
        while not scheduler_stop.wait(30):  # Check every 30 seconds
            now = datetime.now()
            current_time = now.strftime("%H:%M")
            for source in get_sources():
                if current_time in source.epg_schedule:
                    key = f"{source.id}_epg"
                    # Only trigger once per scheduled time
                    if (
                        _last_triggered.get(source.id) != current_time
                        and key not in get_refresh_in_progress()
                    ):
                        log.info("Scheduled EPG refresh for %s at %s", source.name, current_time)
                        _last_triggered[source.id] = current_time
                        get_refresh_in_progress().add(key)

                        def do_refresh(src: Source = source, k: str = key):
                            try:
                                epg_url = None
                                if src.type == "xtream":
                                    client = XtreamClient(src.url, src.username, src.password)
                                    epg_url = client.epg_url
                                elif src.type == "m3u":
                                    _, _, epg_url = fetch_m3u(src.url, src.id)
                                elif src.type == "epg":
                                    epg_url = src.url
                                if epg_url:
                                    _fetch_all_epg([(epg_url, src.epg_timeout, src.id)])
                                    log.info("Scheduled EPG refresh complete for %s", src.name)
                            except Exception as e:
                                log.error("Scheduled EPG refresh failed for %s: %s", src.name, e)
                            finally:
                                get_refresh_in_progress().discard(k)

                        threading.Thread(target=do_refresh, daemon=True).start()

    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()

    yield

    # Shutdown - signal SSE connections to close
    global _shutdown_event
    _shutdown_event = asyncio.Event()
    _shutdown_event.set()
    cleanup_stop.set()
    scheduler_stop.set()
    ffmpeg_session.shutdown()
    partial_play.shutdown()
    downloads.shutdown()


app = FastAPI(title="neTV", lifespan=lifespan)
class RevalidatedStaticFiles(StaticFiles):
    """Static files that the browser must revalidate (ETag) instead of caching heuristically,
    otherwise an updated player.js is not picked up for days."""

    async def get_response(self, path: str, scope: Any) -> Response:
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", RevalidatedStaticFiles(directory=APP_DIR / "static"), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    """Browsers ask for /favicon.ico regardless of the <link rel=icon> tag."""
    return FileResponse(
        APP_DIR / "static" / "favicon.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "max-age=86400"},
    )


class AuthRequired(Exception):
    """Raised when authentication is required."""


@app.exception_handler(AuthRequired)
async def auth_required_handler(request: Request, _exc: AuthRequired):
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Show nice HTML error pages for HTTP errors."""
    # Only handle HTML requests, let API requests get JSON
    accept = request.headers.get("accept", "")
    if "text/html" not in accept:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return TEMPLATES.TemplateResponse(
        request,
        "error.html",
        {"title": f"Erreur {exc.status_code}", "message": exc.detail},
        status_code=exc.status_code,
    )


# NETV_AUTO_LOGIN=<username>: skip the login screen and act as that user
# (only safe when the server is bound to localhost, see NETV_HOST).
AUTO_LOGIN_USER = os.environ.get("NETV_AUTO_LOGIN", "")

# Chromecast: the TV fetches the HLS stream itself, so the server listens on the LAN too.
# Other machines only get the transcode files (unguessable session ids) and only from a
# private network; the rest of the app (no password with NETV_AUTO_LOGIN) stays local.
_CAST_PATH = re.compile(r"^/transcode/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$")


def _is_local_client(host: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "testclient")


def _is_lan_client(host: str) -> bool:
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private and not ip.is_loopback


@app.middleware("http")
async def lan_guard(request: Request, call_next):
    host = request.client.host if request.client else ""
    if _is_local_client(host):
        return await call_next(request)
    if _is_lan_client(host) and request.method in ("GET", "HEAD", "OPTIONS") and _CAST_PATH.match(request.url.path):
        return await call_next(request)
    return Response("Forbidden", status_code=403)


def _lan_ip() -> str:
    import socket

    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.168.0.1", 9))  # no packet is sent: picks the LAN interface
        return s.getsockname()[0]
    return ""


def cast_host_for(request: Request, user_settings: dict) -> str:
    """Address the Chromecast uses to reach this server (Mac's LAN IP unless set by hand)."""
    if user_settings.get("cast_host"):
        return user_settings["cast_host"]
    ip = _lan_ip()
    return f"{ip}:{request.url.port or 80}" if ip else ""


def get_current_user(request: Request) -> dict | None:
    token = request.cookies.get("token")
    payload = verify_token(token) if token else None
    if payload is None and AUTO_LOGIN_USER:
        return {"sub": AUTO_LOGIN_USER}
    return payload


def require_auth(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise AuthRequired
    return user


def require_admin(request: Request) -> dict:
    user = require_auth(request)
    username = user.get("sub", "")
    if not auth.is_admin(username):
        raise HTTPException(403, "Accès administrateur requis")
    return user


# Offline downloads: /api/downloads*, /downloads/files/{id}, /api/online (logic in downloads.py)
app.include_router(downloads.build_router(require_auth))
# Offline playback of finished downloads: /api/local/*, /local/* (logic in local_media.py)
app.include_router(local_media.build_router(require_auth))
# Watch a download while it is still running: /play/partial/*, /partial/* (partial_play.py)
app.include_router(partial_play.build_router(require_auth))
# IPTV line free or busy (sidebar dot): /api/line-status (line_status.py)
app.include_router(line_status.build_router(require_auth))


# =============================================================================
# Auth Routes
# =============================================================================


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Initial setup page - create first admin user."""
    if not auth.is_setup_required():
        return RedirectResponse("/login", status_code=303)
    return TEMPLATES.TemplateResponse(request, "setup.html", {"error": None})


@app.post("/setup")
async def setup_create_user(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm: Annotated[str, Form()],
):
    """Create the initial admin user."""
    if not auth.is_setup_required():
        return RedirectResponse("/login", status_code=303)
    # Validate
    if len(username) < 3:
        return TEMPLATES.TemplateResponse(
            request, "setup.html", {"error": "Le nom d'utilisateur doit contenir au moins 3 caractères"}
        )
    if len(password) < 8:
        return TEMPLATES.TemplateResponse(
            request, "setup.html", {"error": "Le mot de passe doit contenir au moins 8 caractères"}
        )
    if password != confirm:
        return TEMPLATES.TemplateResponse(
            request, "setup.html", {"error": "Les mots de passe ne correspondent pas"}
        )
    auth.create_user(username, password)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None):
    if AUTO_LOGIN_USER:
        return RedirectResponse("/", status_code=303)
    """Login page - redirects to setup if no users exist."""
    if auth.is_setup_required():
        return RedirectResponse("/setup", status_code=303)
    last_user = request.cookies.get("last_user", "")
    return TEMPLATES.TemplateResponse(
        request, "login.html", {"error": error, "last_user": last_user}
    )


def _check_rate_limit(ip: str) -> None:
    """Check login rate limit. Raises HTTPException if exceeded."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # Clean old attempts for this IP
    attempts = [t for t in attempts if now - t < _LOGIN_WINDOW]
    if attempts:
        _login_attempts[ip] = attempts
    elif ip in _login_attempts:
        del _login_attempts[ip]
    # Periodically clean stale IPs (when dict is large)
    if len(_login_attempts) > 1000:
        stale = [k for k, v in _login_attempts.items() if not v or now - max(v) > _LOGIN_WINDOW]
        for k in stale[:100]:
            del _login_attempts[k]
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(429, "Trop de tentatives de connexion, réessayez plus tard")


@app.post("/login")
async def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    """Authenticate user and create session."""
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit(ip)
    if not verify_password(username, password):
        _login_attempts.setdefault(ip, []).append(time.time())
        return RedirectResponse("/login?error=invalid", status_code=303)
    token = create_token({"sub": username})
    response = RedirectResponse("/", status_code=303)
    is_secure = request.url.scheme == "https" or "https" in request.headers.get("x-forwarded-proto", "").lower() or "https" in request.headers.get("x-forwarded-scheme", "").lower()
    response.set_cookie(
        "token", token, httponly=True, samesite="strict", max_age=86400 * 7, secure=is_secure
    )
    response.set_cookie("last_user", username, max_age=86400 * 365, secure=is_secure)
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("token")
    return response


# =============================================================================
# Main Pages
# =============================================================================


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, _user: Annotated[dict, Depends(require_auth)]):
    return RedirectResponse("/guide", status_code=303)


def _fetch_all_epg(epg_urls: list[tuple[str, int, str]]) -> int:
    """Fetch EPG from all URLs into sqlite (in parallel). Returns total program count."""
    user_agent = ffmpeg_command.get_user_agent()

    def fetch_one(url_timeout_source: tuple[str, int, str]) -> tuple[str, int]:
        url, timeout, source_id = url_timeout_source
        try:
            log.info("Fetching EPG (timeout=%ds): %s", timeout, url[:80])
            count = fetch_epg(url, CACHE_DIR, timeout=timeout, source_id=source_id, user_agent=user_agent)
            log.info("EPG done: %d programs from %s", count, url[:50])
            return url, count
        except Exception as e:
            log.error("EPG failed: %s - %s", url[:50], e)
            return url, 0

    total = 0
    max_workers = min(len(epg_urls) or 1, 8)  # Cap at 8 concurrent fetches
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(fetch_one, u) for u in epg_urls]
        for future in concurrent.futures.as_completed(futures):
            _, count = future.result()
            total += count

    # Prune expired programs (keep 24h buffer)
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    pruned = epg.prune_old_programs(cutoff)
    if pruned:
        log.info("Pruned %d expired EPG programs", pruned)

    log.info("EPG fetch complete: %d programs total", total)
    return total


def load_all_epg(epg_urls: list[tuple[str, int, str]]) -> None:
    """Load EPG into sqlite database if empty.

    Args:
        epg_urls: List of (url, timeout, source_id) tuples
    """
    if epg.has_programs():
        log.info("EPG database has %d programs", epg.get_program_count())
        return

    # No data - fetch synchronously
    with _epg_fetch_lock:
        if epg.has_programs():
            return
        log.info("No EPG data, fetching")
        try:
            _fetch_all_epg(epg_urls)
        except Exception as e:
            log.error("EPG fetch failed: %s", e)
            get_cache()["epg_error"] = str(e)


def _start_guide_background_load() -> None:
    """Start background loading of guide data if not already in progress."""
    if "guide_load" in get_refresh_in_progress():
        return
    get_refresh_in_progress().add("guide_load")

    def load():
        try:
            log.info("Loading guide data in background")
            cats, streams, epg_urls = load_all_live_data()
            with get_cache_lock():
                get_cache()["live_categories"] = cats
                get_cache()["live_streams"] = streams
                get_cache()["epg_urls"] = epg_urls
            try:
                _fetch_all_epg(epg_urls)
            except Exception as e:
                with get_cache_lock():
                    get_cache()["epg_error"] = str(e)
            log.info("Guide data loaded")
        finally:
            get_refresh_in_progress().discard("guide_load")

    threading.Thread(target=load, daemon=True).start()


@app.get("/events/epg")
async def epg_events(_user: Annotated[dict, Depends(require_auth)]):
    """SSE endpoint - notifies when EPG is ready."""
    if len(_epg_subscribers) >= _MAX_SSE_SUBSCRIBERS:
        raise HTTPException(503, "Too many subscribers")
    queue: asyncio.Queue[str] = asyncio.Queue()
    _epg_subscribers.add(queue)

    async def event_stream():
        try:
            # If EPG already loaded, send immediately
            if epg.has_programs():
                yield "data: epg_ready\n\n"
                return
            # Wait for EPG ready event or shutdown
            while True:
                if _shutdown_event and _shutdown_event.is_set():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1)
                    yield f"data: {event}\n\n"
                    return
                except TimeoutError:
                    continue
        except TimeoutError:
            yield "data: timeout\n\n"
        finally:
            _epg_subscribers.discard(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/guide", response_class=HTMLResponse)
async def guide_page(
    request: Request,
    user: Annotated[dict, Depends(require_auth)],
    offset: int = 0,  # hours offset from now
    cats: str = "",  # comma-separated category IDs
):
    username = user.get("sub", "")
    # Check if cats was explicitly in URL (even if empty)
    cats_in_url = "cats" in request.query_params

    # If no channel data in memory, try file cache first (async to avoid blocking)
    if "live_categories" not in get_cache() or "live_streams" not in get_cache():
        cached = await asyncio.to_thread(load_file_cache, "live_data")
        if cached:
            data, _ = cached
            with get_cache_lock():
                get_cache()["live_categories"] = data["cats"]
                get_cache()["live_streams"] = data["streams"]
                get_cache()["epg_urls"] = parse_epg_urls(data.get("epg_urls", []))
        else:
            # No cache at all - start background load and show loading page
            _start_guide_background_load()
            return TEMPLATES.TemplateResponse(
                request,
                "guide.html",
                {
                    "grid_data": [],
                    "selected_cats": [],
                    "cats_param": cats,
                    "time_markers": [],
                    "offset": offset,
                    "window_start": "",
                    "loading_message": "Chargement des chaînes…",
                    "channel_count": 0,
                    "loading": True,
                },
            )

    categories = get_cache()["live_categories"]
    # EPG is optional - check sqlite db for data
    epg_loading = not epg.has_programs()

    # Get the full saved filter for dropdown (not just current URL filter)
    user_settings = load_user_settings(username)
    saved_filter_list = user_settings.get("guide_filter", [])
    saved_filter = set(saved_filter_list)  # For fast lookup
    # Build ordered list of category objects matching user's saved order
    cat_by_id = {str(c["category_id"]): c for c in categories}
    ordered_filter_cats = [cat_by_id[cid] for cid in saved_filter_list if cid in cat_by_id]

    # Get saved VIEW selection (separate from Settings filter)
    saved_view_cats = user_settings.get("guide_selected_cats")  # None = show all

    # Determine effective cats: URL param (if present) > saved view > all from filter
    if cats_in_url:
        # URL explicitly has cats param (could be empty for "none")
        effective_cats = cats
    elif saved_view_cats is not None:
        # Use saved view selection (could be [] for "none")
        effective_cats = ",".join(saved_view_cats)
    else:
        # Default: show all from settings filter
        effective_cats = ",".join(saved_filter_list)

    # Use helper to get filtered/sorted streams
    streams, ordered_cats, selected_cats = _get_guide_streams(effective_cats, username)
    total_count = len(streams)

    # Time window: 3 hours starting from offset
    now = datetime.now(UTC)
    window_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=offset)
    window_end = window_start + timedelta(hours=3)

    # Virtual scrolling: only render first batch, JS fetches rest on scroll
    # When disabled, render all rows server-side
    virtual_scroll_enabled = user_settings.get("virtual_scroll", True)
    initial_batch_size = 50 if virtual_scroll_enabled else total_count
    grid_data = _build_guide_rows(streams, 0, initial_batch_size, window_start, window_end)

    # Time markers (every 30 min) - convert to local time for display
    time_markers = []
    for i in range(7):  # 0, 30, 60, 90, 120, 150, 180 minutes
        t = window_start + timedelta(minutes=i * 30)
        t_local = t.astimezone()  # Convert to local timezone
        time_markers.append(
            {
                "label": t_local.strftime("%H:%M"),
                "left_pct": (i * 30 / 180) * 100,
            }
        )

    # Mobile time markers (2 hour window instead of 3)
    time_markers_mobile = []
    for i in range(5):  # 0, 30, 60, 90, 120 minutes
        t = window_start + timedelta(minutes=i * 30)
        t_local = t.astimezone()
        time_markers_mobile.append(
            {
                "label": t_local.strftime("%H:%M"),
                "left_pct": (i * 30 / 120) * 100,
            }
        )

    return TEMPLATES.TemplateResponse(
        request,
        "guide.html",
        {
            "categories": categories,
            "selected_cats": selected_cats,
            "saved_filter": saved_filter,  # Full saved filter for dropdown (set)
            "ordered_filter_cats": ordered_filter_cats,  # Ordered list for dropdown
            "cats_param": cats,
            "effective_cats": effective_cats,  # What's actually being used
            "grid_data": grid_data,
            "time_markers": time_markers,
            "time_markers_mobile": time_markers_mobile,
            "offset": offset,
            "window_start": window_start.strftime("%Y-%m-%d %H:%M"),
            "epg_error": get_cache().get("epg_error"),
            "epg_loading": epg_loading,
            "channel_count": len(grid_data),
            "total_count": total_count,  # For virtual scrolling
            "virtual_scroll": virtual_scroll_enabled,
            "loading": False,
            "content_access": _get_content_access(username),
        },
    )


def _get_guide_streams(cats: str, username: str) -> tuple[list[dict], list[str], set[str]]:
    """Get filtered and sorted streams for guide.

    Returns:
        Tuple of (filtered_streams, ordered_cat_ids, selected_cat_set)
    """
    all_streams = get_cache().get("live_streams", [])

    # Parse selected category IDs (ordered list)
    ordered_cats: list[str] = []
    if cats:
        ordered_cats = [c.strip() for c in cats.split(",") if c.strip()]
    selected_cats = set(ordered_cats)

    if not selected_cats:
        return [], ordered_cats, selected_cats

    # Get user's unavailable groups for filtering
    user_limits = auth.get_user_limits(username)
    unavailable_groups = set(user_limits.get("unavailable_groups", []))

    cat_order = {c: i for i, c in enumerate(ordered_cats)}

    def stream_sort_key(s: dict) -> int:
        for c in s.get("category_ids") or []:
            if str(c) in cat_order:
                return cat_order[str(c)]
        return len(ordered_cats)

    def stream_allowed(s: dict) -> bool:
        cat_ids = s.get("category_ids") or []
        return not any(f"cat:{c}" in unavailable_groups for c in cat_ids)

    streams = [
        s
        for s in all_streams
        if any(str(c) in selected_cats for c in (s.get("category_ids") or [])) and stream_allowed(s)
    ]
    streams.sort(key=stream_sort_key)

    return streams, ordered_cats, selected_cats


def _build_guide_rows(
    streams: list[dict],
    start_idx: int,
    count: int,
    window_start: datetime,
    window_end: datetime,
) -> list[dict]:
    """Build guide grid rows for a range of streams.

    Returns:
        List of row dicts with channel info and programs.
    """
    end_idx = min(start_idx + count, len(streams))
    slice_streams = streams[start_idx:end_idx]

    if not slice_streams:
        return []

    # Collect EPG IDs for batch query
    epg_ids = [s.get("epg_channel_id") or "" for s in slice_streams]
    epg_ids_set = [e for e in epg_ids if e]

    # Batch fetch icons and programs
    icons_map = epg.get_icons_batch(epg_ids_set) if epg_ids_set else {}

    # Build preferred_sources for EPG matching
    preferred_sources = {
        epg_id: s.get("source_id", "")
        for s, epg_id in zip(slice_streams, epg_ids, strict=False)
        if epg_id and s.get("source_id")
    }
    programs_map = (
        epg.get_programs_batch(epg_ids_set, window_start, window_end, preferred_sources)
        if epg_ids_set
        else {}
    )

    # Build rows
    window_end_mobile = window_start + timedelta(hours=2)
    grid_data = []

    for idx, (s, epg_id) in enumerate(zip(slice_streams, epg_ids, strict=False), start=start_idx):
        icon = s.get("stream_icon", "") or icons_map.get(epg_id, "")
        ch = {
            "stream_id": s["stream_id"],
            "name": s["name"],
            "icon": icon,
            "epg_id": epg_id,
            "category_ids": [str(category_id) for category_id in (s.get("category_ids") or [])],
        }
        row = {"channel": ch, "programs": [], "programs_mobile": [], "index": idx}

        for p in programs_map.get(epg_id, []):
            p_start = max(p.start, window_start)
            p_end = min(p.stop, window_end)
            start_mins = (p_start - window_start).total_seconds() / 60
            duration_mins = (p_end - p_start).total_seconds() / 60
            left_pct = (start_mins / 180) * 100
            width_pct = (duration_mins / 180) * 100
            row["programs"].append(
                {
                    "title": p.title,
                    "desc": p.desc,
                    "start": p.start.strftime("%H:%M"),
                    "end": p.stop.strftime("%H:%M"),
                    "start_timestamp": p.start.timestamp(),
                    "end_timestamp": p.stop.timestamp(),
                    "left_pct": left_pct,
                    "width_pct": width_pct,
                }
            )
            # Mobile: 2-hour window
            if p.start < window_end_mobile:
                p_end_m = min(p.stop, window_end_mobile)
                duration_mins_m = (p_end_m - p_start).total_seconds() / 60
                left_pct_m = (start_mins / 120) * 100
                width_pct_m = (duration_mins_m / 120) * 100
                row["programs_mobile"].append(
                    {
                        "title": p.title,
                        "desc": p.desc,
                        "start": p.start.strftime("%H:%M"),
                        "end": p.stop.strftime("%H:%M"),
                        "left_pct": left_pct_m,
                        "width_pct": width_pct_m,
                    }
                )

        grid_data.append(row)

    return grid_data


@app.get("/api/guide/rows")
async def guide_rows_api(
    user: Annotated[dict, Depends(require_auth)],
    start: int = Query(default=0, ge=0, description="Starting row index"),
    count: int = Query(default=130, ge=1, le=500, description="Number of rows to fetch"),
    offset: int = Query(default=0, ge=-168, le=168, description="Hours offset from now"),
    cats: str = "",
):
    """API endpoint for virtual scrolling - returns guide rows as JSON."""
    username = user.get("sub", "")

    # Use saved filter if no cats provided
    if not cats:
        user_settings = load_user_settings(username)
        saved = user_settings.get("guide_filter", [])
        if saved:
            cats = ",".join(saved)

    # Ensure data is loaded
    if "live_streams" not in get_cache():
        cached = await asyncio.to_thread(load_file_cache, "live_data")
        if cached:
            data, _ = cached
            with get_cache_lock():
                get_cache()["live_categories"] = data["cats"]
                get_cache()["live_streams"] = data["streams"]
                get_cache()["epg_urls"] = parse_epg_urls(data.get("epg_urls", []))

    streams, ordered_cat_ids, _ = _get_guide_streams(cats, username)
    total_count = len(streams)

    if total_count == 0:
        return JSONResponse({"rows": [], "categories": [], "total": 0, "start": start})

    category_by_id = {
        str(category["category_id"]): category
        for category in get_cache().get("live_categories", [])
    }
    populated_category_ids = {
        str(category_id)
        for stream in streams
        for category_id in (stream.get("category_ids") or [])
    }
    guide_categories = [
        {
            "category_id": category_id,
            "category_name": category_by_id[category_id]["category_name"],
        }
        for category_id in ordered_cat_ids
        if category_id in category_by_id and category_id in populated_category_ids
    ]

    # Time window
    now = datetime.now(UTC)
    window_start = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=offset)
    window_end = window_start + timedelta(hours=3)

    rows = _build_guide_rows(streams, start, count, window_start, window_end)

    return JSONResponse(
        {
            "rows": rows,
            "categories": guide_categories,
            "total": total_count,
            "start": start,
            "window_start_timestamp": window_start.timestamp(),
        },
        headers={"Cache-Control": "no-store"},
    )


def _start_vod_background_load() -> None:
    """Start background loading of VOD data if not already in progress."""
    if "vod_load" in get_refresh_in_progress():
        return
    get_refresh_in_progress().add("vod_load")

    def load():
        try:
            log.info("Loading VOD data in background")
            vod_cats, vod_streams = load_vod_data()
            with get_cache_lock():
                get_cache()["vod_categories"] = vod_cats
                get_cache()["vod_streams"] = vod_streams
            log.info("VOD data loaded")
        finally:
            get_refresh_in_progress().discard("vod_load")

    threading.Thread(target=load, daemon=True).start()


def _clean_lang(lang: str) -> str:
    """Validate the `lang` query parameter (unknown values mean no filter)."""
    lang = (lang or "").strip().upper()
    return lang if lang and catalog_lang.is_valid(lang) else ""


def _lang_options(counts: dict[str, int], current: str = "") -> list[dict]:
    """Dropdown options with item counts; empty options are hidden unless counts are unknown."""
    opts = []
    for lg in catalog_lang.LANGUAGES:
        n = counts.get(lg["key"]) if counts else None
        if counts and not n and lg["key"] != current:
            continue
        opts.append({"key": lg["key"], "label": lg["label"], "hint": lg.get("hint", ""), "count": (n or 0) if counts else None})
    return opts


def _platform_filter(
    kind: str, platform: str, categories: list[dict], items: list[dict]
) -> tuple[dict | None, list[dict]]:
    """Keep the items of a discovery group (?platform=netflix, cinema, anime...).

    Returns ({"id", "label"} for the template or None, filtered items). Unknown
    groups are ignored.
    """
    import discover

    if not platform:
        return None, items
    cat_ids = discover.group_category_ids(kind, platform, categories)
    if cat_ids is None:
        return None, items
    group = discover.find_group(kind, platform)
    return (
        {"id": group.id, "label": group.label},
        [i for i in items if str(i.get("category_id")) in cat_ids],
    )


@app.get("/vod", response_class=HTMLResponse)
async def vod_page(
    request: Request,
    user: Annotated[dict, Depends(require_auth)],
    category: int | None = None,
    sort: str | None = None,
    lang: str = "",
    offset: int = 0,
    platform: str = "",
):
    lang = _clean_lang(lang)
    # Load from file cache if not in memory (async to avoid blocking)
    if "vod_categories" not in get_cache() or "vod_streams" not in get_cache():
        cached = await asyncio.to_thread(load_file_cache, "vod_data")
        if cached:
            data, _ = cached
            get_cache()["vod_categories"] = data["cats"]
            get_cache()["vod_streams"] = data["streams"]
        else:
            # No cache - start background load and show loading page
            _start_vod_background_load()
            username = user.get("sub", "")
            user_settings = load_user_settings(username)
            return TEMPLATES.TemplateResponse(
                request,
                "vod.html",
                {
                    "categories": [],
                    "streams": [],
                    "current_category": category,
                    "current_sort": sort,
                    "loading": True,
                    "favorites": user_settings.get("favorites", {"series": {}, "movies": {}}),
                },
            )

    username = user.get("sub", "")
    user_settings = load_user_settings(username)

    # Check if user has access to any movies
    content_access = _get_content_access(username)
    if not content_access["movies"]:
        raise HTTPException(403, "L'accès aux films est restreint")

    # Get user's unavailable groups for filtering
    user_limits = auth.get_user_limits(username)
    unavailable_groups = set(user_limits.get("unavailable_groups", []))

    # Filter by group access (movies:{source_id})
    def movie_allowed(s: dict) -> bool:
        source_id = s.get("source_id", "")
        return f"movies:{source_id}" not in unavailable_groups

    streams = [s for s in get_cache()["vod_streams"] if movie_allowed(s)]
    categories = [
        c
        for c in get_cache()["vod_categories"]
        if f"movies:{c.get('source_id', '')}" not in unavailable_groups
    ]

    # Apply user's VOD category filter (if set)
    vod_filter = user_settings.get("vod_filter", [])
    if vod_filter:
        vod_filter_set = set(str(c) for c in vod_filter)
        categories = [c for c in categories if str(c.get("category_id")) in vod_filter_set]
        streams = [s for s in streams if str(s.get("category_id")) in vod_filter_set]

    # Filter by category if specified
    if category:
        streams = [s for s in streams if str(s.get("category_id")) == str(category)]
    platform_group, streams = _platform_filter("movies", platform, categories, streams)

    # Language filter (counts cover the current category, before the language filter)
    lang_names = catalog_lang.category_names(get_cache()["vod_categories"])
    lang_options = _lang_options(catalog_lang.option_counts(streams, lang_names), lang)
    streams = catalog_lang.filter_by_language(streams, lang, lang_names)

    # Sort
    if sort == "alpha":
        streams.sort(key=lambda s: (s.get("name") or "").lower())
    elif sort == "rating":
        streams.sort(key=lambda s: _safe_float(s.get("rating")), reverse=True)
    elif sort == "newest":
        streams.sort(key=lambda s: int(s.get("added") or 0), reverse=True)

    total = len(streams)
    streams, next_url = _page_slice(streams, offset, request)
    if offset > 0:
        return TEMPLATES.TemplateResponse(
            request, "_vod_cards.html", {"streams": streams, "next_url": next_url}
        )

    return TEMPLATES.TemplateResponse(
        request,
        "vod.html",
        {
            "total": total,
            "next_url": next_url,
            "categories": categories,
            "streams": streams,
            "current_category": category,
            "current_sort": sort,
            "current_lang": lang,
            "current_platform": platform_group,
            "lang_options": lang_options,
            "favorites": user_settings.get("favorites", {"series": {}, "movies": {}}),
            "content_access": _get_content_access(username),
        },
    )


@app.get("/api/discover")
async def api_discover(
    user: Annotated[dict, Depends(require_auth)],
    lang: str = "",
    kind: str = "movies",
):
    """Discovery rows for the Movies or Series landing page (cached per catalog, day, lang)."""
    import discover

    lang = _clean_lang(lang)
    if kind not in ("movies", "series"):
        raise HTTPException(400, "Unknown kind")
    if kind == "movies":
        cats_key, items_key, file_key, items_field = "vod_categories", "vod_streams", "vod_data", "streams"
        start_load, filter_key = _start_vod_background_load, "vod_filter"
    else:
        cats_key, items_key, file_key, items_field = "series_categories", "series", "series_data", "series"
        start_load, filter_key = _start_series_background_load, "series_filter"
    if cats_key not in get_cache() or items_key not in get_cache():
        cached = await asyncio.to_thread(load_file_cache, file_key)
        if not cached:
            start_load()
            return {"rows": [], "loading": True}
        data, _ = cached
        get_cache()[cats_key] = data["cats"]
        get_cache()[items_key] = data[items_field]
    username = user.get("sub", "")
    if not _get_content_access(username)[kind]:
        what = "aux films" if kind == "movies" else "aux séries"
        raise HTTPException(403, f"L'accès {what} est restreint")
    unavailable = frozenset(auth.get_user_limits(username).get("unavailable_groups", []))
    cat_filter = frozenset(str(c) for c in load_user_settings(username).get(filter_key, []))
    rows = await asyncio.to_thread(
        discover.get_rows,
        get_cache()[items_key],
        get_cache()[cats_key],
        lang,
        None,
        _logo_url_filter,
        unavailable,
        cat_filter,
        kind,
    )
    return {"rows": rows}


def _start_series_background_load() -> None:
    """Start background loading of series data if not already in progress."""
    if "series_load" in get_refresh_in_progress():
        return
    get_refresh_in_progress().add("series_load")

    def load():
        try:
            log.info("Loading series data in background")
            series_cats, series_list = load_series_data()
            with get_cache_lock():
                get_cache()["series_categories"] = series_cats
                get_cache()["series"] = series_list
            log.info("Series data loaded")
        finally:
            get_refresh_in_progress().discard("series_load")

    threading.Thread(target=load, daemon=True).start()


@app.get("/series", response_class=HTMLResponse)
async def series_page(
    request: Request,
    user: Annotated[dict, Depends(require_auth)],
    category: int | None = None,
    sort: str | None = None,
    lang: str = "",
    offset: int = 0,
    platform: str = "",
):
    lang = _clean_lang(lang)
    # Load from file cache if not in memory (async to avoid blocking)
    if "series_categories" not in get_cache() or "series" not in get_cache():
        cached = await asyncio.to_thread(load_file_cache, "series_data")
        if cached:
            data, _ = cached
            get_cache()["series_categories"] = data["cats"]
            get_cache()["series"] = data["series"]
        else:
            # No cache - start background load and show loading page
            _start_series_background_load()
            username = user.get("sub", "")
            user_settings = load_user_settings(username)
            return TEMPLATES.TemplateResponse(
                request,
                "series.html",
                {
                    "categories": [],
                    "series": [],
                    "current_category": category,
                    "current_sort": sort,
                    "loading": True,
                    "favorites": user_settings.get("favorites", {"series": {}, "movies": {}}),
                },
            )

    username = user.get("sub", "")
    user_settings = load_user_settings(username)

    # Check if user has access to any series
    content_access = _get_content_access(username)
    if not content_access["series"]:
        raise HTTPException(403, "L'accès aux séries est restreint")

    # Get user's unavailable groups for filtering
    user_limits = auth.get_user_limits(username)
    unavailable_groups = set(user_limits.get("unavailable_groups", []))

    # Filter by group access (series:{source_id})
    def series_allowed(s: dict) -> bool:
        source_id = s.get("source_id", "")
        return f"series:{source_id}" not in unavailable_groups

    series = [s for s in get_cache()["series"] if series_allowed(s)]
    categories = [
        c
        for c in get_cache()["series_categories"]
        if f"series:{c.get('source_id', '')}" not in unavailable_groups
    ]

    # Apply user's series category filter (if set)
    series_filter = user_settings.get("series_filter", [])
    if series_filter:
        series_filter_set = set(str(c) for c in series_filter)
        categories = [c for c in categories if str(c.get("category_id")) in series_filter_set]
        series = [s for s in series if str(s.get("category_id")) in series_filter_set]

    # Filter by category if specified
    if category:
        series = [s for s in series if str(s.get("category_id")) == str(category)]
    platform_group, series = _platform_filter("series", platform, categories, series)

    # Language filter (counts cover the current category, before the language filter)
    lang_names = catalog_lang.category_names(get_cache()["series_categories"])
    lang_options = _lang_options(catalog_lang.option_counts(series, lang_names), lang)
    series = catalog_lang.filter_by_language(series, lang, lang_names)

    # Sort
    if sort == "alpha":
        series.sort(key=lambda s: (s.get("name") or "").lower())
    elif sort == "rating":
        series.sort(key=lambda s: _safe_float(s.get("rating")), reverse=True)
    elif sort == "newest":
        series.sort(key=lambda s: int(s.get("last_modified") or 0), reverse=True)
    elif sort == "release":
        series.sort(key=lambda s: str(s.get("releaseDate") or s.get("release_date") or ""), reverse=True)

    total = len(series)
    series, next_url = _page_slice(series, offset, request)
    if offset > 0:
        return TEMPLATES.TemplateResponse(
            request, "_series_cards.html", {"series": series, "next_url": next_url}
        )

    return TEMPLATES.TemplateResponse(
        request,
        "series.html",
        {
            "total": total,
            "next_url": next_url,
            "categories": categories,
            "series": series,
            "current_category": category,
            "current_sort": sort,
            "current_lang": lang,
            "current_platform": platform_group,
            "lang_options": lang_options,
            "favorites": user_settings.get("favorites", {"series": {}, "movies": {}}),
            "content_access": _get_content_access(username),
        },
    )


@app.get("/series/{series_id}", response_class=HTMLResponse)
async def series_detail_page(
    request: Request,
    series_id: int,
    user: Annotated[dict, Depends(require_auth)],
    refresh: bool = False,
):
    username = user.get("sub", "")

    # Check access for this specific series and get source_id
    source_id = ""
    if "series" in get_cache():
        cached_series = next(
            (s for s in get_cache()["series"] if str(s.get("series_id")) == str(series_id)),
            None,
        )
        if cached_series:
            source_id = cached_series.get("source_id", "")
            user_limits = auth.get_user_limits(username)
            unavailable_groups = set(user_limits.get("unavailable_groups", []))
            if f"series:{source_id}" in unavailable_groups:
                raise HTTPException(403, "L'accès à cette série est restreint")

    # Use the series' source, fall back to first Xtream source
    xtream = get_xtream_client_by_source(source_id) if source_id else None
    if not xtream:
        xtream = get_first_xtream_client()
    if not xtream:
        raise HTTPException(404, "Aucune source Xtream configurée")
    cache_key = f"series_info_{source_id}_{series_id}" if source_id else f"series_info_{series_id}"
    try:
        series_data = await asyncio.to_thread(
            get_cached_info, cache_key, lambda: xtream.get_series_info(series_id), refresh
        )
    except (urllib.error.URLError, TimeoutError) as e:
        return TEMPLATES.TemplateResponse(
            request,
            "error.html",
            {
                "title": "Erreur du fournisseur",
                "message": f"Impossible de charger les informations de la série : {e}",
            },
            status_code=502,
        )
    if refresh:
        log.info("Force refreshed series info %s", series_id)
    # Extract year from releaseDate if not present
    if series_data.get("info"):
        info = series_data["info"]
        if not info.get("year") and info.get("releaseDate"):
            info["year"] = info["releaseDate"][:4]
    # Strip redundant series title and episode numbers from episode titles
    if series_data.get("episodes"):
        for season_eps in series_data["episodes"].values():
            for ep in season_eps:
                if ep.get("title"):
                    # Remove patterns like "Series Name - S01E01 - Episode Title"
                    # Keep only the actual episode title
                    title = ep["title"]
                    # Remove S##E## - pattern
                    title = re.sub(r"^S\d+E\d+\s*-\s*", "", title)
                    # Remove any leading "SeriesName - " pattern
                    if " - " in title and len(title.split(" - ")) > 1:
                        parts = title.split(" - ")
                        # Take the last part which should be the actual episode title
                        title = parts[-1]
                    ep["title"] = title.strip()

                # Parse info field if it's JSON
                if ep.get("info"):
                    if isinstance(ep["info"], str):
                        try:
                            info_obj = json.loads(ep["info"])
                            # Extract plot/description from parsed JSON
                            if isinstance(info_obj, dict):
                                ep["description"] = (
                                    info_obj.get("plot") or info_obj.get("description") or ""
                                )
                        except (json.JSONDecodeError, TypeError):
                            pass
                    elif isinstance(ep["info"], dict):
                        # Already a dict
                        ep["description"] = (
                            ep["info"].get("plot") or ep["info"].get("description") or ""
                        )

    username = user.get("sub", "")
    user_settings = load_user_settings(username)
    return TEMPLATES.TemplateResponse(
        request,
        "series_detail.html",
        {
            "series": series_data,
            "series_id": series_id,
            "favorites": user_settings.get("favorites", {"series": {}, "movies": {}}),
        },
    )


@app.get("/movie/{stream_id}", response_class=HTMLResponse)
async def movie_detail_page(
    request: Request,
    stream_id: int,
    user: Annotated[dict, Depends(require_auth)],
):
    username = user.get("sub", "")

    # Load from file cache if not in memory
    if "vod_streams" not in get_cache():
        vod_cats, vod_streams = load_vod_data()
        get_cache()["vod_categories"] = vod_cats
        get_cache()["vod_streams"] = vod_streams

    vod_streams = get_cache().get("vod_streams", [])
    movie = next((m for m in vod_streams if m.get("stream_id") == stream_id), None)

    # Check access for this specific movie
    if movie:
        source_id = movie.get("source_id", "")
        user_limits = auth.get_user_limits(username)
        unavailable_groups = set(user_limits.get("unavailable_groups", []))
        if f"movies:{source_id}" in unavailable_groups:
            raise HTTPException(403, "L'accès à ce film est restreint")

    # Fetch detailed movie info
    if movie:
        source_id = movie.get("source_id", "")
        xtream = get_xtream_client_by_source(source_id) if source_id else None
        if not xtream:
            xtream = get_first_xtream_client()
        if xtream:
            cache_key = (
                f"vod_info_{source_id}_{stream_id}" if source_id else f"vod_info_{stream_id}"
            )
            try:
                vod_info = await asyncio.to_thread(
                    get_cached_info, cache_key, lambda: xtream.get_vod_info(stream_id)
                )
            except (urllib.error.URLError, TimeoutError):
                vod_info = {}
            if vod_info and vod_info.get("info"):
                info = vod_info["info"]
                # Merge detailed info into movie object
                movie = {**movie}  # Copy
                movie["plot"] = info.get("plot") or info.get("description", "")
                movie["director"] = info.get("director", "")
                movie["cast"] = info.get("cast") or info.get("actors", "")
                movie["genre"] = info.get("genre", "")
                movie["rating"] = info.get("rating", "")
                movie["year"] = info.get("releasedate", "")[:4] if info.get("releasedate") else ""
                movie["duration"] = info.get("duration", "")
                movie["cover_big"] = info.get("cover_big") or info.get("movie_image", "")
                movie["youtube_trailer"] = info.get("youtube_trailer", "")

    username = user.get("sub", "")
    user_settings = load_user_settings(username)
    return TEMPLATES.TemplateResponse(
        request,
        "movie_detail.html",
        {
            "movie": movie,
            "favorites": user_settings.get("favorites", {"series": {}, "movies": {}}),
        },
    )


@dataclass(slots=True)
class PlayerInfo:
    """Info needed to render the player page."""

    url: str = ""
    is_m3u: bool = False
    channel_name: str = ""
    program_title: str = ""
    program_desc: str = ""
    program_start: float = 0.0  # Unix timestamp of current program start (EPG)
    program_end: float = 0.0  # Unix timestamp of current program end (EPG)
    deinterlace_fallback: bool = True  # Used when probe is skipped
    source_id: str = ""  # Source ID for stream limit tracking
    category_ids: list[str] | None = None  # Category IDs for live streams (access check)


def _get_episode_desc(ep: dict) -> str:
    """Extract description from episode info (handles str or dict)."""
    info = ep.get("info")
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except (json.JSONDecodeError, TypeError):
            info = None
    if isinstance(info, dict):
        return info.get("plot") or info.get("description") or ""
    return ep.get("description") or ep.get("plot") or ""


def _get_current_program(epg_id: str) -> dict | None:
    """Current EPG program for a channel, or None when the guide has no entry."""
    if not epg_id:
        return None
    now = datetime.now(UTC)
    programs = epg.get_programs_in_range(epg_id, now, now + timedelta(minutes=1))
    if not programs:
        return None
    program = programs[0]
    return {
        "title": program.title,
        "desc": program.desc,
        "start": program.start.timestamp(),
        "end": program.stop.timestamp(),
    }


def _get_live_player_info(stream_id: str) -> PlayerInfo:
    """Get player info for live stream."""
    _ensure_live_cache()
    stream = next(
        (s for s in get_cache()["live_streams"] if str(s.get("stream_id")) == stream_id),
        None,
    )
    if not stream:
        return PlayerInfo()

    info = PlayerInfo(channel_name=stream.get("name", ""))

    if stream.get("direct_url"):
        info.url = stream["direct_url"]
        info.is_m3u = True
    elif stream.get("source_type") == "xtream":
        base, user, pwd = stream["source_url"], stream["source_username"], stream["source_password"]
        orig_id = stream_id.split("_")[-1] if "_" in stream_id else stream_id
        # URL-encode username/password to handle special chars like # in passwords
        user = urllib.parse.quote(user, safe="")
        pwd = urllib.parse.quote(pwd, safe="")
        info.url = f"{base}/live/{user}/{pwd}/{orig_id}.m3u8"

    # Look up source settings
    source_id = stream.get("source_id", "")
    info.source_id = source_id
    info.category_ids = stream.get("category_ids")
    if source_id:
        sources = load_server_settings().get("sources", [])
        source = next((s for s in sources if s.get("id") == source_id), None)
        if source:
            info.deinterlace_fallback = source.get("deinterlace_fallback", True)

    # Look up current program from EPG
    program = _get_current_program(stream.get("epg_channel_id") or "")
    if program:
        info.program_title = program["title"]
        info.program_desc = program["desc"]
        info.program_start = program["start"]
        info.program_end = program["end"]
    return info


def _get_movie_player_info(stream_id: str, ext: str) -> PlayerInfo:
    """Get player info for movie."""
    # Find movie in cache to get its source_id
    cached_movie = None
    if "vod_streams" in get_cache():
        cached_movie = next(
            (m for m in get_cache()["vod_streams"] if str(m.get("stream_id")) == str(stream_id)),
            None,
        )

    # Get client for the movie's source (fall back to first if not found)
    source_id = cached_movie.get("source_id", "") if cached_movie else ""
    xtream = get_xtream_client_by_source(source_id) if source_id else None
    if not xtream:
        xtream = get_first_xtream_client()
    if not xtream:
        return PlayerInfo()

    ext = ext or "mkv"
    info = PlayerInfo(url=xtream.build_stream_url("movie", int(stream_id), ext))
    info.source_id = source_id

    cache_key = f"vod_info_{source_id}_{stream_id}"
    try:
        movie = get_cached_info(cache_key, lambda: xtream.get_vod_info(int(stream_id)))
    except (urllib.error.URLError, TimeoutError):
        return info
    if movie and movie.get("info"):
        m = movie["info"]
        name = m.get("name", "")
        year = str(m.get("year") or m.get("releasedate", ""))[:4]
        info.channel_name = f"{name} ({year})" if year else name
        info.program_desc = m.get("plot") or m.get("description") or ""
    return info


def _get_series_player_info(
    stream_id: str, series_id: int | None, ext: str
) -> tuple[PlayerInfo, str | None]:
    """Get player info for series episode. Returns (info, next_episode_url)."""
    # Find series in cache to get its source_id
    cached_series = None
    source_id = ""
    if series_id and "series" in get_cache():
        cached_series = next(
            (s for s in get_cache()["series"] if str(s.get("series_id")) == str(series_id)),
            None,
        )
        if cached_series:
            source_id = cached_series.get("source_id", "")

    # Get client for the series' source (fall back to first if not found)
    xtream = get_xtream_client_by_source(source_id) if source_id else None
    if not xtream:
        xtream = get_first_xtream_client()
    if not xtream:
        return PlayerInfo(), None

    ext = ext or "mkv"
    info = PlayerInfo(url=xtream.build_stream_url("series", int(stream_id), ext))
    info.source_id = source_id

    if not series_id:
        return info, None

    cache_key = f"series_info_{source_id}_{series_id}"
    try:
        series = get_cached_info(cache_key, lambda: xtream.get_series_info(series_id))
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning("Failed to fetch series info %s: %s", series_id, e)
        return info, None
    if not series:
        return info, None

    if series.get("info"):
        name = series["info"].get("name", "")
        year = series["info"].get("year", "")
        info.channel_name = f"{name} ({year})" if year else name

    # Build flat list of all episodes in order (season, episode)
    all_episodes: list[tuple[int, dict]] = []
    for season_num, eps in sorted((series.get("episodes") or {}).items(), key=lambda x: int(x[0])):
        for ep in sorted(eps, key=lambda e: int(e.get("episode_num", 0))):
            all_episodes.append((int(season_num), ep))

    # Find current episode and next
    next_episode_url = None
    for i, (season_num, ep) in enumerate(all_episodes):
        if str(ep.get("id")) == str(stream_id):
            title = re.sub(r"^S\d+E\d+\s*-\s*", "", ep.get("title", ""))
            if " - " in title:
                title = title.split(" - ")[-1]
            info.program_title = (
                f"S{int(season_num):02d}E{int(ep.get('episode_num', 0)):02d} — {title.strip()}"
            )
            info.program_desc = _get_episode_desc(ep)
            # Get next episode URL
            if i + 1 < len(all_episodes):
                _, next_ep = all_episodes[i + 1]
                next_ext = next_ep.get("container_extension") or ext
                next_episode_url = (
                    f"/play/series/{next_ep['id']}?series_id={series_id}&ext={next_ext}"
                )
            break
    return info, next_episode_url


def _ensure_live_cache() -> None:
    """Ensure live streams and EPG are loaded."""
    if "live_streams" not in get_cache():
        cats, streams, epg_urls = load_all_live_data()
        with get_cache_lock():
            get_cache()["live_categories"] = cats
            get_cache()["live_streams"] = streams
            get_cache()["epg_urls"] = epg_urls
    if not epg.has_programs():
        with contextlib.suppress(Exception):
            load_all_epg(get_cache().get("epg_urls", []))


def _resolve_original_lang(
    kind: str,
    item_id: str,
    source_id: str = "",
    series_id: int | None = None,
    local_path: pathlib.Path | None = None,
    allow_fetch: bool = False,
) -> tuple[str, str]:
    """Original language (lang, source) of a movie or episode, never raises.

    Reads provider info from the file cache (already filled by the play page);
    ``allow_fetch`` lets the API endpoint ask the provider once (get_vod_info).
    """
    try:
        tmdb_id: Any = None
        country: Any = None
        tmdb_kind = "movie"
        if kind == "movie":
            keys = [f"vod_info_{source_id}_{item_id}", f"vod_info_{item_id}"]
            data = None
            for k in keys:
                cached = load_file_cache(k)
                if cached and cached[0]:
                    data = cached[0]
                    break
            if data is None and allow_fetch:
                xtream = get_xtream_client_by_source(source_id) if source_id else None
                xtream = xtream or get_first_xtream_client()
                if xtream:
                    data = get_cached_info(keys[0], lambda: xtream.get_vod_info(int(item_id)))
            info = (data or {}).get("info") or {}
            tmdb_id = info.get("tmdb_id") or info.get("tmdb")
            if not tmdb_id:
                tmdb_id = ((data or {}).get("movie_data") or {}).get("tmdb")
            country = info.get("country")
        else:
            tmdb_kind = "tv"
            if series_id:
                cached = load_file_cache(f"series_info_{source_id}_{series_id}")
                info = ((cached[0] if cached else None) or {}).get("info") or {}
                tmdb_id = info.get("tmdb") or info.get("tmdb_id")
                country = info.get("country")
        api_key = original_lang.get_api_key(load_server_settings())
        return original_lang.lookup(
            cache_dir=CACHE_DIR,
            tmdb_kind=tmdb_kind,
            tmdb_id=tmdb_id,
            api_key=api_key,
            local_path=local_path,
            country=country,
        )
    except Exception:
        log.debug("original language lookup failed", exc_info=True)
        return "", ""


@app.get("/api/original-lang")
async def original_lang_api(
    _user: Annotated[dict, Depends(require_auth)],
    kind: str,
    id: str,
    source_id: str = "",
    series_id: int | None = None,
):
    """Original language of a movie or episode: {"lang": "en", "source": "tmdb"}."""
    if kind not in ("movie", "episode"):
        raise HTTPException(400, "kind must be movie or episode")
    lang, source = await asyncio.to_thread(
        _resolve_original_lang, kind, id, source_id, series_id, None, True
    )
    return JSONResponse({"lang": lang, "source": source}, headers={"Cache-Control": "no-store"})


@app.get("/play/download/{download_id}", response_class=HTMLResponse)
async def player_download_page(
    request: Request,
    download_id: str,
    user: Annotated[dict, Depends(require_auth)],
):
    """Play a finished download through the regular player (works with no internet).

    The local file path is the player's source URL; /transcode/start accepts it
    only when it points inside the download folder. Must be declared before the
    generic /play/{stream_type}/{stream_id:path} route.
    """
    entry, path = downloads.get_manager().file_for(download_id)
    username = user.get("sub", "")
    user_settings = load_user_settings(username)
    raw_url = str(path)
    is_episode = entry.kind == "episode"
    watch_entry = get_watch_position(username, raw_url)
    orig_lang, orig_source = await asyncio.to_thread(
        _resolve_original_lang,
        "episode" if is_episode else "movie",
        entry.item_id,
        entry.source_id,
        entry.series_id if is_episode else None,
        path,
    )
    return TEMPLATES.TemplateResponse(
        request,
        "player.html",
        {
            "raw_url": raw_url,
            "stream_id": entry.item_id,
            "transcode_mode": "never",  # unused: local_download_id drives playback
            "local_download_id": download_id,
            "live_dvr_mins": 0,
            "stream_type": "series" if is_episode else "movie",
            "channel_name": entry.title,
            "program_title": "",
            "program_desc": "",
            "program_start": 0.0,
            "program_end": 0.0,
            "captions_enabled": user_settings.get("captions_enabled", False),
            "resume_position": watch_entry.get("position", 0.0) if watch_entry else 0.0,
            "series_id": entry.series_id if is_episode else None,
            "episode_id": int(entry.item_id) if is_episode and entry.item_id.isdigit() else None,
            "series_name": "",
            "cc_lang": user_settings.get("cc_lang", ""),
            "cc_style": user_settings.get("cc_style", {}),
            "cast_host": cast_host_for(request, user_settings),
            "next_episode_url": None,
            "deinterlace_fallback": False,
            "source_id": "",
            "original_lang": orig_lang,
            "original_lang_source": orig_source,
            "content_access": _get_content_access(username),
        },
    )


@app.get("/play/{stream_type}/{stream_id:path}", response_class=HTMLResponse)
async def player_page(
    request: Request,
    stream_type: str,
    stream_id: str,
    user: Annotated[dict, Depends(require_auth)],
    ext: str = "",
    series_id: int | None = None,
):
    """Render player page for live/movie/series stream."""
    username = user.get("sub", "")
    next_episode_url = None
    if stream_type == "live":
        info = await asyncio.to_thread(_get_live_player_info, stream_id)
    elif stream_type == "movie":
        info = await asyncio.to_thread(_get_movie_player_info, stream_id, ext)
    elif stream_type == "series":
        info, next_episode_url = await asyncio.to_thread(
            _get_series_player_info, stream_id, series_id, ext
        )
    else:
        raise HTTPException(404, "Type de flux invalide")

    if not info.url:
        raise HTTPException(404, "Flux introuvable")

    # Check user's group access
    user_limits = auth.get_user_limits(username)
    unavailable_groups = set(user_limits.get("unavailable_groups", []))
    log.info(
        "Access check: user=%s type=%s source_id=%s unavailable=%s",
        username,
        stream_type,
        info.source_id,
        unavailable_groups,
    )
    if unavailable_groups:
        if stream_type == "live" and info.category_ids:
            # Live streams: blocked if any category is unavailable
            if any(f"cat:{cat_id}" in unavailable_groups for cat_id in info.category_ids):
                raise HTTPException(403, "L'accès à cette chaîne est restreint")
        elif stream_type == "movie" and info.source_id:
            if f"movies:{info.source_id}" in unavailable_groups:
                raise HTTPException(403, "L'accès aux films est restreint")
        elif (
            stream_type == "series"
            and info.source_id
            and f"series:{info.source_id}" in unavailable_groups
        ):
            raise HTTPException(403, "L'accès aux séries est restreint")

    log.info("Play %s/%s: %s", stream_type, stream_id, info.url)

    server_settings = load_server_settings()
    user_settings = load_user_settings(username)
    transcode_mode = server_settings.get("transcode_mode", "auto")
    is_https = request.url.scheme == "https" or "https" in request.headers.get("x-forwarded-proto", "").lower() or "https" in request.headers.get("x-forwarded-scheme", "").lower()
    if transcode_mode == "auto":
        needs_transcode = info.is_m3u or ext in ("mkv", "mp4", "avi", "wmv", "flv")
        mixed_content = is_https and info.url.startswith("http://")
        # Pause/rewind requires a server-side buffer; route live through a
        # local HLS session (stream copy when codecs allow) when DVR is on.
        needs_dvr = stream_type == "live" and server_settings.get("live_dvr_mins", 0) > 0
        if needs_transcode or mixed_content or needs_dvr:
            transcode_mode = "always"

    # Get saved watch position for VOD (per-user)
    resume_position = 0.0
    if stream_type in ("movie", "series"):
        watch_entry = get_watch_position(username, info.url)
        if watch_entry:
            resume_position = watch_entry.get("position", 0.0)

    # For series, stream_id is episode_id
    episode_id = int(stream_id) if stream_type == "series" and stream_id.isdigit() else None
    # Extract series name from channel_name (format: "Series Name (Year)" or just "Series Name")
    series_name = ""
    if stream_type == "series" and info.channel_name:
        # Strip year suffix like " (2020)"
        series_name = re.sub(r"\s*\(\d{4}\)$", "", info.channel_name)

    orig_lang, orig_source = "", ""
    if stream_type in ("movie", "series"):
        orig_lang, orig_source = await asyncio.to_thread(
            _resolve_original_lang,
            "movie" if stream_type == "movie" else "episode",
            stream_id,
            info.source_id,
            series_id,
        )

    return TEMPLATES.TemplateResponse(
        request,
        "player.html",
        {
            "raw_url": info.url,
            "stream_id": stream_id,
            "transcode_mode": transcode_mode,
            "live_dvr_mins": server_settings.get("live_dvr_mins", 0),
            "stream_type": stream_type,
            "channel_name": info.channel_name,
            "program_title": info.program_title,
            "program_desc": info.program_desc,
            "program_start": info.program_start,
            "program_end": info.program_end,
            "captions_enabled": user_settings.get("captions_enabled", False),
            "resume_position": resume_position,
            "series_id": series_id,
            "episode_id": episode_id,
            "series_name": series_name,
            "cc_lang": user_settings.get("cc_lang", ""),
            "cc_style": user_settings.get("cc_style", {}),
            "cast_host": cast_host_for(request, user_settings),
            "next_episode_url": next_episode_url,
            "deinterlace_fallback": info.deinterlace_fallback,
            "source_id": info.source_id,
            "original_lang": orig_lang,
            "original_lang_source": orig_source,
            "content_access": _get_content_access(username),
        },
    )


@app.get("/api/live/program/{stream_id:path}")
async def live_program_api(
    stream_id: str,
    user: Annotated[dict, Depends(require_auth)],
):
    """Current EPG program for a live channel, polled when one ends mid-watch."""

    def lookup() -> dict | None:
        _ensure_live_cache()
        stream = next(
            (s for s in get_cache()["live_streams"] if str(s.get("stream_id")) == stream_id),
            None,
        )
        if not stream:
            raise HTTPException(404, "Flux introuvable")
        return _get_current_program(stream.get("epg_channel_id") or "")

    program = await asyncio.to_thread(lookup)
    return JSONResponse(
        program or {"title": "", "desc": "", "start": 0.0, "end": 0.0},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    user: Annotated[dict, Depends(require_auth)],
    q: str = "",
    regex: bool = False,
    live: bool = False,
    vod: bool = False,
    series: bool = False,
    limit: int = 100,
    lang: str = "",
):
    results: dict[str, list] = {"live": [], "vod": [], "series": []}
    limit = limit if 0 < limit <= 500 else 100  # never render an unbounded result list
    lang = _clean_lang(lang)
    lang_counts: dict[str, int] = {}
    lang_matched: list[dict] = []
    lang_names: dict[tuple[str, str], str] = {}

    # Default all on if none specified
    if not live and not vod and not series:
        live = vod = series = True

    if q:
        if regex:
            # Limit regex length to prevent ReDoS
            if len(q) > 100:
                raise HTTPException(400, "Expression régulière trop longue")
            try:
                pattern = re.compile(q, re.IGNORECASE)

                def match_fn(name: str) -> bool:
                    try:
                        # Timeout via match limit - search only first 1000 chars
                        return pattern.search(name[:1000]) is not None
                    except Exception:
                        return False
            except re.error:

                def match_fn(name: str) -> bool:
                    return False
        else:
            q_lower = q.lower()

            def match_fn(name: str) -> bool:
                return q_lower in name.lower()

        # Load live data (run in thread to avoid blocking)
        if live:
            if "live_streams" not in get_cache():
                cats, streams, epg_urls = await asyncio.to_thread(load_all_live_data)
                with get_cache_lock():
                    get_cache()["live_categories"] = cats
                    get_cache()["live_streams"] = streams
                    get_cache()["epg_urls"] = epg_urls
            matched = sorted(
                [s for s in get_cache()["live_streams"] if match_fn(s.get("name") or "")],
                key=lambda x: x.get("name", "").lower(),
            )
            results["live"] = matched[:limit] if limit else matched

        # Load VOD data (run in thread to avoid blocking)
        if vod:
            if "vod_streams" not in get_cache():
                vod_cats, vod_streams = await asyncio.to_thread(load_vod_data)
                with get_cache_lock():
                    get_cache()["vod_categories"] = vod_cats
                    get_cache()["vod_streams"] = vod_streams
            matched = [s for s in get_cache()["vod_streams"] if match_fn(s.get("name") or "")]
            cat_names = catalog_lang.category_names(get_cache().get("vod_categories", []))
            lang_matched += matched
            lang_names.update(cat_names)
            matched = sorted(
                catalog_lang.filter_by_language(matched, lang, cat_names),
                key=lambda x: x.get("name", "").lower(),
            )
            results["vod"] = matched[:limit] if limit else matched

        # Load series data (run in thread to avoid blocking)
        if series:
            if "series" not in get_cache():
                series_cats, series_list = await asyncio.to_thread(load_series_data)
                with get_cache_lock():
                    get_cache()["series_categories"] = series_cats
                    get_cache()["series"] = series_list
            matched = [s for s in get_cache()["series"] if match_fn(s.get("name") or "")]
            cat_names = catalog_lang.category_names(get_cache().get("series_categories", []))
            lang_matched += matched
            lang_names.update(cat_names)
            matched = sorted(
                catalog_lang.filter_by_language(matched, lang, cat_names),
                key=lambda x: x.get("name", "").lower(),
            )
            results["series"] = matched[:limit] if limit else matched

    username = user.get("sub", "")
    user_settings = load_user_settings(username)

    # Filter results based on user access
    user_limits = auth.get_user_limits(username)
    unavailable_groups = set(user_limits.get("unavailable_groups", []))

    # Filter live results by category access
    results["live"] = [
        s
        for s in results["live"]
        if not any(
            f"cat:{cat_id}" in unavailable_groups for cat_id in (s.get("category_ids") or [])
        )
    ]
    # Filter movie results by source access
    results["vod"] = [
        s for s in results["vod"] if f"movies:{s.get('source_id', '')}" not in unavailable_groups
    ]
    # Filter series results by source access
    results["series"] = [
        s for s in results["series"] if f"series:{s.get('source_id', '')}" not in unavailable_groups
    ]

    # Apply user's category filters from settings
    guide_filter = user_settings.get("guide_filter", [])
    if guide_filter:
        guide_filter_set = set(str(c) for c in guide_filter)
        results["live"] = [
            s
            for s in results["live"]
            if any(str(cat_id) in guide_filter_set for cat_id in (s.get("category_ids") or []))
        ]

    vod_filter = user_settings.get("vod_filter", [])
    if vod_filter:
        vod_filter_set = set(str(c) for c in vod_filter)
        results["vod"] = [s for s in results["vod"] if str(s.get("category_id")) in vod_filter_set]

    series_filter = user_settings.get("series_filter", [])
    if series_filter:
        series_filter_set = set(str(c) for c in series_filter)
        results["series"] = [
            s for s in results["series"] if str(s.get("category_id")) in series_filter_set
        ]

    if lang_matched:
        lang_counts = catalog_lang.option_counts(lang_matched, lang_names)

    content_access = _get_content_access(username)
    return TEMPLATES.TemplateResponse(
        request,
        "search.html",
        {
            "query": q,
            "results": results,
            "regex": regex,
            "search_live": live,
            "search_vod": vod and content_access["movies"],
            "search_series": series and content_access["series"],
            "limit": limit,
            "current_lang": lang,
            "lang_options": _lang_options(lang_counts, lang),
            "favorites": user_settings.get("favorites", {"series": {}, "movies": {}}),
            "content_access": content_access,
        },
    )


@app.get("/stream/{stream_type}/{stream_id}")
async def stream_redirect(
    stream_type: str,
    stream_id: int,
    _user: Annotated[dict, Depends(require_auth)],
    ext: str = "",
):
    xtream = get_first_xtream_client()
    if not xtream:
        raise HTTPException(404, "Aucune source Xtream configurée")
    url = xtream.build_stream_url(stream_type, stream_id, ext)
    return RedirectResponse(url, status_code=302)


@app.get("/playlist.xspf")
async def playlist_xspf(
    _user: Annotated[dict, Depends(require_auth)],
    url: str,
):
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<playlist xmlns="http://xspf.org/ns/0/" version="1">
  <trackList><track><location>{xml_escape(url)}</location></track></trackList>
</playlist>"""
    return Response(
        content=content,
        media_type="application/xspf+xml",
        headers={"Content-Disposition": "attachment; filename=stream.xspf"},
    )


# =============================================================================
# Transcoding routes (logic in ffmpeg_session.py)
# =============================================================================


@app.get("/transcode/start")
async def transcode_start(
    request: Request,
    user: Annotated[dict, Depends(require_auth)],
    url: str,
    content_type: str = "live",  # "movie", "series", or "live"
    series_id: int | None = None,
    episode_id: int | None = None,
    series_name: str = "",
    deinterlace_fallback: str = "1",  # "1" or "0"
    source_id: str = "",
    bandwidth_saver: bool = False,
    fast_start: bool = False,
):
    """Start a transcode session, return session ID."""
    # Local inputs are only allowed for finished downloads (never arbitrary files)
    if (url.startswith(("/", "file:", ".", "~")) or "://" not in url) and not (
        downloads.get_manager().is_inside(url)
    ):
        raise HTTPException(400, "Local input must be inside the download folder")
    deinterlace_fb = deinterlace_fallback == "1"
    username = user.get("sub", "")

    # Get user limits for this source
    user_limits = auth.get_user_limits(username)
    max_streams_per_source = user_limits.get("max_streams_per_source", {})
    user_max_streams = max_streams_per_source.get(source_id, 0) if source_id else 0

    # Get source max_streams (global limit for this source)
    source_max_streams = 0
    if source_id:
        settings = load_server_settings()
        sources = settings.get("sources", [])
        source = next((s for s in sources if s.get("id") == source_id), None)
        if source:
            source_max_streams = source.get("max_streams", 0)

    result = await ffmpeg_session.start_transcode(
        url,
        content_type,
        series_id,
        episode_id,
        series_name,
        deinterlace_fb,
        username,
        source_id,
        user_max_streams,
        source_max_streams,
        bandwidth_saver=bandwidth_saver,
        fast_start=fast_start,
        is_disconnected=request.is_disconnected,
    )
    if fast_start and await request.is_disconnected():
        ffmpeg_session.stop_session(result["session_id"], force=True)
        raise HTTPException(499, "Playback request disconnected")
    return result


@app.post("/transcode/{session_id}/health")
async def transcode_health(
    session_id: str,
    health: PlaybackHealth,
    user: Annotated[dict, Depends(require_auth)],
):
    return ffmpeg_session.report_playback_health(session_id, user.get("sub", ""), health)


@app.get("/transcode/seek/{session_id}")
async def transcode_seek(
    session_id: str,
    time: float,
    _user: Annotated[dict, Depends(require_auth)],
):
    """Seek VOD transcode to a new position."""
    return await ffmpeg_session.seek_transcode(session_id, time)


@app.get("/transcode/progress/{session_id}")
async def transcode_progress(
    session_id: str,
    _user: Annotated[dict, Depends(require_auth)],
):
    """Get transcode progress (segment count, duration)."""
    progress = ffmpeg_session.get_session_progress(session_id)
    if not progress:
        raise HTTPException(404, "Session not found")
    return progress


@app.get("/transcode/{session_id}/{filename}")
async def transcode_file(
    request: Request,
    session_id: str,
    filename: str,
):
    """Serve HLS playlist or segments (no auth - session IDs are unguessable)."""
    # Prevent path traversal
    safe_filename = pathlib.Path(filename).name
    if safe_filename != filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")

    session = ffmpeg_session.get_session(session_id)
    if not session:
        log.debug(f"[CAST] 404 session not found: {session_id}")
        raise HTTPException(404, "Transcode session not found")

    # Multi-audio VOD: stream.m3u8 is served as an HLS master with one EXT-X-MEDIA
    # per audio track; the real (video-only) media playlist is exposed as stream_video.m3u8.
    audio_tracks = session.get("audio_tracks") or []
    if audio_tracks and safe_filename == "stream.m3u8":
        if not (pathlib.Path(session["dir"]) / "stream.m3u8").exists():
            raise HTTPException(404, "File not found")
        return Response(
            content=ffmpeg_command.build_audio_master_playlist(
                [ffmpeg_command.audio_stream_from_dict(t) for t in audio_tracks]
            ),
            media_type="application/vnd.apple.mpegurl",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Access-Control-Allow-Origin": "*",
            },
        )
    if audio_tracks and safe_filename == "stream_video.m3u8":
        safe_filename = filename = "stream.m3u8"

    file_path = pathlib.Path(session["dir"]) / safe_filename
    if not file_path.exists():
        log.debug(f"[CAST] 404 file not found: {file_path}")
        raise HTTPException(404, "File not found")

    # Log Chromecast requests
    ua = request.headers.get("user-agent", "")
    if "CrKey" in ua or "Chromecast" in ua.lower() or "cast" in ua.lower():
        log.debug(f"[CAST] Chromecast request: {filename} UA={ua[:80]}")

    cors = {"Access-Control-Allow-Origin": "*"}
    if filename.endswith(".m3u8"):
        content = file_path.read_text()
        if (
            filename in ("low.m3u8", "high.m3u8")
            and session.get("fast_start")
            and session.get("origin_pts") is not None
        ):
            content = dated_playlist(session["dir"], content, session["origin_pts"], session["origin_time"])
        return Response(
            content=content,
            media_type="application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", **cors},
        )
    if filename.endswith(".vtt"):
        content = file_path.read_text()
        return Response(content=content, media_type="text/vtt", headers=cors)
    return FileResponse(file_path, media_type="video/mp2t", headers=cors)


@app.get("/subs/{session_id}/{filename}")
async def subtitle_file(session_id: str, filename: str):
    """Serve VTT subtitle files (no auth - session IDs are unguessable)."""
    # Prevent path traversal
    safe_filename = pathlib.Path(filename).name
    if safe_filename != filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    if not safe_filename.endswith(".vtt"):
        raise HTTPException(400, "Only VTT files allowed")
    session = ffmpeg_session.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    file_path = pathlib.Path(session["dir"]) / safe_filename
    # Wait briefly for file, return empty VTT if not ready (client will poll again)
    for _ in range(15):  # 3 seconds
        if file_path.exists() and file_path.stat().st_size > 20:
            break
        await asyncio.sleep(0.2)
    try:
        content = file_path.read_text() if file_path.exists() else "WEBVTT\n\n"
    except (UnicodeDecodeError, OSError):
        # File may be partially written or corrupted
        content = "WEBVTT\n\n"
    return Response(
        content=content,
        media_type="text/vtt",
        headers={"Access-Control-Allow-Origin": "*"},
    )


@app.delete("/transcode/{session_id}")
async def transcode_stop(
    session_id: str,
    _user: Annotated[dict, Depends(require_auth)],
    force: bool = False,
):
    """Stop a transcode session (VOD sessions stay cached)."""
    if force:
        session = ffmpeg_session.get_session(session_id)
        if session and session.get("username") != _user.get("sub", ""):
            raise HTTPException(404, "Session not found")
    ffmpeg_session.stop_session(session_id, force=force)
    return {"status": "stopped"}


@app.post("/transcode/{session_id}/stop")
async def transcode_stop_post(
    session_id: str,
    _user: Annotated[dict, Depends(require_auth)],
    force: bool = False,
):
    """Stop a transcode session (POST for sendBeacon, VOD cached)."""
    return await transcode_stop(session_id, _user, force=force)


@app.delete("/transcode-clear")
async def transcode_clear(
    url: str,
    _user: Annotated[dict, Depends(require_auth)],
):
    """Force-delete any cached transcode session for a URL."""
    session_id = ffmpeg_session.clear_url_session(url)
    if session_id:
        ffmpeg_session.stop_session(session_id, force=True)
        log.info("Force-cleared transcode session %s for URL", session_id)
    return {"status": "cleared", "session_id": session_id}


def _build_all_groups() -> list[dict[str, str]]:
    """Build list of all available groups for user restrictions.

    Groups are:
    - Live TV categories: cat:{category_id} displayed as "{source.name}: {category.name}"
    - Movies: movies:{source_id} displayed as "{source.name}: Movies"
    - Series: series:{source_id} displayed as "{source.name}: Series"
    """
    groups = []
    sources_by_id = {s.id: s for s in get_sources()}

    # Live TV categories
    for cat in get_cache().get("live_categories", []):
        source_id = cat.get("source_id", "")
        source = sources_by_id.get(source_id)
        if source:
            groups.append(
                {
                    "id": f"cat:{cat['category_id']}",
                    "name": f"{source.name}: {cat['category_name']}",
                    "type": "live",
                }
            )

    # Movies and Series for Xtream sources
    for source in get_sources():
        if source.type == "xtream":
            groups.append(
                {
                    "id": f"movies:{source.id}",
                    "name": f"{source.name}: Movies",
                    "type": "movies",
                }
            )
            groups.append(
                {
                    "id": f"series:{source.id}",
                    "name": f"{source.name}: Series",
                    "type": "series",
                }
            )

    return groups


def _get_content_access(username: str) -> dict[str, bool]:
    """Check if user has access to movies/series from any source.

    Returns dict with 'movies' and 'series' booleans.
    If no xtream sources exist, access is granted (nothing to restrict).
    """
    user_limits = auth.get_user_limits(username)
    unavailable_groups = set(user_limits.get("unavailable_groups", []))

    has_movies = False
    has_series = False
    has_xtream_source = False

    for source in get_sources():
        if source.type == "xtream":
            has_xtream_source = True
            if f"movies:{source.id}" not in unavailable_groups:
                has_movies = True
            if f"series:{source.id}" not in unavailable_groups:
                has_series = True

    # If no xtream sources, allow access (nothing to restrict)
    if not has_xtream_source:
        return {"movies": True, "series": True}

    return {"movies": has_movies, "series": has_series}


# Register template global for content access check
TEMPLATES.env.globals["get_content_access"] = _get_content_access


def _get_content_access_from_request(request: Request) -> dict[str, bool]:
    """Get content access from request (for use in base template)."""
    token = request.cookies.get("token")
    if not token:
        return {"movies": True, "series": True}  # Not logged in, show all
    payload = verify_token(token)
    if not payload:
        return {"movies": True, "series": True}
    username = payload.get("sub", "")
    if not username:
        return {"movies": True, "series": True}
    return _get_content_access(username)


TEMPLATES.env.globals["get_content_access_from_request"] = _get_content_access_from_request


@app.get("/downloads", response_class=HTMLResponse)
async def downloads_page(request: Request, user: Annotated[dict, Depends(require_auth)]):
    """Offline downloads page (client-side driven by /api/downloads)."""
    return TEMPLATES.TemplateResponse(request, "downloads.html", {})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: Annotated[dict, Depends(require_auth)]):
    username = user.get("sub", "")
    is_admin = auth.is_admin(username)
    server_settings = load_server_settings()
    user_settings = load_user_settings(username)
    # Load categories (from file cache or trigger background load)
    if "live_categories" not in get_cache():
        cached = await asyncio.to_thread(load_file_cache, "live_data")
        if cached:
            data, _ = cached
            with get_cache_lock():
                get_cache()["live_categories"] = data["cats"]
                get_cache()["live_streams"] = data["streams"]
                get_cache()["epg_urls"] = parse_epg_urls(data.get("epg_urls", []))
        else:
            # No cache - start background load
            _start_guide_background_load()

    # Load VOD categories if not cached
    if "vod_categories" not in get_cache():
        cached = await asyncio.to_thread(load_file_cache, "vod_data")
        if cached:
            data, _ = cached
            with get_cache_lock():
                get_cache()["vod_categories"] = data["cats"]
                get_cache()["vod_streams"] = data["streams"]

    # Load series categories if not cached
    if "series_categories" not in get_cache():
        cached = await asyncio.to_thread(load_file_cache, "series_data")
        if cached:
            data, _ = cached
            with get_cache_lock():
                get_cache()["series_categories"] = data["cats"]
                get_cache()["series"] = data["series"]

    # Build source_id -> source_name mapping
    source_names = {s["id"]: s["name"] for s in server_settings.get("sources", [])}

    # Filter categories based on user's unavailable groups
    user_limits = auth.get_user_limits(username)
    unavailable_groups = set(user_limits.get("unavailable_groups", []))
    all_live_cats = get_cache().get("live_categories", [])
    live_categories = [
        cat for cat in all_live_cats if f"cat:{cat['category_id']}" not in unavailable_groups
    ]
    vod_categories = get_cache().get("vod_categories", [])
    series_categories = get_cache().get("series_categories", [])

    return TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        {
            # Server settings
            "sources": server_settings.get("sources", []),
            "transcode_mode": server_settings.get("transcode_mode", "auto"),
            "transcode_hw": server_settings.get("transcode_hw", "nvidia"),
            "max_resolution": server_settings.get("max_resolution", "1080p"),
            "quality": server_settings.get("quality", "high"),
            "vod_transcode_cache_mins": server_settings.get("vod_transcode_cache_mins", 60),
            "live_transcode_cache_secs": server_settings.get("live_transcode_cache_secs", 60),
            "live_dvr_mins": server_settings.get("live_dvr_mins", 60),
            "transcode_dir": server_settings.get("transcode_dir", ""),
            "probe_live": server_settings.get("probe_live", True),
            "probe_movies": server_settings.get("probe_movies", True),
            "probe_series": server_settings.get("probe_series", False),
            "user_agent_preset": server_settings.get("user_agent_preset", "default"),
            "user_agent_custom": server_settings.get("user_agent_custom", ""),
            "available_encoders": AVAILABLE_ENCODERS,
            "sr_available": is_sr_available(),
            "sr_models": get_sr_models(),
            "sr_model": server_settings.get("sr_model", ""),
            "tmdb_api_key": server_settings.get("tmdb_api_key", ""),
            "all_users": auth.get_users_with_admin(),
            "all_groups": _build_all_groups(),
            "current_user": username,
            "is_admin": is_admin,
            # User settings
            "captions_enabled": user_settings.get("captions_enabled", False),
            "virtual_scroll": user_settings.get("virtual_scroll", True),
            "live_categories": live_categories,
            "vod_categories": vod_categories,
            "series_categories": series_categories,
            "source_names": source_names,
            "selected_cats": user_settings.get("guide_filter", []),
            "selected_vod_cats": user_settings.get("vod_filter", []),
            "selected_series_cats": user_settings.get("series_filter", []),
            "cc_lang": user_settings.get("cc_lang", ""),
            "cc_style": user_settings.get("cc_style", {}),
            "cast_host": cast_host_for(request, user_settings),
            "content_access": _get_content_access(username),
        },
    )


@app.post("/settings/guide-filter")
async def settings_guide_filter(
    request: Request,
    user: Annotated[dict, Depends(require_auth)],
):
    username = user.get("sub", "")
    data = await request.json()
    cats = data.get("cats", [])
    if not isinstance(cats, list) or len(cats) > _MAX_FILTER_CATEGORIES:
        raise HTTPException(400, "Invalid filter list")
    user_settings = load_user_settings(username)
    user_settings["guide_filter"] = cats
    save_user_settings(username, user_settings)
    return {"status": "ok"}


@app.post("/settings/vod-filter")
async def settings_vod_filter(
    request: Request,
    user: Annotated[dict, Depends(require_auth)],
):
    username = user.get("sub", "")
    data = await request.json()
    cats = data.get("cats", [])
    if not isinstance(cats, list) or len(cats) > _MAX_FILTER_CATEGORIES:
        raise HTTPException(400, "Invalid filter list")
    user_settings = load_user_settings(username)
    user_settings["vod_filter"] = cats
    save_user_settings(username, user_settings)
    return {"status": "ok"}


@app.post("/settings/series-filter")
async def settings_series_filter(
    request: Request,
    user: Annotated[dict, Depends(require_auth)],
):
    username = user.get("sub", "")
    data = await request.json()
    cats = data.get("cats", [])
    if not isinstance(cats, list) or len(cats) > _MAX_FILTER_CATEGORIES:
        raise HTTPException(400, "Invalid filter list")
    user_settings = load_user_settings(username)
    user_settings["series_filter"] = cats
    save_user_settings(username, user_settings)
    return {"status": "ok"}


@app.post("/settings/add")
async def settings_add_source(
    _user: Annotated[dict, Depends(require_admin)],
    name: Annotated[str, Form()],
    source_type: Annotated[str, Form()],
    url: Annotated[str, Form()],
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    epg_timeout: Annotated[int, Form()] = 120,
    epg_schedule: Annotated[str, Form()] = "",
    epg_enabled: Annotated[str, Form()] = "",  # Checkbox: "on" if checked
    deinterlace_fallback: Annotated[str, Form()] = "",  # Checkbox: "on" if checked
    max_streams: Annotated[int, Form()] = 0,
):
    # Validate inputs
    if not name or not name.strip():
        raise HTTPException(400, "Le nom est obligatoire")
    if source_type not in ("xtream", "m3u", "epg"):
        raise HTTPException(400, "Type de source invalide")
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.scheme not in ("http", "https"):
        raise HTTPException(400, "L'URL doit utiliser http ou https")
    if len(name) > 200:
        raise HTTPException(400, "Nom trop long")

    # Parse schedule times
    schedule_list = []
    for t in epg_schedule.split(","):
        t = t.strip()
        if t and re.match(r"^\d{1,2}:\d{2}$", t):
            schedule_list.append(t.zfill(5))

    settings = load_server_settings()
    sources = settings.get("sources", [])
    source_id = f"src_{int(time.time())}_{len(sources)}"
    sources.append(
        {
            "id": source_id,
            "name": name,
            "type": source_type,
            "url": url.rstrip("/"),
            "username": username,
            "password": password,
            "epg_timeout": max(1, min(3600, epg_timeout)),
            "epg_schedule": schedule_list,
            "epg_enabled": epg_enabled == "on" or source_type == "epg",
            "deinterlace_fallback": deinterlace_fallback == "on",
            "max_streams": max(0, max_streams),
        }
    )
    settings["sources"] = sources
    save_server_settings(settings)
    clear_all_caches()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/edit/{source_id}")
async def settings_edit_source(
    source_id: str,
    _user: Annotated[dict, Depends(require_admin)],
    name: Annotated[str, Form()],
    source_type: Annotated[str, Form()],
    url: Annotated[str, Form()],
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    epg_timeout: Annotated[int, Form()] = 120,
    epg_schedule: Annotated[str, Form()] = "",
    epg_enabled: Annotated[str, Form()] = "",  # Checkbox: "on" if checked
    epg_url: Annotated[str, Form()] = "",
    deinterlace_fallback: Annotated[str, Form()] = "",  # Checkbox: "on" if checked
    max_streams: Annotated[int, Form()] = 0,
):
    # Validate inputs
    if not name or not name.strip():
        raise HTTPException(400, "Le nom est obligatoire")
    if source_type not in ("xtream", "m3u", "epg"):
        raise HTTPException(400, "Type de source invalide")
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.scheme not in ("http", "https"):
        raise HTTPException(400, "L'URL doit utiliser http ou https")
    if len(name) > 200:
        raise HTTPException(400, "Nom trop long")

    # Parse schedule times (comma-separated HH:MM)
    schedule_list = []
    for t in epg_schedule.split(","):
        t = t.strip()
        if t and re.match(r"^\d{1,2}:\d{2}$", t):
            schedule_list.append(t.zfill(5))  # Normalize to HH:MM

    settings = load_server_settings()
    for s in settings.get("sources", []):
        if s["id"] == source_id:
            s["name"] = name
            s["type"] = source_type
            s["url"] = url.rstrip("/")
            s["username"] = username
            s["password"] = password
            s["epg_timeout"] = max(1, min(3600, epg_timeout))
            s["epg_schedule"] = schedule_list
            s["epg_enabled"] = epg_enabled == "on" or source_type == "epg"
            s["epg_url"] = epg_url.strip()
            s["deinterlace_fallback"] = deinterlace_fallback == "on"
            s["max_streams"] = max(0, max_streams)
            break
    save_server_settings(settings)
    clear_all_caches()
    return {"ok": True}


@app.post("/settings/delete/{source_id}")
async def settings_delete_source(
    source_id: str,
    _user: Annotated[dict, Depends(require_admin)],
):
    settings = load_server_settings()
    settings["sources"] = [s for s in settings.get("sources", []) if s["id"] != source_id]
    save_server_settings(settings)
    # Clear all caches including EPG data for this source
    epg.clear_source(source_id)
    clear_all_file_caches()
    return RedirectResponse("/settings", status_code=303)


@app.get("/guide/refresh")
async def guide_refresh(_user: Annotated[dict, Depends(require_auth)]):
    """Refresh guide data in background (stale-while-revalidate)."""

    def refresh_live():
        try:
            log.info("Live refresh: fetching channels")
            cats, streams, epg_urls = load_all_live_data()
            with get_cache_lock():
                get_cache()["live_categories"] = cats
                get_cache()["live_streams"] = streams
                get_cache()["epg_urls"] = epg_urls
            save_file_cache("live_data", {"cats": cats, "streams": streams, "epg_urls": epg_urls})
            log.info("Live refresh: complete (%d categories, %d streams)", len(cats), len(streams))
        except Exception as e:
            log.error("Live refresh failed: %s", e)
        finally:
            get_refresh_in_progress().discard("live_refresh")

    def refresh_epg():
        try:
            epg_urls = get_cache().get("epg_urls", [])
            if epg_urls:
                log.info("EPG refresh: fetching %d sources", len(epg_urls))
                epg.clear()
                count = _fetch_all_epg(epg_urls)
                with get_cache_lock():
                    get_cache().pop("epg_error", None)
                log.info("EPG refresh: complete (%d programs)", count)
            else:
                log.warning("EPG refresh: no EPG URLs available")
        except Exception as e:
            log.error("EPG refresh failed: %s", e)
            with get_cache_lock():
                get_cache()["epg_error"] = str(e)
        finally:
            get_refresh_in_progress().discard("epg_refresh")

    # Set flags before starting threads to avoid race with status polling
    if "live_refresh" not in get_refresh_in_progress():
        get_refresh_in_progress().add("live_refresh")
        threading.Thread(target=refresh_live, daemon=True).start()
    if "epg_refresh" not in get_refresh_in_progress():
        get_refresh_in_progress().add("epg_refresh")
        threading.Thread(target=refresh_epg, daemon=True).start()
    return RedirectResponse("/guide?refreshing=1", status_code=303)


@app.get("/guide/refresh-status")
async def guide_refresh_status(_user: Annotated[dict, Depends(require_auth)]):
    """Return refresh status for polling."""
    return {
        "live": "live_refresh" in get_refresh_in_progress(),
        "epg": "epg_refresh" in get_refresh_in_progress(),
    }


@app.post("/settings/refresh/{source_id}/{refresh_type}")
async def settings_refresh_source(
    source_id: str,
    refresh_type: str,
    _user: Annotated[dict, Depends(require_admin)],
):
    """Refresh a specific data type for a single source."""
    sources = get_sources()
    source = next((s for s in sources if s.id == source_id), None)
    if not source:
        return {"error": "Source not found"}

    key = f"{source_id}_{refresh_type}"
    if key in get_refresh_in_progress():
        return {"status": "already_running"}

    get_refresh_in_progress().add(key)

    def do_refresh():
        try:
            if refresh_type == "live":
                log.info("Refreshing live data for source: %s", source.name)
                cats, streams, epg_url, timeout = fetch_source_live_data(source)
                # Update cache by replacing this source's data
                with get_cache_lock():
                    existing_cats = [
                        c
                        for c in get_cache().get("live_categories", [])
                        if c.get("source_id") != source_id
                    ]
                    existing_streams = [
                        s
                        for s in get_cache().get("live_streams", [])
                        if s.get("source_id") != source_id
                    ]
                    existing_epg = [e for e in get_cache().get("epg_urls", []) if e[2] != source_id]
                    new_cats = existing_cats + cats
                    new_streams = existing_streams + streams
                    new_epg = existing_epg + ([(epg_url, timeout, source_id)] if epg_url else [])
                    get_cache()["live_categories"] = new_cats
                    get_cache()["live_streams"] = new_streams
                    get_cache()["epg_urls"] = new_epg
                # Save to file cache
                save_file_cache(
                    "live_data",
                    {"cats": new_cats, "streams": new_streams, "epg_urls": new_epg},
                )
                log.info(
                    "Live refresh complete for %s: %d cats, %d streams",
                    source.name,
                    len(cats),
                    len(streams),
                )

            elif refresh_type == "epg":
                log.info(
                    "Refreshing EPG for source: %s (timeout=%ds)", source.name, source.epg_timeout
                )
                epg_url = source.epg_url or (source.url if source.type == "epg" else "")
                if epg_url:
                    epg.clear_source(source_id)
                    count = _fetch_all_epg([(epg_url, source.epg_timeout, source_id)])
                    log.info("EPG refresh complete for %s: %d programs", source.name, count)
                else:
                    log.warning("No EPG URL for source: %s", source.name)

            elif refresh_type == "vod" and source.type == "xtream":
                log.info("Refreshing VOD for source: %s", source.name)
                new_cats, new_streams = fetch_source_vod_data(source)
                # Merge with existing data from other sources
                existing_cats, existing_streams = load_vod_data()
                # Remove old data from this source, keep others
                merged_cats = [c for c in existing_cats if c.get("source_id") != source_id]
                merged_streams = [s for s in existing_streams if s.get("source_id") != source_id]
                # Add new data from this source
                merged_cats.extend(new_cats)
                merged_streams.extend(new_streams)
                with get_cache_lock():
                    get_cache().pop("vod_categories", None)
                    get_cache().pop("vod_streams", None)
                for f in CACHE_DIR.glob("vod_data*.json"):
                    f.unlink(missing_ok=True)
                save_file_cache("vod_data", {"cats": merged_cats, "streams": merged_streams})
                log.info(
                    "VOD refresh complete for %s: %d cats, %d streams (total: %d)",
                    source.name,
                    len(new_cats),
                    len(new_streams),
                    len(merged_streams),
                )

            elif refresh_type == "m3u" and source.type == "m3u":
                log.info("Refreshing M3U playlist for source: %s", source.name)
                cats, streams, detected_epg_url = fetch_m3u(source.url, source.id)
                update_source_epg_url(source_id, detected_epg_url)
                with get_cache_lock():
                    existing_cats = [
                        c
                        for c in get_cache().get("live_categories", [])
                        if c.get("source_id") != source_id
                    ]
                    existing_streams = [
                        s
                        for s in get_cache().get("live_streams", [])
                        if s.get("source_id") != source_id
                    ]
                    new_cats = existing_cats + cats
                    new_streams = existing_streams + streams
                    get_cache()["live_categories"] = new_cats
                    get_cache()["live_streams"] = new_streams
                    epg_urls = get_cache().get("epg_urls", [])
                save_file_cache(
                    "live_data",
                    {
                        "cats": new_cats,
                        "streams": new_streams,
                        "epg_urls": epg_urls,
                    },
                )
                log.info(
                    "M3U refresh complete for %s: %d cats, %d streams",
                    source.name,
                    len(cats),
                    len(streams),
                )

        except Exception as e:
            log.error("Source refresh failed (%s/%s): %s", source.name, refresh_type, e)
        finally:
            get_refresh_in_progress().discard(key)

    threading.Thread(target=do_refresh, daemon=True).start()
    return {"status": "started", "key": key}


@app.get("/settings/refresh-status")
async def settings_refresh_status(_user: Annotated[dict, Depends(require_auth)]):
    """Return per-source refresh status."""
    statuses: dict[str, Any] = {}
    for key in list(get_refresh_in_progress()):
        if "_" in key:
            # Format: source_id_type (e.g., "ota_epg" or "src_123_epg")
            parts = key.rsplit("_", 1)
            if len(parts) == 2:
                source_id, rtype = parts
                statuses.setdefault(source_id, {})[rtype] = True
    # Report global guide_load as affecting all sources
    if "guide_load" in get_refresh_in_progress():
        statuses["_global"] = {"live": True, "epg": True}
    return statuses


@app.post("/settings/captions")
async def settings_captions(
    user: Annotated[dict, Depends(require_auth)],
    enabled: Annotated[str, Form()] = "",
):
    username = user.get("sub", "")
    user_settings = load_user_settings(username)
    user_settings["captions_enabled"] = enabled == "on"
    save_user_settings(username, user_settings)
    return {"ok": True}


@app.post("/api/cast-log")
async def cast_log_endpoint(request: Request):
    """Log cast events from client (debug mode only)."""
    if log.isEnabledFor(logging.DEBUG):
        body = await request.body()
        # Sanitize: limit length, single line, printable chars only
        msg = body.decode("utf-8", errors="replace")[:2048]
        msg = "".join(c if c.isprintable() and c != "\n" else "?" for c in msg)
        log.debug(f"[CAST] {msg}")
    return {"ok": True}


@app.get("/api/user-prefs")
async def get_user_prefs(user: Annotated[dict, Depends(require_auth)]):
    """Get user preferences (favorites, cc_lang, cc_style, cast_host, virtual_scroll)."""
    username = user.get("sub", "")
    settings = load_user_settings(username)
    return {
        "favorites": settings.get("favorites", {}),
        "cc_lang": settings.get("cc_lang", ""),
        "cc_style": settings.get("cc_style", {}),
        "cast_host": settings.get("cast_host", ""),
        "virtual_scroll": settings.get("virtual_scroll", True),
    }


@app.post("/api/user-prefs")
async def save_user_prefs(
    request: Request,
    user: Annotated[dict, Depends(require_auth)],
):
    """Save user preferences (partial update)."""
    username = user.get("sub", "")
    body = await request.body()
    if len(body) > 64 * 1024:  # 64KB limit
        raise HTTPException(400, "Request too large")
    data = json.loads(body)
    settings = load_user_settings(username)
    for key in (
        "favorites",
        "cc_lang",
        "cc_style",
        "cast_host",
        "virtual_scroll",
        "guide_selected_cats",
    ):
        if key in data:
            settings[key] = data[key]
    save_user_settings(username, settings)
    return {"ok": True}


def _fetch_logo(url: str, timeout: int = 3) -> tuple[bytes, str]:
    """Fetch logo synchronously. Returns (data, content_type)."""
    from util import safe_urlopen

    with safe_urlopen(url, timeout=timeout) as resp:
        content_type = resp.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            raise ValueError("URL is not an image")
        data = resp.read(LOGO_MAX_SIZE)
        if len(data) >= LOGO_MAX_SIZE:
            raise ValueError("Image too large")
    return data, content_type


@app.get("/api/logo")
async def get_logo(
    url: str,
    _user: Annotated[dict, Depends(require_auth)],
    source: str = "default",
):
    """Proxy and cache external logos to avoid mixed-content issues."""
    if not url:
        raise HTTPException(400, "Missing url parameter")
    # Check cache first
    cached = get_cached_logo(source, url)
    if cached:
        return FileResponse(cached, headers={"Cache-Control": f"max-age={LOGO_BROWSER_TTL}"})
    # Validate URL scheme
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "Invalid URL scheme")
    # Fetch the logo (in thread to avoid blocking)
    try:
        data, content_type = await asyncio.to_thread(_fetch_logo, url)
        path = save_logo(source, url, data, content_type)
        return FileResponse(path, headers={"Cache-Control": f"max-age={LOGO_BROWSER_TTL}"})
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except urllib.error.URLError as e:
        log.debug("Logo fetch failed for %s: %s", url, e)
        raise HTTPException(502, "Failed to fetch logo") from None
    except Exception as e:
        log.debug("Logo fetch error for %s: %s", url, e)
        raise HTTPException(500, "Logo fetch error") from None


@app.post("/settings/transcode")
async def settings_transcode(
    _user: Annotated[dict, Depends(require_admin)],
    transcode_mode: Annotated[str, Form()],
    transcode_hw: Annotated[str, Form()],
    max_resolution: Annotated[str, Form()] = "1080p",
    quality: Annotated[str, Form()] = "high",
    vod_transcode_cache_mins: Annotated[int, Form()] = 60,
    live_transcode_cache_secs: Annotated[int, Form()] = 0,
    live_dvr_mins: Annotated[int, Form()] = 60,
    transcode_dir: Annotated[str, Form()] = "",
    probe_live: Annotated[str | None, Form()] = None,
    probe_movies: Annotated[str | None, Form()] = None,
    probe_series: Annotated[str | None, Form()] = None,
    sr_model: Annotated[str, Form()] = "",
):
    settings = load_server_settings()
    settings["transcode_mode"] = transcode_mode
    # Validate sr_model against available models
    available_models = get_sr_models()
    settings["sr_model"] = sr_model if sr_model in available_models else ""
    settings["transcode_hw"] = transcode_hw
    settings["max_resolution"] = max_resolution
    settings["quality"] = quality if quality in ("high", "medium", "low") else "high"
    settings["vod_transcode_cache_mins"] = max(0, vod_transcode_cache_mins)
    settings["live_transcode_cache_secs"] = max(0, live_transcode_cache_secs)
    settings["live_dvr_mins"] = max(0, live_dvr_mins)
    if transcode_dir:
        settings["transcode_dir"] = transcode_dir
    elif "transcode_dir" in settings:
        del settings["transcode_dir"]  # Use default
    settings["probe_live"] = probe_live == "on"
    settings["probe_movies"] = probe_movies == "on"
    settings["probe_series"] = probe_series == "on"
    save_server_settings(settings)
    return {"ok": True}


@app.post("/settings/tmdb")
async def settings_tmdb(
    _user: Annotated[dict, Depends(require_admin)],
    tmdb_api_key: Annotated[str, Form()] = "",
):
    """Store the TMDB API key (or v4 read token) used for original languages."""
    settings = load_server_settings()
    key = tmdb_api_key.strip()
    if key:
        settings["tmdb_api_key"] = key
    else:
        settings.pop("tmdb_api_key", None)
    save_server_settings(settings)
    return {"ok": True}


@app.post("/settings/refresh-encoders")
async def settings_refresh_encoders(
    _user: Annotated[dict, Depends(require_admin)],
):
    """Re-detect available hardware encoders."""
    encoders = refresh_encoders()
    return {"ok": True, "encoders": encoders}


@app.post("/settings/user-agent")
async def settings_user_agent(
    _user: Annotated[dict, Depends(require_admin)],
    preset: Annotated[str, Form()],
    custom: Annotated[str, Form()] = "",
):
    valid_presets = {"default", "vlc", "chrome", "tivimate", "custom"}
    if preset not in valid_presets:
        preset = "default"
    settings = load_server_settings()
    settings["user_agent_preset"] = preset
    settings["user_agent_custom"] = custom
    save_server_settings(settings)
    return {"ok": True}


def _enrich_probe_cache_stats(stats: list[dict], xtream: Any) -> list[dict]:
    """Enrich probe cache stats with series/episode names (blocking)."""
    for entry in stats:
        series: dict | None = None
        if not entry.get("name") or entry.get("episodes"):
            cache_key = f"series_info_{entry['series_id']}"
            with contextlib.suppress(Exception):
                series = get_cached_info(
                    cache_key, lambda sid=entry["series_id"]: xtream.get_series_info(sid)
                )
        if series:
            if not entry.get("name") and series.get("info"):
                entry["name"] = series["info"].get("name", "")
            ep_map: dict[int, str] = {}
            for season_num, eps in (series.get("episodes") or {}).items():
                for ep in eps:
                    eid = ep.get("id")
                    if eid:
                        ep_num = ep.get("episode_num", 0)
                        title = ep.get("title", "")
                        title = re.sub(r"^S\d+E\d+\s*-\s*", "", title)
                        if " - " in title:
                            title = title.split(" - ")[-1]
                        ep_map[int(eid)] = (
                            f"S{int(season_num):02d}E{int(ep_num):02d} {title.strip()}"
                        )
            for ep in entry.get("episodes", []):
                ep_id = ep.get("episode_id")
                if ep_id in ep_map:
                    ep["name"] = ep_map[ep_id]
    return stats


@app.get("/settings/probe-cache")
async def get_probe_cache(
    _user: Annotated[dict, Depends(require_auth)],
    response: Response,
):
    """Get probe cache stats for settings UI."""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    stats = ffmpeg_command.get_series_probe_cache_stats()
    xtream = get_first_xtream_client()
    if not xtream:
        return {"series": stats}
    stats = await asyncio.to_thread(_enrich_probe_cache_stats, stats, xtream)
    return {"series": stats}


@app.post("/settings/probe-cache/clear")
async def clear_probe_cache(_user: Annotated[dict, Depends(require_admin)]):
    """Clear all probe caches."""
    count = ffmpeg_command.clear_all_probe_cache()
    return {"ok": True, "cleared": count}


@app.post("/settings/probe-cache/clear/{series_id}")
async def clear_series_probe_cache(
    series_id: int,
    _user: Annotated[dict, Depends(require_admin)],
    episode_id: int | None = None,
):
    """Clear probe cache for a specific series or episode."""
    ffmpeg_command.invalidate_series_probe_cache(series_id, episode_id)
    return {"ok": True}


@app.post("/settings/probe-cache/clear-mru/{series_id}")
async def clear_series_mru(
    series_id: int,
    _user: Annotated[dict, Depends(require_admin)],
):
    """Clear only the MRU for a series, keeping episode cache intact."""
    ffmpeg_command.clear_series_mru(series_id)
    return {"ok": True}


@app.post("/settings/data-cache/clear")
async def clear_data_cache(_user: Annotated[dict, Depends(require_admin)]):
    """Clear all data file caches (live, VOD, series) and memory cache."""
    count = clear_all_file_caches()
    return {"ok": True, "cleared": count}


@app.get("/api/settings")
async def get_settings_api(_user: Annotated[dict, Depends(require_auth)]):
    return load_server_settings()


@app.post("/api/settings")
async def update_settings_api(
    request: Request,
    _user: Annotated[dict, Depends(require_admin)],
):
    data = await request.json()
    # Whitelist allowed keys - never allow users/secret_key to be overwritten
    allowed_keys = {
        "transcode_mode",
        "transcode_hw",
        "vod_transcode_cache_mins",
        "probe_live",
        "probe_movies",
        "probe_series",
        "vod_order",
        "series_order",
    }
    settings = load_server_settings()
    for key in allowed_keys:
        if key in data:
            settings[key] = data[key]
    save_server_settings(settings)
    return {"status": "ok"}


@app.post("/api/watch-position")
async def save_watch_position_api(
    request: Request,
    user: Annotated[dict, Depends(require_auth)],
):
    """Save watch position for a stream (per-user)."""
    username = user.get("sub", "")
    data = await request.json()
    url = data.get("url", "")
    position = float(data.get("position", 0))
    duration = float(data.get("duration", 0))
    if url and position >= 0:
        save_watch_position(username, url, position, duration)
    return {"status": "ok"}


@app.get("/api/watch-position")
async def get_watch_position_api(
    user: Annotated[dict, Depends(require_auth)],
    url: str,
):
    """Get watch position for a stream (per-user)."""
    username = user.get("sub", "")
    entry = get_watch_position(username, url)
    if entry:
        return {"position": entry.get("position", 0), "duration": entry.get("duration", 0)}
    return {"position": 0, "duration": 0}


# User management endpoints
@app.post("/settings/users/delete/{username}")
async def settings_delete_user(
    username: str,
    user: Annotated[dict, Depends(require_auth)],
    password: Annotated[str, Form()] = "",
):
    """Delete a user. Self-deletion requires password. Other users require admin."""
    current_user = user.get("sub", "")
    if username == current_user:
        if not password or not auth.verify_password(username, password):
            raise HTTPException(400, "Mot de passe requis pour supprimer votre propre compte")
        auth.delete_user(username)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("token")
        return response
    # Deleting other users requires admin
    if not auth.is_admin(current_user):
        raise HTTPException(403, "Accès administrateur requis")
    if not auth.delete_user(username):
        raise HTTPException(404, "Utilisateur introuvable")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/users/add")
async def settings_add_user(
    _user: Annotated[dict, Depends(require_admin)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    admin: Annotated[str, Form()] = "",
    max_streams_per_source: Annotated[str | None, Form()] = None,
    unavailable_groups: Annotated[str | None, Form()] = None,
):
    """Add a new user."""
    username = username.strip()
    if not username or len(username) < 2:
        raise HTTPException(400, "Le nom d'utilisateur doit contenir au moins 2 caractères")
    if len(password) < 8:
        raise HTTPException(400, "Le mot de passe doit contenir au moins 8 caractères")
    if username in auth.get_all_usernames():
        raise HTTPException(400, "Cet utilisateur existe déjà")
    auth.create_user(username, password, admin=admin == "on")
    # Apply limits if provided
    parsed_max_streams = None
    if max_streams_per_source:
        with contextlib.suppress(json.JSONDecodeError):
            parsed_max_streams = json.loads(max_streams_per_source)
    parsed_unavailable = None
    if unavailable_groups:
        with contextlib.suppress(json.JSONDecodeError):
            parsed_unavailable = json.loads(unavailable_groups)
    if parsed_max_streams or parsed_unavailable:
        auth.set_user_limits(username, parsed_max_streams, parsed_unavailable)
    return {"status": "ok"}


@app.post("/settings/users/password")
async def settings_change_own_password(
    user: Annotated[dict, Depends(require_auth)],
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
):
    """Change own password. Requires current password verification."""
    username = user.get("sub", "")
    if not auth.verify_password(username, current_password):
        raise HTTPException(400, "Le mot de passe actuel est incorrect")
    if len(new_password) < 8:
        raise HTTPException(400, "Le mot de passe doit contenir au moins 8 caractères")
    if not auth.change_password(username, new_password):
        raise HTTPException(404, "Utilisateur introuvable")
    return {"status": "ok"}


@app.post("/settings/users/password/{target_user}")
async def settings_change_password(
    target_user: str,
    user: Annotated[dict, Depends(require_auth)],
    new_password: Annotated[str, Form()],
):
    """Change a user's password. Own password or admin required."""
    current_user = user.get("sub", "")
    if target_user != current_user and not auth.is_admin(current_user):
        raise HTTPException(403, "Accès administrateur requis")
    if len(new_password) < 8:
        raise HTTPException(400, "Le mot de passe doit contenir au moins 8 caractères")
    if not auth.change_password(target_user, new_password):
        raise HTTPException(404, "Utilisateur introuvable")
    return {"status": "ok"}


@app.post("/settings/users/admin/{target_user}")
async def settings_set_admin(
    target_user: str,
    _user: Annotated[dict, Depends(require_admin)],
    admin: Annotated[str, Form()] = "",
):
    """Set admin status for a user."""
    if not auth.set_admin(target_user, admin == "on"):
        raise HTTPException(404, "Utilisateur introuvable")
    return {"status": "ok"}


@app.post("/settings/users/limits/{target_user}")
async def settings_set_user_limits(
    target_user: str,
    _user: Annotated[dict, Depends(require_admin)],
    max_streams_per_source: Annotated[str | None, Form()] = None,  # JSON object string
    unavailable_groups: Annotated[str | None, Form()] = None,  # JSON array string
):
    """Set stream limits and group restrictions for a user."""
    parsed_max_streams = None
    if max_streams_per_source is not None:
        try:
            parsed_max_streams = json.loads(max_streams_per_source)
            if not isinstance(parsed_max_streams, dict):
                raise HTTPException(400, "max_streams_per_source must be a JSON object")
        except json.JSONDecodeError as err:
            raise HTTPException(400, "Invalid JSON for max_streams_per_source") from err

    parsed_unavailable = None
    if unavailable_groups is not None:
        try:
            parsed_unavailable = json.loads(unavailable_groups)
            if not isinstance(parsed_unavailable, list):
                raise HTTPException(400, "unavailable_groups must be a JSON array")
        except json.JSONDecodeError as err:
            raise HTTPException(400, "Invalid JSON for unavailable_groups") from err

    if not auth.set_user_limits(target_user, parsed_max_streams, parsed_unavailable):
        raise HTTPException(404, "Utilisateur introuvable")
    return {"status": "ok"}


if __name__ == "__main__":
    import argparse

    import uvicorn  # pyright: ignore[reportMissingImports]

    parser = argparse.ArgumentParser(description="IPTV Web App")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--https",
        nargs="?",
        const="",
        metavar="DOMAIN",
        help="Enable HTTPS (auto-detect domain, or specify one)",
    )
    parser.add_argument("--cert", help="SSL certificate file (e.g., fullchain.pem)")
    parser.add_argument("--key", help="SSL private key file (e.g., privkey.pem)")
    args = parser.parse_args()

    # LOG_LEVEL env var takes precedence, then --debug flag, then default INFO
    log_level_env = os.environ.get("LOG_LEVEL", "").upper()
    if log_level_env in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        log_level = getattr(logging, log_level_env)
    else:
        log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )

    ssl_args = {}
    if args.cert and args.key:
        ssl_args = {"ssl_certfile": args.cert, "ssl_keyfile": args.key}
    elif args.https is not None:
        live_dir = pathlib.Path("/etc/letsencrypt/live")
        if args.https:
            domain = args.https
        else:
            # Auto-detect first domain
            domains = [
                d.name for d in live_dir.iterdir() if d.is_dir() and (d / "fullchain.pem").exists()
            ]
            if not domains:
                raise SystemExit("No Let's Encrypt certs found in /etc/letsencrypt/live/")
            domain = domains[0]
        cert = live_dir / domain / "fullchain.pem"
        key = live_dir / domain / "privkey.pem"
        if not cert.exists():
            raise SystemExit(f"Cert not found: {cert}")
        log.info("Using Let's Encrypt certs for %s", domain)
        ssl_args = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}

    uv_log = "debug" if args.debug else "info"
    uvicorn.run(
        app,
        host=os.environ.get("NETV_HOST", "0.0.0.0"),
        port=args.port,
        access_log=args.debug,
        log_level=uv_log,
        log_config=None,  # preserve our basicConfig
        timeout_graceful_shutdown=5,
        proxy_headers=True,
        forwarded_allow_ips="*",
        **ssl_args,  # pyright: ignore[reportArgumentType]
    )
