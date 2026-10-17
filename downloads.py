"""Server side downloads of VOD movies and series episodes for offline viewing.

neTV runs on the viewer's own machine, so a "download" is done by the server:
the provider file is streamed to ``NETV_DOWNLOAD_DIR`` (default ``~/Movies/neTV``)
and can then be played with no internet, either through the regular player
(``/play/download/{id}``, which feeds the local file to the transcode pipeline)
or raw with HTTP Range support (``/downloads/files/{id}``).

Concurrency and provider safety
-------------------------------
IPTV accounts often allow a single connection, so the rules are deliberately
conservative:

* One download at a time per source (FIFO by enqueue time).
* If the source has ``max_streams`` > 0, a download only starts while the number
  of active playback sessions on that source is below ``max_streams``. A running
  download that would exceed the limit because playback started yields: it goes
  back to ``queued`` (the ``.part`` file is kept) and resumes automatically once
  playback has ended. Playback always wins over downloading.
* With ``max_streams`` = 0 (unlimited/unknown) only the one-at-a-time rule applies.
* Nothing ever starts by itself after a restart: entries that were queued or
  downloading come back as ``paused`` and must be resumed explicitly.

Data lives in the download folder: ``<title>.<ext>`` for finished files,
``<title>.<ext>.part`` while in progress and ``.netv-downloads.json`` as index.
Provider URLs (which contain credentials) are never persisted; they are rebuilt
from the source configuration each time a download starts.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

import asyncio
import concurrent.futures
import contextlib
import errno
import http.client
import json
import logging
import mimetypes
import os
import pathlib
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from util import _SafeRedirectHandler, atomic_write_json


log = logging.getLogger(__name__)

INDEX_NAME = ".netv-downloads.json"
CHUNK_SIZE = 256 * 1024
MIN_FREE_BYTES = 1024**3  # keep 1 GB free on top of the file size
SPEED_WINDOW_SEC = 5.0
DEFAULT_USER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504}
ACTIVE = ("queued", "downloading", "paused", "done")

Status = Literal["queued", "downloading", "paused", "done", "error", "cancelled"]

mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/mp2t", ".ts")


def default_download_dir() -> pathlib.Path:
    """Folder from NETV_DOWNLOAD_DIR, default ~/Movies/neTV."""
    env = os.environ.get("NETV_DOWNLOAD_DIR", "").strip()
    return pathlib.Path(env).expanduser() if env else pathlib.Path.home() / "Movies" / "neTV"


_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def safe_name(title: str, fallback: str) -> str:
    """Filesystem safe base name: no path chars, no leading dots, bounded length."""
    name = _BAD_CHARS.sub(" ", title or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:150].strip(" .")
    return name or fallback


def safe_ext(ext: str) -> str:
    ext = re.sub(r"[^A-Za-z0-9]", "", ext or "")[:5].lower()
    return ext or "mkv"


@dataclass
class Entry:
    id: str
    kind: str  # "movie" | "episode"
    item_id: str
    source_id: str
    title: str
    filename: str
    ext: str
    status: str = "queued"
    bytes_done: int = 0
    bytes_total: int = 0
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    poster: str | None = None
    series_id: int | None = None
    queued_at: float = field(default_factory=time.time)
    # Audio handling (see remux.py): the provider file has many audio tracks, we keep one
    src_ext: str = ""  # container extension at the provider (``ext`` becomes "mp4" after remux)
    src_bytes: int = 0  # size of the provider file
    audio_pref: int = -1  # audio track (ordinal) forced for this download, -1 = automatic rule
    source_audio: list = field(default_factory=list)  # audio tracks of the provider file
    audio_kept: int = -1  # ordinal in ``source_audio`` of the track in the file
    audio_reason: str = ""
    processing: bool = False  # remux running (the original file stays playable meanwhile)


class _Stopped(Exception):
    """The run was interrupted by pause/cancel/delete/yield/shutdown."""


class _Fatal(Exception):
    """Non retryable failure, message is shown to the user."""


class _Reconnect(ConnectionError):
    """The connection got much slower than before: drop it and resume with a fresh one."""


class SlowWatch:
    """Keeps a download fast on providers that throttle each connection.

    Two mechanisms work together.

    1. Slow connection watchdog (reactive). Some providers serve a new connection
       at full speed for a while (a few hundred MB) and then throttle it to
       ~1 to 2 MB/s, while a fresh connection is fast again. We compare each
       window's rate to the best recent rate of this download and, when it stays
       far below for ``patience`` seconds, ask the caller to reconnect (the
       download resumes with a Range request, nothing is lost). Guards against
       pointless churn:
       - never reconnect while the rate is already good (``fast_enough``),
       - if a reconnect did not bring the speed back (< 1.5x the slow rate), wait
         twice as long before the next one (up to ``max_patience``), and go back
         to the base patience as soon as one helps,
       - the reference rate decays slowly, so a provider that is simply slower
         for a while stops triggering reconnects after a few minutes.

    2. Burst rotation (proactive). Measured on a real provider (XUI style "VOD
       protection"): the full speed part of a response is a fixed FRACTION of that
       response's Content-Length (exactly 15 % of the Range length, whatever the
       file, the offset, the idle time or the User-Agent), then the rate falls to
       about 1.9x the film's average bitrate. Waiting for the watchdog wastes 15 s
       at the slow rate per connection, and because each reconnect asks for the
       remaining bytes the bursts shrink by 15 % each time, so the tail of a big
       file crawled at 2 MB/s. Once the fraction is known, the connection is
       dropped right after ``fraction * margin`` of its response and a new Range
       starts (about 0.3 s per request), which keeps the whole file at full speed
       (measured 2.5x faster than the watchdog alone on 1 GB of an 8 GB film).
       The fraction is learned, not assumed: the first throttled connection tells
       it (bytes received before the rate fell, divided by the announced length),
       and every later connection that shows a fall before the planned cut
       corrects it. Nothing changes on providers that do not throttle (the
       fraction stays unknown and the connection is never rotated).

    Knobs (all optional):
    - window, patience, slow_ratio, fast_enough, max_patience, decay: watchdog
      as described above.
    - burst_fraction: known fraction of the response served fast (0.15 for the
      measured provider); 0 means learn it. The manager remembers what was learned
      per provider host and seeds new downloads with it.
    - burst_margin: cut at this share of the fraction (default 0.95) so that we
      stop just before the throttle starts.
    - min_burst: do not rotate when the planned fast part is smaller than this
      (default 4 MiB, a new request costs about 0.3 s, so tiny bursts and the tail
      of a file are simply read to the end).
    - knee_window, knee_ratio: how the end of the burst is measured, a sub-window
      of ``knee_window`` seconds is "slow" below ``knee_ratio`` of the peak of the
      connection, and two slow sub-windows in a row mark the end of the burst.
    - rotate: set False to disable the proactive part.
    """

    MIN_FRACTION = 0.02
    MAX_FRACTION = 0.6

    def __init__(
        self,
        window: float = 5.0,
        patience: float = 10.0,
        slow_ratio: float = 0.4,
        fast_enough: float = 8 * 1024 * 1024,
        max_patience: float = 300.0,
        decay: float = 0.98,
        burst_fraction: float = 0.0,
        burst_margin: float = 0.95,
        min_burst: int = 4 * 1024 * 1024,
        knee_window: float = 0.5,
        knee_ratio: float = 0.35,
        rotate: bool = True,
    ) -> None:
        self.window = window
        self.base_patience = patience
        self.patience = patience
        self.slow_ratio = slow_ratio
        self.fast_enough = fast_enough
        self.max_patience = max_patience
        self.decay = decay
        self.fraction = burst_fraction
        self.burst_margin = burst_margin
        self.min_burst = min_burst
        self.knee_window = knee_window
        self.knee_ratio = knee_ratio
        self.rotate = rotate
        self.best = 0.0  # best recent window rate, bytes/s
        self.reconnects = 0
        self.rotations = 0
        self._before: float | None = None  # slow rate that triggered the last reconnect
        self._c_start = 0  # position where the current connection began
        self._c_len = 0  # bytes announced for the current connection (0 = unknown)
        self._reset(0.0, 0)
        self._reset_knee(0.0, 0)

    def _reset(self, now: float, done: int) -> None:
        self._w_start = now
        self._w_done = done
        self._slow_since: float | None = None

    def _reset_knee(self, now: float, done: int) -> None:
        self._k_start = now
        self._k_done = done
        self._k_peak = 0.0
        self._k_good_done = done  # position at the end of the last fast sub-window
        self._k_slow_run = 0
        self._knee: int | None = None  # bytes received when the burst ended

    def _learn(self) -> None:
        """At the end of a connection: if we saw where its burst ended, keep the fraction."""
        if self._knee is not None and self._c_len > 0:
            f = self._knee / self._c_len
            if self.MIN_FRACTION <= f <= self.MAX_FRACTION:
                self.fraction = f

    def new_connection(self, now: float, done: int, length: int = 0) -> None:
        """A new response starts at ``done``; ``length`` is its announced size if known."""
        self._learn()
        self._reset(now, done)
        self._reset_knee(now, done)
        self._c_start = done
        self._c_len = length

    def _track_knee(self, now: float, done: int) -> None:
        el = now - self._k_start
        if el < self.knee_window:
            return
        rate = (done - self._k_done) / el
        self._k_start, self._k_done = now, done
        self._k_peak = max(self._k_peak, rate)
        if rate >= self.knee_ratio * self._k_peak:
            self._k_good_done = done
            self._k_slow_run = 0
            return
        self._k_slow_run += 1
        if self._k_slow_run >= 2 and self._knee is None:
            self._knee = self._k_good_done - self._c_start

    def should_rotate(self, done: int) -> bool:
        """True when the fast part of this response is spent and a new one should start."""
        if not (self.rotate and self.fraction > 0 and self._c_len > 0):
            return False
        budget = self.fraction * self.burst_margin * self._c_len
        if budget < self.min_burst:
            return False
        got = done - self._c_start
        if got < budget or got >= self._c_len:
            return False
        self.rotations += 1
        return True

    def feed(self, now: float, done: int) -> bool:
        """Call after each chunk. True when the connection should be replaced."""
        self._track_knee(now, done)
        elapsed = now - self._w_start
        if elapsed < self.window:
            return False
        rate = (done - self._w_done) / elapsed
        window_start = self._w_start
        self._w_start, self._w_done = now, done
        if self._before is not None:  # first full window after a reconnect: did it help?
            if rate > self._before * 1.5:
                self.patience = self.base_patience
            else:
                self.patience = min(self.patience * 2, self.max_patience)
            self._before = None
        self.best = max(rate, self.best * self.decay)
        if rate >= self.fast_enough or rate >= self.slow_ratio * self.best:
            self._slow_since = None
            return False
        if self._slow_since is None:
            self._slow_since = window_start
        if now - self._slow_since >= self.patience:
            self._before = rate
            self.reconnects += 1
            return True
        return False


class _Ctl:
    """Per run control block, one per download thread."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.reason = ""

    def request_stop(self, reason: str) -> None:
        self.reason = reason
        self.stop.set()

    def check(self) -> None:
        if self.stop.is_set():
            raise _Stopped


