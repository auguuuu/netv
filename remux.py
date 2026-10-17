"""Turn a finished download into a light MP4 with ONE audio track.

Providers ship "[ALL]" releases: one H.264 video and up to ~20 audio tracks. Such a file cannot be
played directly by Chromium (no audio track switching), so the player falls back to HLS, which
spawns one ffmpeg per 6 s segment and stutters as soon as the machine is busy.

After the download we therefore stream copy the video and keep a single audio track:

* VO when the original language is English or French, otherwise the French track (English
  when the original language is unknown);
* an explicit ``pref`` (index among the file's audio tracks) overrides the rule, that is how a
  re-download with another language works.

Text subtitles are kept (as ``mov_text``) so the player's CC menu keeps working. The list of the
audio tracks of the source is returned, so the UI can still show them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import json
import logging
import pathlib
import re
import shutil
import subprocess

from original_lang import LANG_2


log = logging.getLogger(__name__)

COPY_VIDEO = {"h264", "hevc"}
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "subviewer"}
KEEP_LANGS = {"en", "fr"}  # VO is kept for these, everything else gets the French track
DEFAULT_LANG = "fr"
UNKNOWN_VO_LANG = "en"

# Variants of a language that are a worse default than the plain track
_AVOID = re.compile(
    r"canad|qu[eé]b|commentar|descri|\bad\b|audiodesc|latin|brazil|brasil|forced|karaoke", re.I
)


class RemuxError(Exception):
    """The file cannot (or should not) be remuxed; the original stays untouched."""


def lang2(tag: str) -> str:
    """ISO 639-1 code of an ffprobe language tag ('fre' -> 'fr'), '' when unknown."""
    tag = (tag or "").strip().lower()
    if not tag or tag in ("und", "mis", "mul", "zxx"):
        return ""
    return LANG_2.get(tag, tag if len(tag) == 2 else "")


@dataclass
class Plan:
    audio: list[dict[str, Any]] = field(default_factory=list)  # every audio track of the source
    kept: int = -1  # ordinal (among audio tracks) of the kept one
    reason: str = ""


def list_audio(probe: dict[str, Any]) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    for s in probe.get("streams") or []:
        if s.get("codec_type") != "audio":
            continue
        tags = {str(k).lower(): v for k, v in (s.get("tags") or {}).items()}
        raw = str(tags.get("language", ""))
        tracks.append(
            {
                "idx": len(tracks),
                "lang": lang2(raw) or raw.lower(),
                "title": str(tags.get("title", "")),
                "codec": str(s.get("codec_name", "")),
                "profile": str(s.get("profile", "")),
                "channels": int(s.get("channels") or 2),
                "default": bool((s.get("disposition") or {}).get("default")),
            }
        )
    return tracks


def choose_audio(tracks: list[dict[str, Any]], vo: str = "", pref: int = -1) -> tuple[int, str]:
    """Ordinal of the audio track to keep and why. ``(-1, reason)`` when there is no audio."""
    if not tracks:
        return -1, "no audio"
    if 0 <= pref < len(tracks):
        return pref, "chosen"
    if len(tracks) == 1:
        return 0, "only track"
    vo = (vo or "").lower()
    if vo in KEEP_LANGS:
        wanted, reason = vo, "vo"
    elif vo:  # a known VO that is neither English nor French: the French dub
        wanted, reason = DEFAULT_LANG, f"fr (vo {vo})"
    else:  # unknown VO (no TMDB key, no country): most of the catalog is English
        wanted, reason = UNKNOWN_VO_LANG, "en (vo unknown)"

    def pick(lang: str) -> int:
        cands = [t for t in tracks if t["lang"] == lang]
        if not cands:
            return -1
        # worse variants last, then the default flag, then file order
        cands.sort(key=lambda t: (bool(_AVOID.search(t["title"])), not t["default"], t["idx"]))
        return cands[0]["idx"]

    idx = pick(wanted)
    if idx < 0 and not vo:  # unknown VO and no English track: French
        wanted, reason = DEFAULT_LANG, "fr (vo unknown, no english)"
        idx = pick(wanted)
    if idx >= 0:
        return idx, reason
    if vo:  # e.g. no French track: fall back to the VO if the file has it
        idx = pick(vo)
        if idx >= 0:
            return idx, f"{wanted} missing, vo kept"
    for t in tracks:
        if t["default"]:
            return t["idx"], f"{wanted} missing, default track"
    return 0, f"{wanted} missing, first track"


def ffprobe(path: pathlib.Path, timeout: float = 120.0) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         str(path)],
        capture_output=True, text=True, timeout=timeout, check=True,
    )  # fmt: skip
    return json.loads(out.stdout or "{}")


def _duration(probe: dict[str, Any]) -> float:
    try:
        return float((probe.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def needs_remux(probe: dict[str, Any], ext: str) -> bool:
    """False when the file is already a single-audio MP4 (nothing to gain)."""
    return ext.lower() != "mp4" or len(list_audio(probe)) != 1


def build_cmd(
    src: pathlib.Path,
    dst: pathlib.Path,
    probe: dict[str, Any],
    audio: dict[str, Any],
    *,
    subtitles: bool = True,
) -> list[str]:
    streams = probe.get("streams") or []
    video = next(
        (
            s for s in streams
            if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )  # fmt: skip
    if video is None:
        raise RemuxError("no video stream")
    if video.get("codec_name") not in COPY_VIDEO:
        raise RemuxError(f"video codec {video.get('codec_name')} is not remuxed")
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    a_stream = audio_streams[audio["idx"]]
    cmd = [
        "nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src),
        "-map", f"0:{video['index']}", "-map", f"0:{a_stream['index']}",
    ]  # fmt: skip
    n_sub = 0
    sub_titles: list[str] = []
    if subtitles:
        for s in streams:
            if s.get("codec_type") == "subtitle" and s.get("codec_name") in TEXT_SUBS:
                cmd += ["-map", f"0:{s['index']}"]
                n_sub += 1
                tags = {str(k).lower(): v for k, v in (s.get("tags") or {}).items()}
                sub_titles.append(str(tags.get("title", "")))
    cmd += ["-c:v", "copy"]
    if video.get("codec_name") == "hevc":
        cmd += ["-tag:v", "hvc1"]
    # Stereo AAC-LC is copied; anything else (E-AC-3, 5.1 AAC...) becomes stereo AAC:
    # browsers play a 5.1 AAC track of an MP4 silently on a stereo output
    if audio["codec"] == "aac" and audio["profile"].upper() in ("LC", "") and audio["channels"] <= 2:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-ac", "2", "-b:a", "192k"]
    if n_sub:
        cmd += ["-c:s", "mov_text"]
        # MP4 has no per-track "title": the handler name is what survives (read back by
        # local_media.parse_probe), it tells "Canadian" from "European" French subtitles
        for i, title in enumerate(sub_titles):
            if title:
                cmd += [f"-metadata:s:s:{i}", f"handler_name={title}"]
    cmd += [
        "-map_metadata", "0", "-map_chapters", "0",
        "-disposition:a:0", "default", "-movflags", "+faststart", "-f", "mp4", str(dst),
    ]  # fmt: skip
    return cmd


def verify(dst: pathlib.Path, src_probe: dict[str, Any]) -> None:
    """Raise RemuxError unless ``dst`` looks like a complete copy (one video, one audio)."""
    try:
        out = ffprobe(dst)
    except (subprocess.SubprocessError, ValueError, OSError) as exc:
        raise RemuxError(f"output unreadable: {exc}") from exc
    kinds = [s.get("codec_type") for s in out.get("streams") or []]
    if kinds.count("video") != 1 or kinds.count("audio") != 1:
        raise RemuxError("output has the wrong streams")
    want, got = _duration(src_probe), _duration(out)
    if want > 0 and got < want - max(2.0, want * 0.002):
        raise RemuxError(f"output too short ({got:.0f}s of {want:.0f}s)")


def remux(
    src: pathlib.Path,
    dst: pathlib.Path,
    *,
    vo: str = "",
    pref: int = -1,
    timeout: float = 3600.0,
    run: Any = subprocess.run,
) -> Plan:
    """Write ``dst`` (MP4, one audio track) from ``src`` and verify it. Never touches ``src``."""
    probe = ffprobe(src)
    tracks = list_audio(probe)
    kept, reason = choose_audio(tracks, vo, pref)
    if kept < 0:
        raise RemuxError("no audio track")
    free = shutil.disk_usage(dst.parent).free
    if free < int(src.stat().st_size * 0.9) + 1024**3:
        raise RemuxError("not enough free disk space")
    tmp = dst.with_name(dst.name + ".remux")
    last = ""
    for with_subs in (True, False):  # an odd subtitle track must not cost us the whole remux
        tmp.unlink(missing_ok=True)
        cmd = build_cmd(src, tmp, probe, tracks[kept], subtitles=with_subs)
        res = run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0 and tmp.is_file():
            break
        last = (res.stderr or "")[-300:]
        log.info("remux of %s failed (subs=%s): %s", src.name, with_subs, last)
    else:
        tmp.unlink(missing_ok=True)
        raise RemuxError(f"ffmpeg failed: {last}")
    try:
        verify(tmp, probe)
        tmp.replace(dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return Plan(audio=tracks, kept=kept, reason=reason)
