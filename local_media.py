"""Instant, seekable playback of finished offline downloads.

A downloaded file is local, so nothing here needs a live transcode session:

* The file is probed once (ffprobe, cached by path + size + mtime).
* When the browser can play it as is, the client uses the raw file (Range
  requests, native seeking, see ``/downloads/files/{id}``).
* Otherwise a COMPLETE VOD HLS playlist (with ``#EXT-X-ENDLIST``) is built up
  front from the keyframe index and every segment is produced on demand by a
  short ffmpeg run (``-ss`` before ``-i`` on a local file is instant). There is
  no session, no long running ffmpeg and nothing to restart when seeking: any
  segment can be fetched at any time, in any order.

Layout of the HLS presentation (the same as the transcode path, see
``ffmpeg_command.build_hls_ffmpeg_cmd``): classic MPEG-TS segments written by ffmpeg's
own muxer, no hand made containers and no timestamp patching.

* one video (+ audio) segment per keyframe cut: stream copy of the video (variant
  ``copy``) or H.264 re-encode on a fixed grid (variant ``x264``, when the codec is not
  playable or as a last resort fallback);
* a file with at most one audio track has that audio muxed into the video segments; with
  2 or more, the video segments are video only and every track is an audio-only rendition
  (``EXT-X-MEDIA``) cut on the very same segment boundaries;
* audio: AAC-LC stereo is copied, anything else is encoded to AAC (stereo, 48 kHz) on a
  global grid of whole frames, with 2 frames of lead in that are dropped, so the
  independently encoded segments overlap-add like one continuous encode;
* subtitles: not part of HLS. Text tracks are extracted once to complete WebVTT
  files (``/local/{id}/sub/{n}.vtt``) that the player loads as text tracks.

Every segment is cut with ``-ss`` before ``-i`` (timestamps restart at the seek point) and
placed with ``-output_ts_offset``, so every emitted timestamp is
``pts - T0 + TIMELINE_OFFSET`` with T0 the pts of the first video keyframe, and segment
n+1 continues segment n exactly.

Only finished files inside the download folder are ever served (the path comes
from ``downloads.DownloadManager.file_for``, never from the request).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import pathlib
import re

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse

import downloads


log = logging.getLogger(__name__)

PLAN_VERSION = 5  # bump when the plan / segment layout changes (invalidates caches)
TARGET_SEGMENT_SEC = 6.0
MAX_COPY_SEGMENT_SEC = 90.0  # longer keyframe gaps: re-encode on a fixed grid instead
TIMELINE_OFFSET = 1.0  # seconds added to every timestamp (keeps dts >= 0 with B-frames)
AAC_FRAME = 1024
AUDIO_LEAD_FRAMES = 2  # encoder warm up frames so independent segments overlap-add cleanly
OUT_SAMPLE_RATE = 48000
VARIANTS = ("copy", "x264")
DEFAULT_CACHE_MB = 1024
FFMPEG_TIMEOUT = 120.0
MAX_CONCURRENT_JOBS = 4

DIRECT_VIDEO = {"h264", "hevc", "vp9", "av1"}
DIRECT_AUDIO = {"aac", "mp3", "opus", "flac", "vorbis"}
# Video codecs that can be copied into MPEG-TS (hls.js transmuxes them for MSE)
HLS_COPY_VIDEO = {"h264", "hevc"}
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text", "subviewer"}

LANG_2 = {
    "eng": "en", "fre": "fr", "fra": "fr", "jpn": "ja", "spa": "es", "ger": "de", "deu": "de",
    "ita": "it", "por": "pt", "rus": "ru", "chi": "zh", "zho": "zh", "kor": "ko", "ara": "ar",
    "dut": "nl", "nld": "nl", "pol": "pl", "tur": "tr", "hin": "hi", "swe": "sv", "dan": "da",
    "nor": "no", "fin": "fi", "gre": "el", "ell": "el", "heb": "he", "cze": "cs", "ces": "cs",
    "rum": "ro", "ron": "ro", "hun": "hu", "ukr": "uk", "tha": "th", "vie": "vi", "ind": "id",
    "per": "fa", "fas": "fa", "cat": "ca",
}  # fmt: skip


# =============================================================================
# Probe (pure parsing)
# =============================================================================


@dataclass(frozen=True)
class VideoInfo:
    index: int
    codec: str
    profile: str
    level: int
    width: int
    height: int
    pix_fmt: str
    fps: float
    start: float
    bit_depth: int


@dataclass(frozen=True)
class AudioInfo:
    index: int  # absolute stream index
    ordinal: int  # position among audio streams (0-based)
    codec: str
    profile: str
    channels: int
    sample_rate: int
    start: float
    lang: str
    title: str
    default: bool


@dataclass(frozen=True)
class SubInfo:
    index: int
    ordinal: int
    codec: str
    lang: str
    title: str
    default: bool
    forced: bool

    @property
    def is_text(self) -> bool:
        return self.codec in TEXT_SUB_CODECS


@dataclass(frozen=True)
class MediaInfo:
    container: str  # "mp4" | "mkv" | "webm" | other ffprobe format name (avi, mpegts...)
    ext: str
    duration: float
    start: float  # format start_time (ffmpeg -ss is relative to it)
    bit_rate: int
    size: int
    video: VideoInfo | None
    audio: tuple[AudioInfo, ...]
    subs: tuple[SubInfo, ...]


def _num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _frac(value: Any) -> float:
    try:
        num, den = str(value).split("/")
        return float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def _container_kind(format_name: str, ext: str) -> str:
    names = set((format_name or "").split(","))
    if names & {"mov", "mp4", "m4a", "3gp"}:
        return "mp4"
    if "matroska" in names or "webm" in names:
        return "webm" if ext == "webm" else "mkv"
    return next(iter(names), "") or ext


_GENERIC_HANDLER = re.compile(r"handler|core media|isom|mp4|mov_text|text media", re.I)


def _track_title(tags: dict[str, Any]) -> str:
    """Track title; MP4 keeps it in ``handler_name`` (muxers write 'SubtitleHandler' by default)."""
    title = str(tags.get("title", ""))
    if title:
        return title
    handler = str(tags.get("handler_name", ""))
    return "" if _GENERIC_HANDLER.search(handler) else handler


def parse_probe(data: dict[str, Any], ext: str = "", size: int = 0) -> MediaInfo:
    """Turn ``ffprobe -show_format -show_streams`` JSON into a MediaInfo."""
    fmt = data.get("format") or {}
    ext = ext.lower().lstrip(".")
    video: VideoInfo | None = None
    audio: list[AudioInfo] = []
    subs: list[SubInfo] = []
    for s in data.get("streams") or []:
        kind = s.get("codec_type")
        tags = {str(k).lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        idx = int(s.get("index", 0))
        if kind == "video" and not disp.get("attached_pic") and video is None:
            depth = int(_num(s.get("bits_per_raw_sample"), 0)) or (
                10 if "10" in str(s.get("pix_fmt", "")) else 8
            )
            video = VideoInfo(
                index=idx,
                codec=str(s.get("codec_name", "")),
                profile=str(s.get("profile", "")),
                level=int(_num(s.get("level"), 0)),
                width=int(_num(s.get("width"), 0)),
                height=int(_num(s.get("height"), 0)),
                pix_fmt=str(s.get("pix_fmt", "")),
                fps=_frac(s.get("avg_frame_rate")) or _frac(s.get("r_frame_rate")) or 24.0,
                start=_num(s.get("start_time"), 0.0),
                bit_depth=depth,
            )
        elif kind == "audio":
            audio.append(
                AudioInfo(
                    index=idx,
                    ordinal=len(audio),
                    codec=str(s.get("codec_name", "")),
                    profile=str(s.get("profile", "")),
                    channels=int(_num(s.get("channels"), 2)),
                    sample_rate=int(_num(s.get("sample_rate"), OUT_SAMPLE_RATE)),
                    start=_num(s.get("start_time"), 0.0),
                    lang=str(tags.get("language", "")),
                    title=str(tags.get("title", "")),
                    default=bool(disp.get("default")),
                )
            )
        elif kind == "subtitle":
            subs.append(
                SubInfo(
                    index=idx,
                    ordinal=len(subs),
                    codec=str(s.get("codec_name", "")),
                    lang=str(tags.get("language", "")),
                    title=_track_title(tags),
                    default=bool(disp.get("default")),
                    forced=bool(disp.get("forced")),
                )
            )
    return MediaInfo(
        container=_container_kind(str(fmt.get("format_name", "")), ext),
        ext=ext,
        duration=_num(fmt.get("duration")),
        start=_num(fmt.get("start_time")),
        bit_rate=int(_num(fmt.get("bit_rate"))),
        size=size or int(_num(fmt.get("size"))),
        video=video,
        audio=tuple(audio),
        subs=tuple(subs),
    )


# =============================================================================
# Codec strings and the direct play decision (pure)
# =============================================================================

_H264_PROFILE_IDC = {
    "baseline": 0x42, "constrained baseline": 0x42, "main": 0x4D, "extended": 0x58,
    "high": 0x64, "high 10": 0x6E, "high 4:2:2": 0x7A, "high 4:4:4 predictive": 0xF4,
}  # fmt: skip


def video_codec_string(v: VideoInfo) -> str:
    """RFC 6381 codec string for MSE / canPlayType (best effort)."""
    if v.codec == "h264":
        idc = _H264_PROFILE_IDC.get(v.profile.lower(), 0x64)
        cons = 0x40 if v.profile.lower() == "constrained baseline" else 0
        level = v.level if v.level > 0 else 40
        return f"avc1.{idc:02X}{cons:02X}{level:02X}"
    if v.codec == "hevc":
        main10 = v.bit_depth > 8 or "10" in v.profile
        profile, compat = (2, "4") if main10 else (1, "6")
        level = v.level if v.level > 0 else 120
        return f"hvc1.{profile}.{compat}.L{level}.B0"
    if v.codec == "vp9":
        return "vp09.00.10.08" if v.bit_depth <= 8 else "vp09.02.10.10"
    if v.codec == "av1":
        return "av01.0.08M.08" if v.bit_depth <= 8 else "av01.0.08M.10"
    return v.codec


def audio_codec_string(a: AudioInfo) -> str:
    if a.codec == "aac":
        return {"he-aac": "mp4a.40.5", "he-aacv2": "mp4a.40.29"}.get(a.profile.lower(), "mp4a.40.2")
    return {"mp3": "mp4a.6B", "opus": "opus", "flac": "flac", "vorbis": "vorbis"}.get(
        a.codec, a.codec
    )


def hls_audio_is_copy(a: AudioInfo) -> bool:
    """Plain AAC-LC up to stereo is copied; everything else is encoded to AAC."""
    return a.codec == "aac" and a.profile.upper() in ("LC", "") and a.channels <= 2


@dataclass(frozen=True)
class DirectDecision:
    ok: bool
    mimes: tuple[str, ...] = ()
    reason: str = ""


def direct_play_decision(info: MediaInfo) -> DirectDecision:
    """Can the raw file be handed to a <video> tag? (the client still checks canPlayType)."""
    v = info.video
    if v is None:
        return DirectDecision(False, reason="no video stream")
    if info.container not in ("mp4", "mkv", "webm"):
        return DirectDecision(False, reason=f"container {info.container} is not browser friendly")
    if v.codec not in DIRECT_VIDEO:
        return DirectDecision(False, reason=f"video codec {v.codec}")
    if v.codec == "h264" and (v.bit_depth > 8 or v.pix_fmt not in ("yuv420p", "yuvj420p", "")):
        return DirectDecision(False, reason="H.264 above 8 bit 4:2:0")
    for a in info.audio:
        if a.codec not in DIRECT_AUDIO:
            return DirectDecision(False, reason=f"audio codec {a.codec}")
        if a.codec == "vorbis" and info.container == "mp4":
            return DirectDecision(False, reason="vorbis in mp4")
    codecs = [video_codec_string(v)]
    if info.audio:
        codecs.append(audio_codec_string(info.audio[0]))
    joined = ", ".join(codecs)
    container_mime = {"mp4": "video/mp4", "mkv": "video/x-matroska", "webm": "video/webm"}[
        info.container
    ]
    mimes = [f'{container_mime}; codecs="{joined}"']
    if info.container == "mkv":
        # Some browsers only know the webm label for matroska with vp9/av1
        mimes.append(f'video/webm; codecs="{joined}"')
    return DirectDecision(True, tuple(mimes))


def hls_variant_mime(info: MediaInfo, variant: str) -> str:
    """MSE mime of the video + audio the HLS variant produces."""
    v = info.video
    if v is None:
        return ""
    codecs = ["avc1.640028" if variant == "x264" else video_codec_string(v)]
    if info.audio:
        codecs.append("mp4a.40.2")
    return f'video/mp4; codecs="{", ".join(codecs)}"'


def video_variant_needs_encode(info: MediaInfo) -> bool:
    """True when stream copy cannot produce a playable MPEG-TS."""
    return info.video is None or info.video.codec not in HLS_COPY_VIDEO


# =============================================================================
# Segment planning (pure)
# =============================================================================


@dataclass(frozen=True)
class VideoSeg:
    start: float  # seconds on the presentation timeline (0 = first keyframe)
    duration: float
    abs_start: float  # keyframe pts in the file (copy), unused for x264
    packets: int  # video packets in the segment (copy), 0 for x264
    span: float = (
        0.0  # copy: input length that ends just before the next keyframe's dts (0 = unknown)
    )


@dataclass(frozen=True)
class KeyframeIndex:
    keyframes: tuple[tuple[float, int], ...]  # (pts seconds, packet index in decode order)
    packets: int
    end: float  # pts of the last frame + one frame duration
    frame_dur: float
    dts: tuple[float, ...] = ()  # decode time of each keyframe (nan when unknown), parallel


def plan_video_copy(kf: KeyframeIndex, target: float = TARGET_SEGMENT_SEC) -> list[VideoSeg]:
    """Cut on keyframes, as late as possible without going past ``target`` seconds.

    A GOP longer than the target simply becomes one longer segment.
    """
    if not kf.keyframes:
        return []
    t0 = kf.keyframes[0][0]
    cuts: list[tuple[float, int]] = [kf.keyframes[0]]
    last = kf.keyframes[0]  # latest keyframe seen since the current cut
    for cand in kf.keyframes[1:]:
        if cand[0] - cuts[-1][0] > target + 1e-6:
            # cand is too far: close the segment on the previous keyframe when there is one
            cut = last if last is not cuts[-1] else cand
            cuts.append(cut)
        last = cand
    tail = max(kf.end, last[0] + kf.frame_dur)
    if last is not cuts[-1] and tail - cuts[-1][0] > target + 1e-6:
        cuts.append(last)
    dts_of = dict(zip((i for _, i in kf.keyframes), kf.dts, strict=False))
    segs: list[VideoSeg] = []
    for n, (t, i) in enumerate(cuts):
        span = 0.0
        if n + 1 < len(cuts):
            nt, ni = cuts[n + 1]
            nd = dts_of.get(ni, math.nan)
            if not math.isnan(nd) and nd > t:
                # the next keyframe's dts is the first packet of the next segment: stop half a frame before
                span = round(nd - t - kf.frame_dur / 2, 6)
        else:
            nt, ni = max(kf.end, t + kf.frame_dur), kf.packets
        segs.append(VideoSeg(round(t - t0, 6), round(nt - t, 6), t, max(1, ni - i), span))
    return segs


def plan_video_grid(duration: float, seg: float = TARGET_SEGMENT_SEC) -> list[VideoSeg]:
    """Fixed grid (re-encoded video): every segment starts on its own IDR frame."""
    count = max(1, math.ceil(duration / seg - 1e-9))
    out = []
    for i in range(count):
        start = i * seg
        length = min(seg, duration - start) if duration > start else seg
        out.append(VideoSeg(start, length, start, 0))
    return out


# =============================================================================
# Playlists (pure)
# =============================================================================


def media_playlist(segments: list[tuple[str, float]]) -> str:
    """Complete VOD media playlist (ENDLIST) of MPEG-TS segments."""
    target = max(1, math.ceil(max((d for _, d in segments), default=1.0)))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    for uri, dur in segments:
        lines.append(f"#EXTINF:{dur:.6f},")
        lines.append(uri)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _attr(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ").replace("\r", " ")


def audio_track_names(audio: tuple[AudioInfo, ...]) -> list[str]:
    """Unique, readable NAME for each rendition."""
    names: list[str] = []
    for a in audio:
        base = a.title or a.lang or f"Piste {a.ordinal + 1}"
        name = base
        n = 2
        while name in names:
            name = f"{base} ({n})"
            n += 1
        names.append(name)
    return names


def audio_is_separate(info: MediaInfo) -> bool:
    """Same rule as the transcode path: 2+ audio tracks become audio-only renditions."""
    return len(info.audio) >= 2


def master_playlist(info: MediaInfo, variant: str) -> str:
    v = info.video
    assert v is not None
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-INDEPENDENT-SEGMENTS"]
    separate = audio_is_separate(info)
    if separate:
        names = audio_track_names(info.audio)
        default_ord = next((a.ordinal for a in info.audio if a.default), 0)
        for a, name in zip(info.audio, names, strict=True):
            lang = LANG_2.get(a.lang.lower(), a.lang.lower()) or "und"
            flag = "YES" if a.ordinal == default_ord else "NO"
            lines.append(
                f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="{_attr(name)}",'
                f'LANGUAGE="{_attr(lang)}",DEFAULT={flag},AUTOSELECT=YES,'
                f'CHANNELS="{min(a.channels, 2)}",URI="a{a.ordinal}.m3u8"'
            )
    codecs = ["avc1.640028" if variant == "x264" else video_codec_string(v)]
    if info.audio:
        codecs.append("mp4a.40.2")
    bandwidth = max(info.bit_rate, 500_000)
    stream = (
        f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},AVERAGE-BANDWIDTH={bandwidth},"
        f'CODECS="{",".join(codecs)}"'
    )
    if v.width and v.height:
        stream += f",RESOLUTION={v.width}x{v.height}"
    if v.fps:
        stream += f",FRAME-RATE={v.fps:.3f}"
    if separate:
        stream += ',AUDIO="aud"'
    lines += [stream, "v.m3u8"]
    return "\n".join(lines) + "\n"


def adts_packets(data: bytes) -> list[bytes]:
    """Split an ADTS stream into its frames."""
    out: list[bytes] = []
    i = 0
    while i + 7 <= len(data):
        if data[i] != 0xFF or data[i + 1] & 0xF0 != 0xF0:
            raise ValueError("lost ADTS sync")
        length = ((data[i + 3] & 3) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        if length < 7 or i + length > len(data):
            break
        out.append(data[i : i + length])
        i += length
    return out


# =============================================================================
# WebVTT helpers (pure)
# =============================================================================

_VTT_TIME = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})\.(\d{3})")


def _vtt_stamp(sec: float) -> str:
    ms = round(max(0.0, sec) * 1000)
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def shift_vtt(text: str, delta: float) -> str:
    """Move every cue by ``delta`` seconds (clamped at 0). No-op for tiny deltas."""
    if abs(delta) < 0.001:
        return text

    def sub(m: re.Match[str]) -> str:
        total = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        return _vtt_stamp(total + int(m.group(4)) / 1000 + delta)

    return "\n".join(
        _VTT_TIME.sub(sub, line) if "-->" in line else line for line in text.split("\n")
    )


# =============================================================================
# ffmpeg command builders (pure)
# =============================================================================
#
# One segment = one short ffmpeg run producing MPEG-TS with ffmpeg's own muxer, the same
# container and codecs as the transcode path (ffmpeg_command.build_hls_ffmpeg_cmd): video
# copied on keyframes (or x264), audio copied (AAC-LC stereo) or encoded to AAC. The input
# is seeked with ``-ss`` BEFORE ``-i`` (timestamps restart at 0 at the seek point) and
# ``-output_ts_offset`` puts every segment back on the shared timeline, so segment n+1
# continues segment n by construction (never mixed with ``-copyts``).

_TS_OUT = [
    "-avoid_negative_ts", "disabled", "-muxdelay", "0", "-muxpreload", "0",
    "-f", "mpegts", "pipe:1",
]  # fmt: skip
_BASE = ["ffmpeg", "-nostdin", "-v", "error", "-y"]


def _seek_args(seconds: float) -> list[str]:
    return ["-ss", f"{max(seconds, 0.0):.6f}"] if seconds > 0 else []


def seg_file_start(pres_t0: float, variant: str, seg: VideoSeg) -> float:
    """Position of the segment start in the file (file clock, before the format start)."""
    return seg.abs_start if variant == "copy" else pres_t0 + seg.start


def _video_input(
    path: str, info: MediaInfo, variant: str, t0: float, seg: VideoSeg, muxed: bool
) -> tuple[list[str], list[str]]:
    """(input args, output args) of the video part of a segment.

    A copied video stream normally stops after exactly ``seg.packets`` packets. That
    ``-frames:v`` limit also ends the audio of the same run early, so when audio is muxed in,
    the video input is limited by time instead (``seg.span``, from the keyframe dts).
    """
    assert info.video is not None
    v = info.video
    seek = _seek_args(seg_file_start(t0, variant, seg) - info.start)
    if variant == "copy":
        limit = ["-t", f"{seg.span:.6f}"] if muxed and seg.span > 0 else []
        out = ["-map", "0:0", "-c:v", "copy", "-copypriorss", "0"]
        if not limit:
            out += ["-frames:v", str(seg.packets)]
        return [*seek, *limit, "-i", path], out
    fps = v.fps if 1 < v.fps < 120 else 24.0
    out = [
        "-map", "0:0", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", "4.0", "-bf", "0",
        "-g", str(int(fps * 12)),
    ]  # fmt: skip
    return [*seek, "-t", f"{seg.duration:.6f}", "-i", path], out


def segment_cmd(
    path: str,
    info: MediaInfo,
    variant: str,
    t0: float,
    seg: VideoSeg,
    *,
    video: bool,
    audio: AudioInfo | None,
    piped_audio_shift: float | None = None,
) -> list[str]:
    """One TS segment: ``video`` and/or ``audio`` (copied from the file, or, when
    ``piped_audio_shift`` is set, AAC packets read from stdin that start ``shift`` seconds
    after the segment start).

    The video and the audio are two inputs of the same file, each limited to the time
    window of the segment: ffmpeg's own ``-ss``/``-t`` decide which packets belong to it.
    """
    cmd = list(_BASE)
    nxt = 0  # index of the next input
    vmap: list[str] = []
    if video:
        vin, vmap = _video_input(path, info, variant, t0, seg, muxed=audio is not None)
        cmd += [*vin]
        nxt += 1
    amap: list[str] = []
    if audio is not None:
        if piped_audio_shift is not None:
            cmd += ["-itsoffset", f"{piped_audio_shift:.6f}", "-f", "aac", "-i", "pipe:0"]
            amap = ["-map", f"{nxt}:a", "-c:a", "copy"]
        else:
            seek = _seek_args(seg_file_start(t0, variant, seg) - info.start)
            cmd += [*seek, "-t", f"{seg.duration:.6f}", "-i", path]
            amap = ["-map", f"{nxt}:{audio.index}", "-c:a", "copy", "-copypriorss", "0"]
    cmd += [*vmap, *amap]
    if not video:
        cmd += ["-vn"]
    if audio is None:
        cmd += ["-an"]
    cmd += ["-sn", "-dn", "-output_ts_offset", f"{seg.start + TIMELINE_OFFSET:.6f}", *_TS_OUT]
    return cmd


def audio_pipe_cmd(path: str, info: MediaInfo, a: AudioInfo, t0: float, f0: int, count: int):
    """Encode AAC for global frames [f0, f0 + count) of the 48 kHz frame grid (ADTS on stdout).

    The grid starts at the first keyframe. The encoder starts ``lead`` frames early so
    that the packets kept (see ``audio_keep_from``) are the ones a single continuous encode
    of the whole track would produce, up to quantisation: every segment then splices onto
    the previous one with the AAC overlap-add intact.
    """
    lead = min(AUDIO_LEAD_FRAMES, f0)
    start = (f0 - lead) * AAC_FRAME / OUT_SAMPLE_RATE
    channels = ["-ac", "2"] if a.channels > 2 else []
    bitrate = "96k" if a.channels == 1 else "192k"
    return [
        *_BASE, *_seek_args((t0 - info.start) + start),
        "-i", path, "-map", f"0:{a.index}",
        "-af", f"aresample={OUT_SAMPLE_RATE}:async=1:first_pts=0",
        "-c:a", "aac", "-b:a", bitrate, *channels, "-profile:a", "aac_low",
        "-frames:a", str(lead + 1 + count), "-vn", "-sn", "-dn",
        "-f", "adts", "pipe:1",
    ]  # fmt: skip


def audio_frame_range(seg_start: float, seg_end: float) -> tuple[int, int]:
    """Global 48 kHz AAC frame range [f0, f1) whose start lies inside [seg_start, seg_end)."""
    f0 = math.ceil(seg_start * OUT_SAMPLE_RATE / AAC_FRAME - 1e-6)
    f1 = math.ceil(seg_end * OUT_SAMPLE_RATE / AAC_FRAME - 1e-6)
    return f0, max(f1, f0 + 1)


def audio_keep_from(f0: int) -> int:
    """Index of the first kept packet in the encode that started at frame ``f0 - lead``.

    Packet j holds the audio of frame ``f0 - lead + j - 1`` (packet 0 is the encoder priming
    packet). The first segment keeps everything (its priming packet lies before time 0).
    """
    return 0 if f0 == 0 else min(AUDIO_LEAD_FRAMES, f0) + 1


def audio_only_pipe_cmd(first_packet_time: float, seg: VideoSeg) -> list[str]:
    """ADTS on stdin -> audio-only TS whose first packet sits at ``first_packet_time`` (s)."""
    return [
        *_BASE, "-f", "aac", "-i", "pipe:0", "-map", "0:a", "-c:a", "copy",
        "-output_ts_offset", f"{first_packet_time + TIMELINE_OFFSET:.6f}", *_TS_OUT,
    ]  # fmt: skip


def subtitle_cmd(path: str, subs: list[SubInfo], outs: list[str]) -> list[str]:
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", path]
    for s, out in zip(subs, outs, strict=True):
        cmd += ["-map", f"0:{s.index}", "-c:s", "webvtt", "-f", "webvtt", out]
    return cmd


def scan_cmd(path: str, video_index: int) -> list[str]:
    return [
        "ffprobe", "-v", "error", "-select_streams", str(video_index),
        "-show_entries", "packet=pts_time,dts_time,flags", "-of", "csv=p=0", path,
    ]  # fmt: skip


def parse_scan(lines: Any, default_fps: float = 24.0) -> KeyframeIndex:
    """Parse ``pts,dts,flags`` packet lines (decode order) into a KeyframeIndex."""
    keyframes: list[tuple[float, int]] = []
    kf_dts: list[float] = []
    count = 0
    last_pts = 0.0
    prev_dts: float | None = None
    deltas: list[float] = []
    for line in lines:
        parts = str(line).strip().split(",")
        if len(parts) < 3:
            continue
        pts = _num(parts[0], math.nan)
        dts = _num(parts[1], math.nan)
        if math.isnan(pts):
            pts = dts
        if math.isnan(pts):
            count += 1
            continue
        if parts[2][:1] == "K":
            keyframes.append((pts, count))
            kf_dts.append(dts)
        last_pts = max(last_pts, pts)
        if not math.isnan(dts):
            if prev_dts is not None and dts - prev_dts > 0 and len(deltas) < 500:
                deltas.append(dts - prev_dts)
            prev_dts = dts
        count += 1
    deltas.sort()
    frame_dur = deltas[len(deltas) // 2] if deltas else 1.0 / default_fps
    return KeyframeIndex(tuple(keyframes), count, last_pts + frame_dur, frame_dur, tuple(kf_dts))


# =============================================================================
# Service (cache, ffmpeg runs, dedupe)
# =============================================================================


@dataclass
class Presentation:
    """Everything needed to serve one file (and one variant) once probed and scanned."""

    info: MediaInfo
    variant: str
    t0: float  # pts of the first video keyframe (presentation time 0)
    duration: float
    video: list[VideoSeg]
    key: str
    dir: pathlib.Path
    path: str


def default_cache_root() -> pathlib.Path:
    env = os.environ.get("NETV_LOCAL_CACHE_DIR", "").strip()
    if env:
        return pathlib.Path(env).expanduser()
    from cache import CACHE_DIR

    return CACHE_DIR / "local-hls"


def cache_limit_bytes() -> int:
    try:
        mb = int(os.environ.get("NETV_LOCAL_CACHE_MB", DEFAULT_CACHE_MB))
    except ValueError:
        mb = DEFAULT_CACHE_MB
    return max(16, mb) * 1024 * 1024


class LocalMedia:
    """Probe, plan and produce HLS pieces for local files. One instance per process."""

    def __init__(self, cache_root: pathlib.Path | None = None, max_bytes: int | None = None):
        self.root = cache_root or default_cache_root()
        self.max_bytes = max_bytes if max_bytes is not None else cache_limit_bytes()
        self._infos: dict[str, MediaInfo] = {}
        self._kfs: dict[str, KeyframeIndex] = {}
        self._pres: dict[str, Presentation] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._jobs: asyncio.Semaphore | None = None
        self._writes = 0
        self.ffmpeg_runs = 0  # observable in tests and logs

    # ------------------------------------------------------------ helpers

    def _lock(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = self._locks[name] = asyncio.Lock()
        return lock

    @staticmethod
    def file_key(path: pathlib.Path) -> str:
        st = path.stat()
        raw = f"{PLAN_VERSION}|{path}|{st.st_size}|{st.st_mtime_ns}"
        return hashlib.sha1(raw.encode()).hexdigest()[:20]

    async def _run(
        self, cmd: list[str], stdin: bytes | None = None, timeout: float = FFMPEG_TIMEOUT
    ) -> bytes:
        if self._jobs is None:
            self._jobs = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
        async with self._jobs:
            self.ffmpeg_runs += 1
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
            except BaseException:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
                raise
        if proc.returncode != 0:
            tail = err.decode(errors="replace")[-300:]
            raise RuntimeError(f"{cmd[0]} failed ({proc.returncode}): {tail}")
        return out

    # -------------------------------------------------------------- probe

    async def info(self, path: pathlib.Path) -> MediaInfo:
        key = self.file_key(path)
        cached = self._infos.get(key)
        if cached:
            return cached
        async with self._lock("probe:" + key):
            cached = self._infos.get(key)
            if cached:
                return cached
            cmd = [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ]  # fmt: skip
            out = await self._run(cmd, timeout=60)
            info = parse_probe(json.loads(out or b"{}"), path.suffix, path.stat().st_size)
            self._infos[key] = info
            return info

    async def keyframes(self, path: pathlib.Path, info: MediaInfo) -> KeyframeIndex:
        """Keyframe index of the video stream (one full packet scan, cached on disk)."""
        assert info.video is not None
        key = self.file_key(path)
        cached = self._kfs.get(key)
        if cached:
            return cached
        async with self._lock("scan:" + key):
            cached = self._kfs.get(key)
            if cached:
                return cached
            store = self.root / key / "keyframes.json"
            with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
                raw = json.loads(store.read_text())
                kf = KeyframeIndex(
                    tuple((float(t), int(i)) for t, i in raw["kf"]),
                    int(raw["packets"]),
                    float(raw["end"]),
                    float(raw["frame_dur"]),
                    tuple(float(d) if d is not None else math.nan for d in raw.get("dts", ())),
                )
                self._kfs[key] = kf
                return kf
            out = await self._run(scan_cmd(str(path), info.video.index), timeout=1800)
            kf = parse_scan(out.decode(errors="replace").splitlines(), info.video.fps)
            self._kfs[key] = kf
            store.parent.mkdir(parents=True, exist_ok=True)
            tmp = store.with_suffix(f".{os.getpid()}.tmp")
            payload = {
                "kf": kf.keyframes,
                "packets": kf.packets,
                "end": kf.end,
                "frame_dur": kf.frame_dur,
                "dts": [None if math.isnan(d) else d for d in kf.dts],
            }
            tmp.write_text(json.dumps(payload))
            tmp.replace(store)
            return kf

    async def presentation(self, path: pathlib.Path, variant: str) -> Presentation:
        info = await self.info(path)
        if info.video is None:
            raise HTTPException(422, "No video stream")
        key = self.file_key(path)
        if variant == "copy" and video_variant_needs_encode(info):
            variant = "x264"
        cached = self._pres.get(f"{key}:{variant}")
        if cached:
            return cached
        kf = await self.keyframes(path, info)
        if not kf.keyframes:
            raise HTTPException(422, "No keyframes found")
        t0 = kf.keyframes[0][0]
        duration = max(kf.end - t0, 0.001)
        container_tail = info.duration - (t0 - info.start)
        if container_tail > duration + 1:
            duration = container_tail  # the container knows a longer tail (audio outlasts video)
        video = plan_video_copy(kf) if variant == "copy" else plan_video_grid(duration)
        if variant == "copy" and max(s.duration for s in video) > MAX_COPY_SEGMENT_SEC:
            variant = "x264"
            video = plan_video_grid(duration)
        pres = Presentation(
            info, variant, t0, duration, video, key, self.root / key / variant, str(path)
        )
        self._pres[f"{key}:{variant}"] = pres
        return pres

    # ---------------------------------------------------------- segments

    async def _cached(
        self, pres: Presentation, name: str, produce: Callable[[], Any]
    ) -> pathlib.Path:
        """Return the cached file, producing it once (concurrent callers share the work)."""
        target = pres.dir / name
        if target.is_file():
            with contextlib.suppress(OSError):
                os.utime(target)
            return target
        async with self._lock(f"{pres.key}:{pres.variant}:{name}"):
            if target.is_file():
                return target
            files = await produce()
            for fname, data in files.items():
                dest = pres.dir / fname
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
                tmp.write_bytes(data)
                tmp.replace(dest)
            self._writes += 1
            if self._writes % 25 == 0:
                await asyncio.to_thread(self.trim_cache)
            return target

    def segment_window(self, pres: Presentation, k: int) -> tuple[float, float]:
        """[start, end) of segment k on the presentation timeline."""
        seg = pres.video[k]
        return seg.start, seg.start + seg.duration

    async def segment(self, pres: Presentation, n: int | None, k: int) -> pathlib.Path:
        """Segment k as MPEG-TS. ``n`` is the audio track of an audio-only rendition; ``None``
        is the video segment (with the only audio track muxed in when the file has one)."""
        info = pres.info
        seg = pres.video[k]
        muxed = not audio_is_separate(info)
        if n is None:
            audio = info.audio[0] if muxed and info.audio else None
            name = f"v/{k}.ts"
        else:
            audio = info.audio[n]
            name = f"a{n}/{k}.ts"

        async def produce() -> dict[str, bytes]:
            if audio is None or hls_audio_is_copy(audio):
                cmd = segment_cmd(
                    pres.path, info, pres.variant, pres.t0, seg, video=n is None, audio=audio
                )
                return {name: await self._run(cmd)}
            start, end = self.segment_window(pres, k)
            f0, f1 = audio_frame_range(start, end)
            enc = audio_pipe_cmd(pres.path, info, audio, pres.t0, f0, f1 - f0)
            packets = adts_packets(await self._run(enc))
            keep = audio_keep_from(f0)
            packets = packets[keep : keep + (f1 - f0) + (1 if f0 == 0 else 0)]
            if not packets:
                raise RuntimeError("audio encode produced no packets")
            first = (f0 - 1 if f0 == 0 else f0) * AAC_FRAME / OUT_SAMPLE_RATE
            if n is None:
                cmd = segment_cmd(
                    pres.path, info, pres.variant, pres.t0, seg,
                    video=True, audio=audio, piped_audio_shift=first - seg.start,
                )  # fmt: skip
            else:
                cmd = audio_only_pipe_cmd(first, seg)
            return {name: await self._run(cmd, stdin=b"".join(packets))}

        return await self._cached(pres, name, produce)

    # --------------------------------------------------------- subtitles

    async def subtitle(self, path: pathlib.Path, info: MediaInfo, n: int) -> pathlib.Path:
        """Complete WebVTT of text subtitle ``n`` (all text tracks are extracted in one pass)."""
        key = self.file_key(path)
        outdir = self.root / key / "sub"
        target = outdir / f"{n}.vtt"
        if target.is_file():
            return target
        texts = [s for s in info.subs if s.is_text]
        if not any(s.ordinal == n for s in texts):
            raise HTTPException(404, "No such text subtitle")
        async with self._lock("sub:" + key):
            if target.is_file():
                return target
            outdir.mkdir(parents=True, exist_ok=True)
            outs = [str(outdir / f"{s.ordinal}.raw.vtt") for s in texts]
            await self._run(subtitle_cmd(str(path), texts, outs), timeout=1800)
            shift = 0.0
            with contextlib.suppress(Exception):
                # cue times are relative to the container start, the timeline to the first keyframe
                shift = (await self.presentation(path, "copy")).t0 - info.start
            for s, out in zip(texts, outs, strict=True):
                text = pathlib.Path(out).read_text(encoding="utf-8", errors="replace")
                tmp = outdir / f"{s.ordinal}.vtt.{os.getpid()}.tmp"
                tmp.write_text(shift_vtt(text, -shift), encoding="utf-8")
                tmp.replace(outdir / f"{s.ordinal}.vtt")
                pathlib.Path(out).unlink(missing_ok=True)
        return target

    # ------------------------------------------------------------- cache

    def trim_cache(self) -> int:
        """Delete the least recently used segments until the cache fits. Returns bytes freed."""
        files: list[tuple[float, int, pathlib.Path]] = []
        total = 0
        for p in [*self.root.glob("*/*/*/*.ts")]:
            with contextlib.suppress(OSError):
                st = p.stat()
                files.append((st.st_mtime, st.st_size, p))
                total += st.st_size
        freed = 0
        for _, size, p in sorted(files):
            if total - freed <= self.max_bytes:
                break
            with contextlib.suppress(OSError):
                p.unlink()
                freed += size
        return freed


# =============================================================================
# Plan (JSON for the client)
# =============================================================================


def build_plan(download_id: str, info: MediaInfo, duration: float) -> dict[str, Any]:
    v = info.video
    direct = direct_play_decision(info)
    base = f"/local/{download_id}"
    subs = [
        {
            "idx": s.ordinal,
            "lang": s.lang,
            "title": s.title,
            "forced": s.forced,
            "default": s.default,
            "url": f"{base}/sub/{s.ordinal}.vtt",
        }
        for s in info.subs
        if s.is_text
    ]
    hls = []
    if v is not None:
        for variant in VARIANTS:
            if variant == "copy" and video_variant_needs_encode(info):
                continue
            hls.append(
                {
                    "variant": variant,
                    "master": f"{base}/{variant}/master.m3u8",
                    "mime": hls_variant_mime(info, variant),
                }
            )
    return {
        "id": download_id,
        "duration": duration,
        "container": info.container,
        "video": {
            "codec": v.codec if v else "",
            "width": v.width if v else 0,
            "height": v.height if v else 0,
        },
        "audio": [
            {
                "idx": a.ordinal,
                "lang": a.lang,
                "title": a.title,
                "codec": a.codec,
                "channels": a.channels,
                "default": a.default,
            }
            for a in info.audio
        ],
        "subs": subs,
        "direct": {
            "ok": direct.ok,
            "reason": direct.reason,
            "url": f"/downloads/files/{download_id}",
            "mimes": list(direct.mimes),
        },
        "hls": hls,
    }


# =============================================================================
# Routes
# =============================================================================

_service: LocalMedia | None = None


def get_service() -> LocalMedia:
    global _service
    if _service is None:
        _service = LocalMedia()
    return _service


def set_service(service: LocalMedia | None) -> None:
    """Replace the singleton (tests)."""
    global _service
    _service = service


M3U8 = "application/vnd.apple.mpegurl"
_FAILURES = (RuntimeError, ValueError, OSError, TimeoutError, json.JSONDecodeError)


def build_router(require_auth: Callable[..., Any]) -> APIRouter:
    router = APIRouter()
    auth = [Depends(require_auth)]

    def finished_file(download_id: str) -> pathlib.Path:
        """Path of a FINISHED download inside the download folder (never a .part file)."""
        manager = downloads.get_manager()
        _, path = manager.file_for(download_id)
        if not manager.is_inside(str(path)):
            raise HTTPException(400, "File is outside the download folder")
        return path

    async def presentation(download_id: str, variant: str) -> Presentation:
        if variant not in VARIANTS:
            raise HTTPException(404, "Unknown variant")
        path = finished_file(download_id)
        try:
            return await get_service().presentation(path, variant)
        except _FAILURES as exc:
            log.warning("local media plan failed for %s: %s", download_id, exc)
            raise HTTPException(500, "Could not analyse the file") from exc

    async def produce(coro: Any) -> FileResponse:
        try:
            path = await coro
        except _FAILURES as exc:
            log.warning("local segment failed: %s", exc)
            raise HTTPException(500, "Segment failed") from exc
        # no-cache: the URLs do not change when a fix changes the segment bytes, and a browser
        # holding an old segment for minutes would keep playing the old (wrongly aligned) audio
        return FileResponse(
            path, media_type="video/mp2t", headers={"Cache-Control": "private, no-cache"}
        )

    def playlist(text: str) -> Response:
        return Response(text, media_type=M3U8, headers={"Cache-Control": "no-store"})

    @router.get("/api/local/{download_id}/plan", dependencies=auth)
    async def local_plan(download_id: str):
        path = finished_file(download_id)
        svc = get_service()
        try:
            info = await svc.info(path)
            duration = info.duration
            if info.video is not None:
                # exact presentation length once the keyframe index exists
                with contextlib.suppress(*_FAILURES, HTTPException):
                    duration = (await svc.presentation(path, "copy")).duration
        except _FAILURES as exc:
            log.warning("local media probe failed for %s: %s", download_id, exc)
            raise HTTPException(500, "Could not analyse the file") from exc
        plan = build_plan(download_id, info, duration)
        entry = downloads.get_manager().get(download_id)
        if entry is not None and len(entry.source_audio) > 1:
            plan["source_audio"] = {
                "tracks": entry.source_audio,
                "kept": entry.audio_kept,
                "size": entry.src_bytes,
            }
        return plan

    @router.get("/local/{download_id}/{variant}/master.m3u8", dependencies=auth)
    async def local_master(download_id: str, variant: str):
        pres = await presentation(download_id, variant)
        return playlist(master_playlist(pres.info, pres.variant))

    @router.get("/local/{download_id}/{variant}/v.m3u8", dependencies=auth)
    async def local_video_playlist(download_id: str, variant: str):
        pres = await presentation(download_id, variant)
        segs = [(f"v/{i}.ts", s.duration) for i, s in enumerate(pres.video)]
        return playlist(media_playlist(segs))

    @router.get("/local/{download_id}/{variant}/a{n}.m3u8", dependencies=auth)
    async def local_audio_playlist(download_id: str, variant: str, n: int):
        pres = await presentation(download_id, variant)
        if not audio_is_separate(pres.info) or not 0 <= n < len(pres.info.audio):
            raise HTTPException(404, "No such audio track")
        segs = [(f"a{n}/{k}.ts", s.duration) for k, s in enumerate(pres.video)]
        return playlist(media_playlist(segs))

    @router.get("/local/{download_id}/{variant}/v/{k}.ts", dependencies=auth)
    async def local_video_segment(download_id: str, variant: str, k: int):
        pres = await presentation(download_id, variant)
        if not 0 <= k < len(pres.video):
            raise HTTPException(404, "No such segment")
        return await produce(get_service().segment(pres, None, k))

    @router.get("/local/{download_id}/{variant}/a{n}/{k}.ts", dependencies=auth)
    async def local_audio_segment(download_id: str, variant: str, n: int, k: int):
        pres = await presentation(download_id, variant)
        if not audio_is_separate(pres.info) or not 0 <= n < len(pres.info.audio):
            raise HTTPException(404, "No such segment")
        if not 0 <= k < len(pres.video):
            raise HTTPException(404, "No such segment")
        return await produce(get_service().segment(pres, n, k))

    @router.get("/local/{download_id}/sub/{n}.vtt", dependencies=auth)
    async def local_subtitle(download_id: str, n: int):
        path = finished_file(download_id)
        svc = get_service()
        try:
            out = await svc.subtitle(path, await svc.info(path), n)
        except _FAILURES as exc:
            log.warning("subtitle extraction failed for %s: %s", download_id, exc)
            raise HTTPException(500, "Subtitle extraction failed") from exc
        return FileResponse(
            out, media_type="text/vtt", headers={"Cache-Control": "private, max-age=3600"}
        )

    return router
