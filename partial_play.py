"""Watch a download while it is still running.

The ``.part`` file grows as the provider sends it. One ffmpeg per download reads it through
a pipe (a feeder thread follows the file like ``tail -f`` and closes the pipe once the last
byte has been written) and remuxes it into an HLS *event* playlist: everything already
downloaded is seekable, the playlist keeps growing with the download and is closed
(``ENDLIST``) when the film is complete. No extra provider connection is used.

Video is stream copied (H.264 in MPEG-TS, HEVC in fMP4), the chosen audio track is encoded to
stereo AAC and text subtitles are written as growing WebVTT files.

Routes: ``/play/partial/{id}`` (page), ``/api/partial/{id}/status``, ``/partial/{id}/{file}``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import asyncio
import contextlib
import logging
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import downloads
import remux


log = logging.getLogger(__name__)

TMP_PREFIX = "netv_partial_"
MIN_BYTES = 8 * 1024 * 1024  # enough for the container header and a first segment
IDLE_SEC = 120.0  # the page pings every 20 s; no ping for this long ends the session
FEED_CHUNK = 1024 * 1024
_FILE_RE = re.compile(r"^(stream\.m3u8|seg\d+\.(ts|m4s)|init\.mp4|sub\d+\.vtt)$")
_EXTINF = re.compile(r"#EXTINF:([\d.]+)")
TEMPLATES = Jinja2Templates(directory=pathlib.Path(__file__).parent / "templates")


@dataclass
class Session:
    download_id: str
    audio: int
    dir: pathlib.Path
    proc: subprocess.Popen[bytes]
    duration: float
    audio_tracks: list[dict[str, Any]]
    subs: list[dict[str, Any]]
    last_access: float = field(default_factory=time.time)
    stop: threading.Event = field(default_factory=threading.Event)


_sessions: dict[str, Session] = {}
_lock = threading.Lock()


def list_text_subs(probe: dict[str, Any]) -> list[dict[str, Any]]:
    """Text subtitle tracks: ``ord`` is the ordinal among ALL subtitle streams (for -map 0:s:N)."""
    subs: list[dict[str, Any]] = []
    ordinal = -1
    for s in probe.get("streams") or []:
        if s.get("codec_type") != "subtitle":
            continue
        ordinal += 1
        if str(s.get("codec_name", "")) not in remux.TEXT_SUBS:
            continue
        tags = {str(k).lower(): v for k, v in (s.get("tags") or {}).items()}
        raw = str(tags.get("language", ""))
        subs.append(
            {
                "idx": len(subs),
                "ord": ordinal,
                "lang": remux.lang2(raw) or raw.lower(),
                "title": str(tags.get("title", "")),
            }
        )
    return subs


def video_codec(probe: dict[str, Any]) -> str:
    for s in probe.get("streams") or []:
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic"):
            return str(s.get("codec_name", ""))
    return ""


def build_cmd(codec: str, audio: int, subs: list[dict[str, Any]], out: pathlib.Path) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt",
           "-i", "pipe:0"]  # fmt: skip
    for s in subs:
        cmd += ["-map", f"0:s:{s['ord']}", "-c:s", "webvtt", "-flush_packets", "1",
                str(out / f"sub{s['idx']}.vtt")]  # fmt: skip
    cmd += ["-map", "0:v:0", "-map", f"0:a:{audio}", "-c:v", "copy",
            "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "192k",
            "-max_delay", "5000000", "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
            "-hls_flags", "independent_segments", "-hls_playlist_type", "event"]  # fmt: skip
    if codec == "hevc":
        cmd += ["-tag:v", "hvc1", "-hls_segment_type", "fmp4",
                "-hls_segment_filename", str(out / "seg%05d.m4s")]  # fmt: skip
    else:
        cmd += ["-hls_segment_filename", str(out / "seg%05d.ts")]
    return [*cmd, str(out / "stream.m3u8")]


def ready_seconds(playlist: str) -> tuple[float, bool]:
    """(seconds already playable, playlist closed) from an HLS media playlist."""
    return sum(float(x) for x in _EXTINF.findall(playlist)), "#EXT-X-ENDLIST" in playlist


def _feed(sess: Session, path: pathlib.Path) -> None:
    """Copy the growing file into ffmpeg; the open handle survives the final rename/remux."""
    stdin = sess.proc.stdin
    assert stdin is not None
    try:
        with path.open("rb") as f:
            while not sess.stop.is_set() and sess.proc.poll() is None:
                chunk = f.read(FEED_CHUNK)
                if chunk:
                    stdin.write(chunk)
                    continue
                entry = downloads.get_manager().get(sess.download_id)
                if entry is None or entry.status in ("done", "cancelled"):
                    break
                if entry.bytes_total > 0 and f.tell() >= entry.bytes_total:
                    break
                time.sleep(0.5)  # wait for the download to write more
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(Exception):
            stdin.close()


def _watch(sess: Session) -> None:
    """End the session once nobody has asked for it for a while."""
    while not sess.stop.wait(10):
        if time.time() - sess.last_access > IDLE_SEC:
            stop(sess.download_id)
            return


def stop(download_id: str) -> None:
    with _lock:
        sess = _sessions.pop(download_id, None)
    if sess is None:
        return
    sess.stop.set()
    with contextlib.suppress(Exception):
        sess.proc.kill()
    shutil.rmtree(sess.dir, ignore_errors=True)
    log.info("Partial playback of %s stopped", download_id)


def shutdown() -> None:
    for download_id in list(_sessions):
        stop(download_id)


def cleanup_leftovers() -> None:
    """Remove the folders of sessions that did not survive a restart."""
    for d in pathlib.Path(tempfile.gettempdir()).glob(TMP_PREFIX + "*"):
        shutil.rmtree(d, ignore_errors=True)


def start(download_id: str, audio: int = -1) -> Session:
    """Session for this download (reused when the audio track is the same)."""
    manager = downloads.get_manager()
    entry = manager.get(download_id)
    if entry is None:
        raise HTTPException(404, "Téléchargement introuvable")
    with _lock:
        sess = _sessions.get(download_id)
    if sess is not None and sess.proc.poll() not in (None, 0):
        stop(download_id)  # ffmpeg failed: try again from scratch
        sess = None
    if sess is not None and audio in (-1, sess.audio):
        sess.last_access = time.time()
        return sess
    if sess is not None:
        stop(download_id)
    try:
        path = manager._part(entry)
    except ValueError as exc:
        raise HTTPException(400, "Nom de fichier invalide") from exc
    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        raise HTTPException(409, "Pas encore assez de données téléchargées")
    try:
        probe = remux.ffprobe(path, timeout=30)
    except Exception as exc:
        raise HTTPException(409, "Le début du fichier n'est pas encore lisible") from exc
    codec = video_codec(probe)
    if codec not in remux.COPY_VIDEO:
        raise HTTPException(415, "Ce fichier ne pourra être lu qu'une fois le téléchargement terminé")
    tracks = remux.list_audio(probe)
    if not tracks:
        raise HTTPException(415, "Aucune piste audio")
    if not 0 <= audio < len(tracks):
        audio = remux.choose_audio(tracks, "", entry.audio_pref)[0]
    subs = list_text_subs(probe)
    out = pathlib.Path(tempfile.mkdtemp(prefix=TMP_PREFIX))
    proc = subprocess.Popen(
        build_cmd(codec, audio, subs, out),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sess = Session(download_id, audio, out, proc, remux._duration(probe), tracks, subs)
    with _lock:
        _sessions[download_id] = sess
    threading.Thread(target=_feed, args=(sess, path), daemon=True, name="partial-feed").start()
    threading.Thread(target=_watch, args=(sess,), daemon=True, name="partial-watch").start()
    log.info("Partial playback of %s started (audio %d, %d subs)", entry.filename, audio, len(subs))
    return sess


def build_router(require_auth: Callable[..., Any]) -> APIRouter:
    router = APIRouter()
    auth = [Depends(require_auth)]

    @router.get("/play/partial/{download_id}", response_class=HTMLResponse, dependencies=auth)
    async def partial_page(request: Request, download_id: str, a: int = -1):
        entry = downloads.get_manager().get(download_id)
        if entry is None:
            raise HTTPException(404, "Download not found")
        if entry.status == "done" and download_id not in _sessions:
            return RedirectResponse(f"/play/download/{download_id}", status_code=303)
        sess = await asyncio.to_thread(start, download_id, a)
        return TEMPLATES.TemplateResponse(
            request,
            "partial_player.html",
            {
                "id": download_id,
                "title": entry.title,
                "duration": sess.duration,
                "audio": sess.audio,
                "audio_tracks": sess.audio_tracks,
                "subs": sess.subs,
            },
        )

    @router.get("/api/partial/{download_id}/status", dependencies=auth)
    async def partial_status(download_id: str):
        sess = _sessions.get(download_id)
        if sess is None:
            raise HTTPException(404, "No session")
        sess.last_access = time.time()
        entry = downloads.get_manager().get(download_id)
        playlist = sess.dir / "stream.m3u8"
        text = playlist.read_text() if playlist.is_file() else ""
        ready, ended = ready_seconds(text)
        return {
            "status": entry.status if entry else "cancelled",
            "bytes_done": entry.bytes_done if entry else 0,
            "bytes_total": entry.bytes_total if entry else 0,
            "duration": sess.duration,
            "ready": ready,
            "ended": ended,
            "failed": sess.proc.poll() not in (None, 0),
        }

    @router.get("/partial/{download_id}/{name}", dependencies=auth)
    async def partial_file(download_id: str, name: str):
        sess = _sessions.get(download_id)
        if sess is None or not _FILE_RE.match(name):
            raise HTTPException(404, "Not found")
        sess.last_access = time.time()
        path = sess.dir / name
        if name == "stream.m3u8":
            for _ in range(60):  # the first segment takes a moment
                if path.is_file() or sess.proc.poll() is not None:
                    break
                await asyncio.sleep(0.25)
            if not path.is_file():
                raise HTTPException(503, "Not ready")
            return Response(
                path.read_text(),
                media_type="application/vnd.apple.mpegurl",
                headers={"Cache-Control": "no-store"},
            )
        if not path.is_file():
            raise HTTPException(404, "Not found")
        if name.endswith(".vtt"):
            return Response(
                path.read_text(errors="replace"),
                media_type="text/vtt",
                headers={"Cache-Control": "no-store"},
            )
        return FileResponse(path, headers={"Cache-Control": "private, no-cache"})

    return router
