"""Is the IPTV line free? Small green / orange / red dot in the sidebar.

Xtream providers report ``active_cons`` (streams open right now on the account, from ANY
device: another neTV, a TV app...) and ``max_connections`` in ``player_api.php``. Subtracting
what this server is using itself (playback sessions and running downloads on the source)
tells whether somebody else is watching.

States: ``free`` (a stream can start), ``mine`` (the line is full with our own streams),
``busy`` (full because of someone else), ``unknown`` (provider unreachable).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import json
import logging
import threading
import time
import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends

import downloads


log = logging.getLogger(__name__)

CACHE_SEC = 15.0
TIMEOUT_SEC = 5.0
USER_AGENT = "TiviMate/4.7.0"

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def _provider_counts(src: Any) -> tuple[int, int]:
    """(active_cons, max_connections) announced by the provider for this account."""
    base = src.url.rstrip("/")
    query = urllib.parse.urlencode({"username": src.username, "password": src.password})
    req = urllib.request.Request(f"{base}/player_api.php?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:  # noqa: S310 (configured source)
        info = json.loads(resp.read()).get("user_info") or {}
    return int(info.get("active_cons") or 0), int(info.get("max_connections") or 0)


def _local_streams(source_id: str) -> int:
    """Streams this server holds on the source: playback sessions + running downloads."""
    _, playing = downloads.default_source_limits(source_id)
    manager = downloads.get_manager()
    with manager._lock:
        running = list(manager._running)
    dl = sum(1 for eid in running if (e := manager.get(eid)) and e.source_id == source_id)
    return playing + dl


def classify(active: int, maximum: int, local: int) -> str:
    others = max(0, active - local)
    if maximum <= 0 or active < maximum:
        return "free"
    return "busy" if others > 0 else "mine"


def source_status(src: Any) -> dict[str, Any]:
    now = time.time()
    with _lock:
        hit = _cache.get(src.id)
    if hit and now - hit[0] < CACHE_SEC:
        return hit[1]
    local = _local_streams(src.id)
    try:
        active, maximum = _provider_counts(src)
        state = classify(active, maximum, local)
    except Exception as exc:
        log.info("Line status of %s unavailable: %s", src.name, exc)
        active, maximum, state = -1, 0, "unknown"
    result = {
        "id": src.id,
        "name": src.name,
        "state": state,
        "active": active,
        "max": maximum,
        "local": local,
    }
    with _lock:
        _cache[src.id] = (now, result)
    return result


def build_router(require_auth: Callable[..., Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/line-status", dependencies=[Depends(require_auth)])
    def line_status():
        from cache import get_sources

        return {"sources": [source_status(s) for s in get_sources() if s.type == "xtream"]}

    return router