class DownloadManager:
    """Queue, worker threads and persistent index for downloads."""

    def __init__(
        self,
        directory: pathlib.Path,
        url_builder: Callable[[Entry], str],
        *,
        user_agent: Callable[[], str | None] = lambda: None,
        source_limits: Callable[[str], tuple[int, int]] = lambda _sid: (0, 0),
        min_free: int = MIN_FREE_BYTES,
        backoff: float = 2.0,
        max_retries: int = 4,
        timeout: float = 30.0,
        tick_interval: float = 2.0,
        slow_watch: Callable[[], SlowWatch] = SlowWatch,
        optimizer: Callable[[Entry, pathlib.Path, pathlib.Path], Any] | None = None,
    ) -> None:
        """``source_limits(source_id)`` returns (max_streams, active_playback_sessions)."""
        self.dir = pathlib.Path(directory).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir = self.dir.resolve()
        self.url_builder = url_builder
        self.user_agent = user_agent
        self.source_limits = source_limits
        self.min_free = min_free
        self.backoff = backoff
        self.max_retries = max_retries
        self.timeout = timeout
        self.tick_interval = tick_interval
        self._slow_watch = slow_watch
        # optimizer(entry, src, dst) writes dst (single audio MP4) and returns a remux.Plan;
        # None disables the post-download step
        self.optimizer = optimizer
        self._post = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="dl-opt")
        self._watches: dict[str, SlowWatch] = {}
        self._fractions: dict[str, float] = {}  # provider host -> learned fast fraction
        self._lock = threading.RLock()
        self._entries: dict[str, Entry] = {}
        self._running: dict[str, tuple[_Ctl, str]] = {}  # id -> (ctl, filename)
        self._samples: dict[str, deque[tuple[float, int]]] = {}
        self._tick_stop = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._closing = False
        self._opener = urllib.request.build_opener(_SafeRedirectHandler())
        self._load()

    # ------------------------------------------------------------------ paths

    def safe_path(self, filename: str) -> pathlib.Path:
        """Resolve a stored file name inside the download dir or raise ValueError."""
        if (
            not filename
            or filename in (".", "..")
            or os.sep in filename
            or "/" in filename
            or "\\" in filename
            or "\x00" in filename
        ):
            raise ValueError("Unsafe file name")
        path = (self.dir / filename).resolve()
        if path.parent != self.dir:
            raise ValueError("Unsafe file path")
        return path

    def _part(self, entry_or_name: Entry | str) -> pathlib.Path:
        name = entry_or_name if isinstance(entry_or_name, str) else entry_or_name.filename
        return self.safe_path(name + ".part")

    def _unique_filename(self, base: str, ext: str) -> str:
        taken = {e.filename for e in self._entries.values()}
        taken |= {name for _, name in self._running.values()}
        n = 1
        while True:
            name = f"{base}.{ext}" if n == 1 else f"{base} ({n}).{ext}"
            if (
                name not in taken
                and not (self.dir / name).exists()
                and not (self.dir / (name + ".part")).exists()
            ):
                return name
            n += 1

    # ------------------------------------------------------------ persistence

    def _index_path(self) -> pathlib.Path:
        return self.dir / INDEX_NAME

    def _load(self) -> None:
        path = self._index_path()
        try:
            raw = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError) as e:
            log.warning("Download index unreadable (%s), starting empty", e)
            raw = {}
        names = {f.name for f in Entry.__dataclass_fields__.values()}
        for item in raw.get("downloads", []):
            try:
                entry = Entry(**{k: v for k, v in item.items() if k in names})
                self.safe_path(entry.filename)
            except (TypeError, ValueError):
                log.warning("Dropping invalid download index entry")
                continue
            entry.processing = False  # a remux interrupted by a restart is simply redone on demand
            if entry.status in ("queued", "downloading"):
                entry.status = "paused"
            if entry.status == "done":
                try:
                    entry.bytes_done = self.safe_path(entry.filename).stat().st_size
                    entry.bytes_total = entry.bytes_done
                except OSError:
                    entry.status = "error"
                    entry.error = "File is missing"
            elif entry.status == "paused":
                try:
                    entry.bytes_done = self._part(entry).stat().st_size
                except OSError:
                    entry.bytes_done = 0
            self._entries[entry.id] = entry
        for stale in self.dir.glob("*.remux"):  # half written output of an interrupted remux
            self._unlink(stale)
        self._save()

    def _save(self) -> None:
        with self._lock:
            data = {"version": 1, "downloads": [asdict(e) for e in self._entries.values()]}
            try:
                atomic_write_json(self._index_path(), data)
            except OSError as e:
                log.error("Cannot write download index: %s", e)

    # ---------------------------------------------------------------- public

    def start(self) -> None:
        """Start the background scheduler tick (yield to playback, start queue)."""
        if self._tick_thread and self._tick_thread.is_alive():
            return
        self._tick_stop.clear()
        self._closing = False

        def loop() -> None:
            while not self._tick_stop.wait(self.tick_interval):
                try:
                    self.schedule()
                except Exception:
                    log.exception("Download scheduler error")

        self._tick_thread = threading.Thread(target=loop, daemon=True, name="download-tick")
        self._tick_thread.start()

    def shutdown(self) -> None:
        """Stop workers, keep partial files, mark running entries paused."""
        self._tick_stop.set()
        with self._lock:
            self._closing = True
            for eid, (ctl, _) in list(self._running.items()):
                ctl.request_stop("shutdown")
                entry = self._entries.get(eid)
                if entry and entry.status == "downloading":
                    entry.status = "paused"
            self._save()
        self._post.shutdown(wait=False, cancel_futures=True)

    def add(
        self,
        kind: str,
        item_id: str,
        *,
        source_id: str = "",
        title: str = "",
        ext: str = "mkv",
        poster: str | None = None,
        series_id: int | None = None,
        audio_pref: int = -1,
    ) -> Entry:
        """Queue a download, or return the existing queued/downloading/paused/done one."""
        item_id = str(item_id)
        with self._lock:
            for e in list(self._entries.values()):
                if e.kind == kind and e.item_id == item_id:
                    if e.status == "done":
                        try:
                            if self.safe_path(e.filename).is_file():
                                return e
                        except ValueError:
                            pass
                        self._remove(e)  # stale: file vanished
                    elif e.status in ACTIVE:
                        return e
                    else:  # error / cancelled: replace
                        self._remove(e)
            ext = safe_ext(ext)
            base = safe_name(title, f"{kind}-{item_id}")
            entry = Entry(
                id=uuid.uuid4().hex,
                kind=kind,
                item_id=item_id,
                source_id=source_id,
                title=title or base,
                filename=self._unique_filename(base, ext),
                ext=ext,
                poster=poster,
                series_id=series_id,
                src_ext=ext,
                audio_pref=audio_pref,
            )
            self._entries[entry.id] = entry
            self._save()
            self.schedule()
            return entry

    def get(self, download_id: str) -> Entry | None:
        return self._entries.get(download_id)

    def pause(self, download_id: str) -> Entry:
        with self._lock:
            e = self._require(download_id)
            if e.status in ("queued", "downloading"):
                run = self._running.get(e.id)
                e.status = "paused"
                if run:
                    run[0].request_stop("pause")
                self._save()
            return e

    def resume(self, download_id: str) -> Entry:
        with self._lock:
            e = self._require(download_id)
            if e.status in ("paused", "error", "cancelled"):
                e.status = "queued"
                e.error = None
                e.queued_at = time.time()
                self._save()
                self.schedule()
            return e

    def cancel(self, download_id: str) -> Entry:
        """Stop and delete the partial file. Keeps the (cancelled) entry."""
        with self._lock:
            e = self._require(download_id)
            if e.status in ("done", "cancelled"):
                return e
            e.status = "cancelled"
            e.bytes_done = 0
            e.error = None
            run = self._running.get(e.id)
            if run:
                run[0].request_stop("cancel")  # worker removes the .part
            else:
                self._unlink(self._part(e))
            self._save()
            self.schedule()
            return e

    def delete(self, download_id: str) -> None:
        with self._lock:
            e = self._require(download_id)
            self._remove(e)
            self._save()
            self.schedule()

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            entries = sorted(self._entries.values(), key=lambda e: e.created_at)
            return [self.public(e) for e in entries]

    def lookup(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        """Map "kind:id" to the most relevant entry; unknown keys are omitted."""
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for key in keys:
                kind, _, item_id = key.partition(":")
                best: Entry | None = None
                for e in self._entries.values():
                    if e.kind == kind and e.item_id == item_id:
                        if e.status in ACTIVE:
                            best = e
                            break
                        if best is None or e.created_at > best.created_at:
                            best = e
                if best:
                    total = best.bytes_total
                    out[key] = {
                        "status": best.status,
                        "download_id": best.id,
                        "bytes_done": best.bytes_done,
                        "bytes_total": total,
                        "percent": round(100 * best.bytes_done / total, 1) if total else None,
                    }
        return out

    def file_for(self, download_id: str) -> tuple[Entry, pathlib.Path]:
        e = self._require(download_id)
        if e.status != "done":
            raise HTTPException(409, "Download is not finished")
        try:
            path = self.safe_path(e.filename)
        except ValueError as exc:
            raise HTTPException(400, "Invalid file name") from exc
        if not path.is_file():
            raise HTTPException(404, "File is missing")
        return e, path

    def free_bytes(self) -> int:
        try:
            return shutil.disk_usage(self.dir).free
        except OSError:
            return 0

    def is_inside(self, path_str: str) -> bool:
        """True if path_str (plain path or file:// URL) points inside the download dir."""
        if path_str.startswith("file://"):
            path_str = urllib.parse.unquote(urllib.parse.urlparse(path_str).path)
        try:
            p = pathlib.Path(path_str).resolve()
        except (OSError, RuntimeError):
            return False
        return p.is_file() and p.parent == self.dir and not p.name.startswith(".")

    # ------------------------------------------------------- audio / remux

    def optimize(self, download_id: str) -> bool:
        """Queue the remux of a finished download (single audio MP4). False if not applicable."""
        e = self._entries.get(download_id)
        if self.optimizer is None or e is None or e.status != "done" or e.processing:
            return False
        if e.ext == "mp4" and e.audio_kept >= 0:
            return False  # already done
        e.processing = True
        self._save()
        try:
            self._post.submit(self._optimize, e.id)
        except RuntimeError:  # shutting down
            e.processing = False
            return False
        return True

    def _optimize(self, download_id: str) -> None:
        e = self._entries.get(download_id)
        if e is None or self.optimizer is None:
            return
        try:
            src = self.safe_path(e.filename)
            stem = src.name[: -len(src.suffix)] if src.suffix else src.name
            dst = self.safe_path(self._unique_filename(stem, "mp4"))
            plan = self.optimizer(e, src, dst)
        except Exception as exc:  # the original file is untouched, playback works as before
            log.info("Download %s: audio optimisation skipped (%s)", e.filename, exc)
            with self._lock:
                e.processing = False
                self._save()
            return
        with self._lock:
            if e.id not in self._entries or e.status != "done":
                self._unlink(dst)  # deleted while we worked
                return
            old = e.filename
            e.src_bytes = e.src_bytes or src.stat().st_size
            e.src_ext = e.src_ext or e.ext
            e.filename = dst.name
            e.ext = "mp4"
            e.source_audio = plan.audio
            e.audio_kept = plan.kept
            e.audio_reason = plan.reason
            e.bytes_done = e.bytes_total = dst.stat().st_size
            e.processing = False
            self._save()
        with contextlib.suppress(ValueError):
            self._unlink(self.safe_path(old))
        log.info(
            "Download %s: kept audio %s (%s), %.1f GB -> %.1f GB",
            dst.name, plan.audio[plan.kept].get("lang"), plan.reason,
            e.src_bytes / 1e9, e.bytes_done / 1e9,
        )  # fmt: skip

    def redownload_with_audio(self, download_id: str, track: int) -> Entry:
        """Delete a finished download and fetch the provider file again keeping audio ``track``."""
        e = self._require(download_id)
        if e.status != "done" or not e.source_audio:
            raise HTTPException(409, "This download has no alternative audio track")
        if not 0 <= track < len(e.source_audio):
            raise HTTPException(400, "No such audio track")
        if track == e.audio_kept:
            raise HTTPException(409, "This audio track is already the one in the file")
        if e.processing:
            raise HTTPException(409, "The file is still being prepared")
        kind, item_id, source_id = e.kind, e.item_id, e.source_id
        title, poster, series_id = e.title, e.poster, e.series_id
        ext = e.src_ext or "mkv"
        with self._lock:
            self._remove(e)
            return self.add(
                kind, item_id, source_id=source_id, title=title, ext=ext,
                poster=poster, series_id=series_id, audio_pref=track,
            )  # fmt: skip

    # -------------------------------------------------------------- internals

    def _require(self, download_id: str) -> Entry:
        e = self._entries.get(download_id)
        if not e:
            raise HTTPException(404, "Download not found")
        return e

    @staticmethod
    def _unlink(path: pathlib.Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log.warning("Cannot delete %s: %s", path.name, e)

    def _remove(self, e: Entry) -> None:
        run = self._running.get(e.id)
        e.status = "cancelled"
        if run:
            run[0].request_stop("delete")  # worker removes .part
        else:
            self._unlink(self._part(e))
        with contextlib.suppress(ValueError):
            self._unlink(self.safe_path(e.filename))
        self._entries.pop(e.id, None)
        self._samples.pop(e.id, None)

    def public(self, e: Entry) -> dict[str, Any]:
        speed = 0.0
        samples = self._samples.get(e.id)
        if e.status == "downloading" and samples:
            now = time.time()
            window = [s for s in list(samples) if now - s[0] <= SPEED_WINDOW_SEC]
            if len(window) >= 2 and window[-1][0] > window[0][0]:
                speed = (window[-1][1] - window[0][1]) / (window[-1][0] - window[0][0])
        eta = None
        if speed > 1 and e.bytes_total > e.bytes_done:
            eta = int((e.bytes_total - e.bytes_done) / speed)
        return {
            "id": e.id,
            "kind": e.kind,
            "item_id": e.item_id,
            "title": e.title,
            "filename": e.filename,
            "ext": e.ext,
            "status": e.status,
            "bytes_done": e.bytes_done,
            "bytes_total": e.bytes_total,
            "speed_bps": int(speed),
            "eta_seconds": eta,
            "error": e.error,
            "created_at": e.created_at,
            "finished_at": e.finished_at,
            "poster": e.poster,
            "processing": e.processing,
            "audio_kept": e.audio_kept,
        }

    def schedule(self) -> None:
        """Yield to playback where needed, then start eligible queued entries (FIFO)."""
        with self._lock:
            if self._closing:
                return
            # 1. downloads must give way to playback on limited sources
            for eid, (ctl, _) in list(self._running.items()):
                e = self._entries.get(eid)
                if not e or e.status != "downloading" or ctl.stop.is_set():
                    continue
                max_streams, playing = self._limits(e.source_id)
                if max_streams > 0 and playing >= max_streams:
                    log.info("Download %s yields to playback", e.filename)
                    e.status = "queued"
                    ctl.request_stop("yield")
            # 2. start at most one per source
            busy = {
                self._entries[eid].source_id
                for eid in self._running
                if eid in self._entries and self._entries[eid].status == "downloading"
            }
            queued = sorted(
                (e for e in self._entries.values() if e.status == "queued"),
                key=lambda e: e.queued_at,
            )
            for e in queued:
                if e.id in self._running or e.source_id in busy:
                    continue
                max_streams, playing = self._limits(e.source_id)
                if max_streams > 0 and playing >= max_streams:
                    continue
                e.status = "downloading"
                e.error = None
                busy.add(e.source_id)
                ctl = _Ctl()
                self._running[e.id] = (ctl, e.filename)
                self._samples[e.id] = deque(maxlen=200)
                threading.Thread(
                    target=self._worker, args=(e, ctl), daemon=True, name=f"dl-{e.id[:6]}"
                ).start()
            self._save()

    def _limits(self, source_id: str) -> tuple[int, int]:
        try:
            return self.source_limits(source_id)
        except Exception:
            return (0, 0)

    def _worker(self, e: Entry, ctl: _Ctl) -> None:
        try:
            self._download(e, ctl)
            if e.status == "done":
                self.optimize(e.id)
        except _Stopped:
            pass
        except _Fatal as exc:
            self._fail(e, str(exc))
        except Exception as exc:
            log.exception("Download %s crashed", e.filename)
            self._fail(e, f"Unexpected error: {exc}")
        finally:
            with self._lock:
                self._running.pop(e.id, None)
                if ctl.reason in ("cancel", "delete"):
                    e.bytes_done = 0
                    with contextlib.suppress(ValueError):
                        self._unlink(self._part(e.filename))
                elif e.status == "paused":
                    with contextlib.suppress(OSError, ValueError):
                        e.bytes_done = self._part(e).stat().st_size
                if e.status != "downloading":
                    self._samples.pop(e.id, None)
                self._save()
                self.schedule()

    def _fail(self, e: Entry, message: str) -> None:
        with self._lock:
            if e.status == "downloading":
                e.status = "error"
                e.error = message
        log.warning("Download %s failed: %s", e.filename, message)

    def _download(self, e: Entry, ctl: _Ctl) -> None:
        part = self._part(e)
        attempts = 0
        while True:
            ctl.check()
            before = part.stat().st_size if part.exists() else 0
            try:
                if self._attempt(e, ctl, part):
                    return
                raise ConnectionError("connection closed before the file was complete")
            except (_Stopped, _Fatal):
                raise
            except _Reconnect as exc:
                log.info("Download %s: %s", e.filename, exc)
                continue  # progress was made, resume right away with a fresh connection
            except urllib.error.HTTPError as exc:
                if exc.code == 416:
                    if self._already_complete(e, ctl, part, exc):
                        return
                    self._unlink(part)  # our offset is bogus, start again
                elif exc.code not in RETRYABLE_HTTP:
                    raise _Fatal(f"Provider answered HTTP {exc.code} {exc.reason}") from exc
                err: Exception = exc
            except (TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
                err = exc
            after = part.stat().st_size if part.exists() else 0
            attempts = 0 if after > before else attempts + 1
            if attempts > self.max_retries:
                raise _Fatal(f"Network error: {err}") from err
            delay = self.backoff * (2 ** max(attempts - 1, 0))
            log.info("Download %s: %s, retry in %.1fs", e.filename, err, delay)
            if ctl.stop.wait(delay):
                raise _Stopped

    def _attempt(self, e: Entry, ctl: _Ctl, part: pathlib.Path) -> bool:
        """One HTTP request. True if the file is complete and renamed."""
        url = self.url_builder(e)
        have = part.stat().st_size if part.exists() else 0
        headers = {
            "User-Agent": self.user_agent() or DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if have:
            headers["Range"] = f"bytes={have}-"
        resp = self._opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout)
        with resp:
            code = getattr(resp, "status", 200)
            ctype = resp.headers.get_content_type()
            if ctype.startswith("text/") or ctype in ("application/json", "application/xml"):
                raise _Fatal("Provider returned an error page instead of a video")
            total = 0
            if code == 206:
                m = re.match(r"bytes (\d+)-\d+/(\d+|\*)", resp.headers.get("Content-Range", ""))
                if not m or int(m.group(1)) != have:
                    self._unlink(part)
                    raise ConnectionError("unexpected Content-Range, restarting")
                total = int(m.group(2)) if m.group(2).isdigit() else 0
                mode = "ab"
            else:  # 200: server ignored Range (or none asked): restart from zero
                have = 0
                mode = "wb"
                cl = resp.headers.get("Content-Length", "")
                total = int(cl) if cl.isdigit() else 0
            e.bytes_total = total
            e.bytes_done = have
            needed = max(total - have, 0) + self.min_free
            if self.free_bytes() < needed:
                raise _Fatal(
                    f"Not enough disk space (need {needed // 1024**2} MB free, "
                    f"have {self.free_bytes() // 1024**2} MB)"
                )
            samples = self._samples.setdefault(e.id, deque(maxlen=200))
            watch = self._watches.setdefault(e.id, self._slow_watch())
            host = urllib.parse.urlsplit(url).netloc
            if watch.fraction <= 0 and self._fractions.get(host):
                watch.fraction = self._fractions[host]  # what earlier downloads learned
            done = have
            cl = resp.headers.get("Content-Length", "")
            length = int(cl) if cl.isdigit() else (max(total - have, 0) if code == 206 else 0)
            watch.new_connection(time.time(), done, length)
            if watch.fraction > 0:
                self._fractions[host] = watch.fraction
            with open(part, mode) as f:
                while True:
                    ctl.check()
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    try:
                        f.write(chunk)
                    except OSError as exc:
                        if exc.errno == errno.ENOSPC:
                            raise _Fatal("Disk is full") from exc
                        raise
                    done += len(chunk)
                    e.bytes_done = done
                    now = time.time()
                    samples.append((now, done))
                    if watch.feed(now, done) and (not total or done < total):
                        raise _Reconnect(
                            f"connection slowed down (best {watch.best / 1e6:.1f} MB/s), "
                            f"reconnecting (#{watch.reconnects})"
                        )
                    if watch.should_rotate(done) and (not total or done < total):
                        raise _Reconnect(
                            f"fast part of the response is spent ({watch.fraction:.0%} of "
                            f"{length // 1024**2} MB), rotating (#{watch.rotations})"
                        )
        if total and done < total:
            return False
        if total and done > total:
            raise _Fatal("Received more data than announced")
        self._finalize(e, ctl, part, done)
        return True

    def _already_complete(
        self, e: Entry, ctl: _Ctl, part: pathlib.Path, exc: urllib.error.HTTPError
    ) -> bool:
        """416 on a Range request: the .part may already hold the whole file."""
        m = re.match(r"bytes \*/(\d+)", exc.headers.get("Content-Range", "") if exc.headers else "")
        if not m or not part.exists() or part.stat().st_size != int(m.group(1)):
            return False
        e.bytes_total = int(m.group(1))
        self._finalize(e, ctl, part, e.bytes_total)
        return True

    def _finalize(self, e: Entry, ctl: _Ctl, part: pathlib.Path, done: int) -> None:
        with self._lock:
            if ctl.stop.is_set():
                raise _Stopped
            os.replace(part, self.safe_path(e.filename))
            e.status = "done"
            e.bytes_done = e.bytes_total = done
            e.src_bytes = done
            e.finished_at = time.time()
            e.error = None
            self._watches.pop(e.id, None)


# ============================================================================
# Provider resolution (uses the same building blocks as the play routes)
# ============================================================================


def _pick_source(source_id: str) -> Any:
    from cache import get_sources

    xtream_sources = [s for s in get_sources() if s.type == "xtream"]
    for s in xtream_sources:
        if source_id and s.id == source_id:
            return s
    return xtream_sources[0] if xtream_sources else None


def build_provider_url(entry: Entry) -> str:
    """Same URL the play routes build: /movie/... or /series/... with source credentials."""
    from m3u import get_xtream_client_by_source

    src = _pick_source(entry.source_id)
    client = get_xtream_client_by_source(src.id) if src else None
    if not client:
        raise _Fatal("Source is not configured any more")
    stream_type = "movie" if entry.kind == "movie" else "series"
    return client.build_stream_url(stream_type, int(entry.item_id), entry.src_ext or entry.ext)


def describe_item(
    kind: str,
    item_id: str,
    source_id: str = "",
    ext: str = "",
    series_id: int | None = None,
) -> dict[str, Any]:
    """Look up source, container extension, title and poster in the cached catalogs.

    Blocking (may hit the provider through the info cache): call from a thread.
    """
    from cache import get_cache, get_cached_info
    from m3u import get_xtream_client_by_source, load_vod_data

    result: dict[str, Any] = {
        "source_id": source_id,
        "ext": ext,
        "title": "",
        "poster": None,
        "series_id": series_id,
    }
    if kind == "movie":
        streams = get_cache().get("vod_streams")
        if streams is None:
            try:
                streams = load_vod_data()[1]
            except Exception:
                streams = []
        movie = next((m for m in streams if str(m.get("stream_id")) == str(item_id)), None)
        if movie:
            result["source_id"] = source_id or movie.get("source_id", "")
            result["ext"] = ext or movie.get("container_extension") or ""
            result["title"] = movie.get("name", "")
            result["poster"] = movie.get("stream_icon") or None
    elif series_id:
        src = _pick_source(source_id)
        client = get_xtream_client_by_source(src.id) if src else None
        if src and not source_id:
            result["source_id"] = src.id
        if client:
            try:
                series = get_cached_info(
                    f"series_info_{src.id}_{series_id}", lambda: client.get_series_info(series_id)
                )
            except Exception as exc:
                log.info("Series info unavailable for download: %s", exc)
                series = None
            if series:
                sname = (series.get("info") or {}).get("name", "")
                cover = (series.get("info") or {}).get("cover")
                for season, eps in (series.get("episodes") or {}).items():
                    for ep in eps:
                        if str(ep.get("id")) == str(item_id):
                            result["ext"] = ext or ep.get("container_extension") or ""
                            tail = re.sub(r"^S\d+E\d+\s*-\s*", "", ep.get("title", "") or "")
                            code = f"S{int(season):02d}E{int(ep.get('episode_num', 0) or 0):02d}"
                            result["title"] = " ".join(x for x in (sname, code, tail.strip()) if x)
                            img = (ep.get("info") or {}).get("movie_image")
                            result["poster"] = img or cover or None
    if not result["source_id"]:
        src = _pick_source("")
        result["source_id"] = src.id if src else ""
    return result


def default_source_limits(source_id: str) -> tuple[int, int]:
    """(max_streams of the source, valid playback sessions currently on it)."""
    from cache import load_server_settings

    import ffmpeg_session

    max_streams = 0
    for s in load_server_settings().get("sources", []):
        if s.get("id") == source_id:
            max_streams = int(s.get("max_streams", 0) or 0)
    playing = sum(
        1
        for _, s in ffmpeg_session.get_source_sessions(source_id)
        if ffmpeg_session.is_session_valid(s)
    )
    return max_streams, playing


# ============================================================================
# Reachability
# ============================================================================


def _source_endpoints() -> list[tuple[str, int]]:
    from cache import get_sources

    out: list[tuple[str, int]] = []
    for s in get_sources():
        try:
            p = urllib.parse.urlparse(s.url)
            if p.hostname:
                out.append((p.hostname, p.port or (443 if p.scheme == "https" else 80)))
        except ValueError:
            continue
    return list(dict.fromkeys(out))


async def check_online(
    endpoints: list[tuple[str, int]] | None = None, timeout: float = 2.0
) -> bool:
    """True if any configured source host accepts a TCP connection. Never raises."""

    async def probe(host: str, port: int) -> bool:
        _, writer = await asyncio.open_connection(host, port)
        writer.close()
        return True

    async def any_reachable() -> bool:
        eps = endpoints if endpoints is not None else await asyncio.to_thread(_source_endpoints)
        if not eps:
            return True  # nothing configured, nothing to be offline from
        tasks = [asyncio.ensure_future(probe(h, p)) for h, p in eps]
        try:
            for fut in asyncio.as_completed(tasks):
                try:
                    if await fut:
                        return True
                except Exception:
                    continue
            return False
        finally:
            for t in tasks:
                t.cancel()

    try:
        return await asyncio.wait_for(any_reachable(), timeout)
    except Exception:
        return False


# ============================================================================
# Singleton + routes
# ============================================================================

_manager: DownloadManager | None = None
_manager_lock = threading.Lock()


# Set by main.py: (entry, provider file) -> ISO 639-1 original language or "" (may block)
vo_resolver: Callable[[Entry, pathlib.Path], str] | None = None


def default_optimizer(entry: Entry, src: pathlib.Path, dst: pathlib.Path) -> Any:
    """Single audio MP4 of ``src`` (rule in remux.py)."""
    import remux

    vo = ""
    if vo_resolver is not None:
        try:
            vo = vo_resolver(entry, src) or ""
        except Exception:
            log.debug("original language lookup failed", exc_info=True)
    return remux.remux(src, dst, vo=vo, pref=entry.audio_pref)


def get_manager() -> DownloadManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            from cache import get_user_agent

            _manager = DownloadManager(
                default_download_dir(),
                build_provider_url,
                user_agent=get_user_agent,
                source_limits=default_source_limits,
                optimizer=None if os.environ.get("NETV_DOWNLOAD_REMUX") == "0" else default_optimizer,
            )
        return _manager


def set_manager(manager: DownloadManager | None) -> None:
    """Replace the singleton (tests)."""
    global _manager
    with _manager_lock:
        _manager = manager


def startup() -> None:
    """Create the manager, load the index and start the scheduler tick."""
    get_manager().start()


def shutdown() -> None:
    if _manager is not None:
        _manager.shutdown()


class DownloadRequest(BaseModel):
    kind: Literal["movie", "episode"]
    id: str
    source_id: str = ""
    title: str = ""
    # Optional extras the UI may know (saves a lookup / offline friendly)
    ext: str = ""
    series_id: int | None = None
    poster: str | None = None


class AudioChangeRequest(BaseModel):
    track: int


def build_router(
    require_auth: Callable[..., Any],
    describe: Callable[..., dict[str, Any]] | None = None,
) -> APIRouter:
    """Routes: /api/downloads*, /downloads/files/{id}, /api/online."""
    describe = describe or describe_item
    router = APIRouter()
    auth = [Depends(require_auth)]

    @router.post("/api/downloads", dependencies=auth)
    async def create_download(req: DownloadRequest):
        item_id = req.id.strip()
        if not item_id or (req.kind == "movie" and not item_id.isdigit()) or len(item_id) > 40:
            raise HTTPException(400, "Invalid id")
        if req.kind == "episode" and not item_id.isdigit():
            raise HTTPException(400, "Invalid id")
        mgr = get_manager()
        existing = mgr.lookup([f"{req.kind}:{item_id}"]).get(f"{req.kind}:{item_id}")
        if existing and existing["status"] in ACTIVE:
            entry = mgr.get(existing["download_id"])
            if entry:
                return {"download": mgr.public(entry)}
        info = await asyncio.to_thread(
            describe, req.kind, item_id, req.source_id, req.ext, req.series_id
        )
        entry = mgr.add(
            req.kind,
            item_id,
            source_id=info.get("source_id") or req.source_id,
            title=req.title or info.get("title") or "",
            ext=info.get("ext") or req.ext or "mkv",
            poster=req.poster or info.get("poster"),
            series_id=req.series_id or info.get("series_id"),
        )
        return {"download": mgr.public(entry)}

    @router.get("/api/downloads", dependencies=auth)
    async def list_downloads():
        mgr = get_manager()
        return {
            "downloads": mgr.list_all(),
            "dir": str(mgr.dir),
            "free_bytes": mgr.free_bytes(),
        }

    @router.get("/api/downloads/lookup", dependencies=auth)
    async def lookup_downloads(keys: str = ""):
        wanted = [k.strip() for k in keys.split(",") if ":" in k][:500]
        return get_manager().lookup(wanted)

    @router.get("/api/downloads/status", dependencies=auth)
    async def status_downloads(kind: str = "", id: str = "", ids: str = ""):
        keys = [k.strip() for k in ids.split(",") if ":" in k]
        if kind and id:
            keys.append(f"{kind}:{id}")
        found = get_manager().lookup(keys[:500])
        if kind and id and not ids:
            item = found.get(f"{kind}:{id}")
            return item or {"status": None, "download_id": None}
        return found

    @router.post("/api/downloads/{download_id}/pause", dependencies=auth)
    async def pause_download(download_id: str):
        mgr = get_manager()
        return {"download": mgr.public(mgr.pause(download_id))}

    @router.post("/api/downloads/{download_id}/resume", dependencies=auth)
    async def resume_download(download_id: str):
        mgr = get_manager()
        return {"download": mgr.public(mgr.resume(download_id))}

    @router.post("/api/downloads/{download_id}/cancel", dependencies=auth)
    async def cancel_download(download_id: str):
        mgr = get_manager()
        return {"download": mgr.public(mgr.cancel(download_id))}

    @router.post("/api/downloads/{download_id}/audio", dependencies=auth)
    async def change_audio(download_id: str, req: AudioChangeRequest):
        """Another audio track means another file: the download is replaced and fetched again."""
        mgr = get_manager()
        entry = await asyncio.to_thread(mgr.redownload_with_audio, download_id, req.track)
        return {"download": mgr.public(entry)}

    @router.post("/api/downloads/{download_id}/optimize", dependencies=auth)
    async def optimize_download(download_id: str):
        mgr = get_manager()
        mgr._require(download_id)
        return {"queued": mgr.optimize(download_id)}

    @router.delete("/api/downloads/{download_id}", dependencies=auth)
    async def delete_download(download_id: str):
        get_manager().delete(download_id)
        return {"ok": True}

    @router.get("/downloads/files/{download_id}", dependencies=auth)
    async def download_file(download_id: str):
        entry, path = get_manager().file_for(download_id)
        media_type = mimetypes.guess_type(entry.filename)[0] or "application/octet-stream"
        # FileResponse handles Range / If-Range / HEAD; content-disposition inline
        return FileResponse(path, media_type=media_type, content_disposition_type="inline")

    @router.get("/api/online", dependencies=auth)
    async def api_online():
        return {"online": await check_online()}

    return router
