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

Layout of the HLS presentation:

* video: fragmented MP4 segments, stream copy cut on keyframes (variant
  ``copy``) or re-encoded to H.264 on a fixed grid (variant ``x264``, used when
  the video codec is not playable or as a last resort fallback);
* audio: one rendition per audio track, on a grid of whole AAC frames. AAC
  tracks are copied, anything else is encoded to AAC with 2 frames of lead in
  so that the independently encoded segments overlap-add cleanly;
* subtitles: not part of HLS. Text tracks are extracted once to complete WebVTT
  files (``/local/{id}/sub/{n}.vtt``) that the player loads as text tracks.

Every emitted timestamp is ``pts - T0 + TIMELINE_OFFSET`` with T0 the pts of the
first video keyframe, so all renditions share one exact timeline.

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
import struct

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse

import downloads


log = logging.getLogger(__name__)

PLAN_VERSION = 4  # bump when the plan / segment layout changes (invalidates caches)
TARGET_SEGMENT_SEC = 6.0
MAX_COPY_SEGMENT_SEC = 90.0  # longer keyframe gaps: re-encode on a fixed grid instead
TIMELINE_OFFSET = 1.0  # seconds added to every timestamp (keeps dts >= 0 with B-frames)
AAC_FRAME = 1024
AUDIO_LEAD_FRAMES = 2  # encoder warm up frames so chunks overlap-add like one stream
AUDIO_CHUNK_SEGMENTS = 8  # encoded audio segments made by one ffmpeg run (no seam inside)
OUT_SAMPLE_RATE = 48000
VARIANTS = ("copy", "x264")
DEFAULT_CACHE_MB = 1024
FFMPEG_TIMEOUT = 120.0
MAX_CONCURRENT_JOBS = 4

DIRECT_VIDEO = {"h264", "hevc", "vp9", "av1"}
DIRECT_AUDIO = {"aac", "mp3", "opus", "flac", "vorbis"}
# Video codecs whose fragmented MP4 copy plays through MSE (client still verifies support)
HLS_COPY_VIDEO = {"h264", "hevc", "av1", "vp9"}
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
                    title=str(tags.get("title", "")),
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
    """True when stream copy cannot produce a playable fragmented MP4."""
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


@dataclass(frozen=True)
class KeyframeIndex:
    keyframes: tuple[tuple[float, int], ...]  # (pts seconds, packet index in decode order)
    packets: int
    end: float  # pts of the last frame + one frame duration
    frame_dur: float


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
    segs: list[VideoSeg] = []
    for n, (t, i) in enumerate(cuts):
        if n + 1 < len(cuts):
            nt, ni = cuts[n + 1]
        else:
            nt, ni = max(kf.end, t + kf.frame_dur), kf.packets
        segs.append(VideoSeg(round(t - t0, 6), round(nt - t, 6), t, max(1, ni - i)))
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


@dataclass(frozen=True)
class AudioGrid:
    sample_rate: int
    frames_per_segment: int
    count: int
    total_samples: int  # duration in samples

    def seg_start_samples(self, k: int) -> int:
        return k * self.frames_per_segment * AAC_FRAME

    def seg_frames(self, k: int) -> int:
        """AAC frames in segment k (the last one is shorter)."""
        left = self.total_samples - self.seg_start_samples(k)
        return max(1, min(self.frames_per_segment, math.ceil(left / AAC_FRAME)))

    def seg_duration(self, k: int) -> float:
        return self.seg_frames(k) * AAC_FRAME / self.sample_rate


def plan_audio_grid(
    duration: float, sample_rate: int, target: float = TARGET_SEGMENT_SEC
) -> AudioGrid:
    frames = max(1, round(target * sample_rate / AAC_FRAME))
    total = max(1, math.ceil(duration * sample_rate))
    count = max(1, math.ceil(total / (frames * AAC_FRAME)))
    return AudioGrid(sample_rate, frames, count, total)


# =============================================================================
# Playlists (pure)
# =============================================================================


def media_playlist(init_uri: str, segments: list[tuple[str, float]]) -> str:
    """Complete VOD media playlist (ENDLIST) for fragmented MP4 segments."""
    target = max(1, math.ceil(max((d for _, d in segments), default=1.0)))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-INDEPENDENT-SEGMENTS",
        f'#EXT-X-MAP:URI="{init_uri}"',
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


def master_playlist(info: MediaInfo, variant: str) -> str:
    v = info.video
    assert v is not None
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
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
    if info.audio:
        stream += ',AUDIO="aud"'
    lines += [stream, "v.m3u8"]
    return "\n".join(lines) + "\n"


# =============================================================================
# Fragmented MP4 helpers (pure)
# =============================================================================


def _boxes(data: bytes | bytearray, start: int, end: int):
    """Yield (type, box_start, payload_start, box_end) for the boxes in [start, end)."""
    off = start
    while off + 8 <= end:
        size, typ = struct.unpack_from(">I4s", data, off)
        hdr = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, off + 8)[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            raise ValueError("corrupt mp4 box")
        yield typ, off, off + hdr, off + size
        off += size


def split_fmp4(data: bytes) -> tuple[bytes, bytes]:
    """Split a fragmented MP4 into (init segment, media fragments)."""
    for typ, box_start, _, _ in _boxes(data, 0, len(data)):
        if typ in (b"moof", b"styp", b"sidx"):
            return data[:box_start], data[box_start:]
    raise ValueError("no fragment in mp4 output")


def _trafs(data: bytes | bytearray):
    for typ, _, payload, end in _boxes(data, 0, len(data)):
        if typ != b"moof":
            continue
        for ttyp, _, tpayload, tend in _boxes(data, payload, end):
            if ttyp == b"traf":
                yield tpayload, tend


def first_composition_offset(media: bytes) -> int:
    """Composition offset (ticks) of the first sample, 0 when the fragment has none."""
    for tpayload, tend in _trafs(media):
        for typ, _, payload, _end in _boxes(media, tpayload, tend):
            if typ != b"trun":
                continue
            version = media[payload]
            flags = int.from_bytes(media[payload + 1 : payload + 4], "big")
            count = struct.unpack_from(">I", media, payload + 4)[0]
            if not flags & 0x800 or count == 0:
                return 0
            pos = payload + 8
            if flags & 0x1:
                pos += 4
            if flags & 0x4:
                pos += 4
            for bit in (0x100, 0x200, 0x400):
                if flags & bit:
                    pos += 4
            fmt = ">i" if version >= 1 else ">I"
            return int(struct.unpack_from(fmt, media, pos)[0])
        return 0
    return 0


def first_base_time(media: bytes) -> int:
    """baseMediaDecodeTime of the first fragment."""
    for tpayload, tend in _trafs(media):
        for typ, _, payload, _end in _boxes(media, tpayload, tend):
            if typ == b"tfdt":
                if media[payload] == 1:
                    return struct.unpack_from(">Q", media, payload + 4)[0]
                return struct.unpack_from(">I", media, payload + 4)[0]
    return 0


def set_first_base_time(media: bytes, target: int) -> bytes:
    """Shift every tfdt so the first fragment starts at ``target`` ticks (keeps the spacing)."""
    delta = target - first_base_time(media)
    out = bytearray(media)
    for tpayload, tend in _trafs(media):
        for typ, _, payload, _end in _boxes(media, tpayload, tend):
            if typ != b"tfdt":
                continue
            if out[payload] == 1:
                cur = struct.unpack_from(">Q", out, payload + 4)[0]
                struct.pack_into(">Q", out, payload + 4, max(0, cur + delta))
            else:
                new = struct.unpack_from(">I", out, payload + 4)[0] + delta
                if not 0 <= new < 2**32:
                    raise ValueError("timestamp does not fit a 32 bit tfdt")
                struct.pack_into(">I", out, payload + 4, new)
    return bytes(out)


def mp4_timescale(init: bytes) -> int:
    """Track timescale from the mdhd box of an init segment."""
    idx = init.find(b"mdhd")
    if idx < 0:
        return 16000
    version = init[idx + 4]
    pos = idx + 8 + (8 if version == 0 else 16)
    return struct.unpack_from(">I", init, pos)[0] or 16000


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

_MOV_FLAGS = "empty_moov+default_base_moof+frag_keyframe"


def _seek_args(seconds: float) -> list[str]:
    return ["-ss", f"{seconds:.6f}"] if seconds > 0 else []


def video_copy_cmd(path: str, info: MediaInfo, seg: VideoSeg) -> list[str]:
    assert info.video is not None
    v = info.video
    tag = ["-tag:v", "hvc1"] if v.codec == "hevc" else []
    return [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        *_seek_args(seg.abs_start - info.start),
        "-i", path, "-map", f"0:{v.index}", "-c:v", "copy", "-copypriorss", "0", *tag,
        "-frames:v", str(seg.packets), "-an", "-sn", "-dn",
        "-f", "mp4", "-movflags", _MOV_FLAGS, "pipe:1",
    ]  # fmt: skip


def video_x264_cmd(path: str, info: MediaInfo, seg: VideoSeg, t0: float) -> list[str]:
    assert info.video is not None
    v = info.video
    fps = v.fps if 1 < v.fps < 120 else 24.0
    return [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        *_seek_args((t0 - info.start) + seg.start),
        "-i", path, "-map", f"0:{v.index}", "-t", f"{seg.duration:.6f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-level:v", "4.0", "-bf", "0", "-g", str(int(fps * 12)),
        "-an", "-sn", "-dn",
        "-f", "mp4", "-movflags", _MOV_FLAGS, "pipe:1",
    ]  # fmt: skip


def audio_copy_cmd(path: str, info: MediaInfo, a: AudioInfo, grid: AudioGrid, k: int) -> list[str]:
    """Stream copy of the AAC frames of segment k (cut on frame boundaries).

    ``-copypriorss 0`` is essential: an input seek on a real mkv lands on the cue point
    BEFORE the target (often 1 to 2 s earlier), and without it ffmpeg copies every packet
    from there, so the segment holds audio from earlier than its timestamp (the sound
    then lags the picture by up to seconds). It drops the packets before the seek time.
    """
    seek = (a.start - info.start) + grid.seg_start_samples(k) / grid.sample_rate
    seek -= AAC_FRAME / (2 * grid.sample_rate)  # land in the middle of the frame before
    return [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        *_seek_args(seek),
        "-i", path, "-map", f"0:{a.index}", "-c:a", "copy", "-copypriorss", "0",
        "-frames:a", str(grid.seg_frames(k)), "-vn", "-sn", "-dn",
        "-f", "mp4", "-movflags", "empty_moov+default_base_moof", "pipe:1",
    ]  # fmt: skip


def audio_encode_cmd(
    path: str, info: MediaInfo, a: AudioInfo, grid: AudioGrid, first: int, count: int, t0: float
) -> list[str]:
    """Encode AAC for ``count`` consecutive segments starting at segment ``first``.

    One continuous encode covers the whole chunk, so there is no seam inside it. The encoder
    starts ``AUDIO_LEAD_FRAMES`` frames early (priming packet + warm up) so the first kept
    packet equals what one continuous encode of the whole track would have produced (see
    ``audio_chunk_slice``). A chunk starting at segment 0 keeps the priming packet (its
    timestamp is one frame before 0) so the decoder overlaps correctly.
    """
    frames = sum(grid.seg_frames(k) for k in range(first, first + count))
    seek = (t0 - info.start) + grid.seg_start_samples(first) / grid.sample_rate
    if first == 0:
        total = frames + 1
    else:
        total = AUDIO_LEAD_FRAMES + 1 + frames
        seek -= AUDIO_LEAD_FRAMES * AAC_FRAME / grid.sample_rate
    channels = ["-ac", "2"] if a.channels > 2 else []
    bitrate = "96k" if a.channels == 1 else "192k"
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        *_seek_args(seek),
        "-i", path, "-map", f"0:{a.index}",
        "-af", f"aresample={grid.sample_rate}:async=1:first_pts=0",
        "-c:a", "aac", "-b:a", bitrate, *channels,
        "-frames:a", str(total), "-vn", "-sn", "-dn",
        "-f", "adts", "pipe:1",
    ]  # fmt: skip
    return cmd


def audio_chunk_range(grid: AudioGrid, first: int, k: int) -> tuple[int, int, int]:
    """(start, stop, timestamp shift in samples) of segment k's packets in the chunk stream.

    A chunk starting at segment 0 begins with the encoder priming packet, which sits one frame
    before time 0 and travels with segment 0. A later chunk begins with the priming packet and
    ``AUDIO_LEAD_FRAMES`` warm up packets, which are skipped.
    """
    frames = grid.seg_frames(k)
    if first == 0:
        if k == 0:
            return 0, frames + 1, -AAC_FRAME
        start = 1 + k * grid.frames_per_segment
    else:
        start = AUDIO_LEAD_FRAMES + 1 + (k - first) * grid.frames_per_segment
    return start, start + frames, 0


def audio_chunk_slice(
    packets: list[bytes], grid: AudioGrid, first: int, k: int
) -> tuple[list[bytes], int]:
    """AAC packets of segment k inside the chunk that starts at segment ``first``."""
    start, stop, shift = audio_chunk_range(grid, first, k)
    return packets[start:stop], shift


class AdtsStream:
    """Incremental ADTS parser: feed bytes as they arrive, complete frames appear in packets."""

    def __init__(self) -> None:
        self.packets: list[bytes] = []
        self._pending = bytearray()

    def feed(self, data: bytes) -> None:
        self._pending += data
        buf = self._pending
        i = 0
        while i + 7 <= len(buf):
            if buf[i] != 0xFF or buf[i + 1] & 0xF0 != 0xF0:
                raise ValueError("lost ADTS sync")
            length = ((buf[i + 3] & 3) << 11) | (buf[i + 4] << 3) | (buf[i + 5] >> 5)
            if length < 7:
                raise ValueError("bad ADTS frame length")
            if i + length > len(buf):
                break
            self.packets.append(bytes(buf[i : i + length]))
            i += length
        del buf[:i]


def audio_mux_cmd() -> list[str]:
    """ADTS on stdin -> fragmented MP4 on stdout (stream copy, timestamps start at 0)."""
    return [
        "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "aac", "-i", "pipe:0", "-c:a", "copy",
        "-bsf:a", "aac_adtstoasc", "-f", "mp4", "-movflags", "empty_moov+default_base_moof", "pipe:1",
    ]  # fmt: skip


def audio_init_cmd(sample_rate: int, channels: int) -> list[str]:
    """One silent AAC frame muxed to fMP4: only its init segment (decoder config) is used.

    The ADTS -> MP4 step cannot write the decoder config (it is only known after the first
    packet, the fragmented header is written before), so the init comes from the encoder.
    """
    layout = "mono" if channels == 1 else "stereo"
    return [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={layout}",
        "-c:a", "aac", "-b:a", "96k" if channels == 1 else "192k", "-frames:a", "2",
        "-f", "mp4", "-movflags", "empty_moov+default_base_moof", "pipe:1",
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
        last_pts = max(last_pts, pts)
        if not math.isnan(dts):
            if prev_dts is not None and dts - prev_dts > 0 and len(deltas) < 500:
                deltas.append(dts - prev_dts)
            prev_dts = dts
        count += 1
    deltas.sort()
    frame_dur = deltas[len(deltas) // 2] if deltas else 1.0 / default_fps
    return KeyframeIndex(tuple(keyframes), count, last_pts + frame_dur, frame_dur)


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
    audio_grids: list[AudioGrid]
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


class _ChunkJob:
    """A running audio chunk encode: packets become available while ffmpeg is still going."""

    def __init__(self) -> None:
        self.stream = AdtsStream()
        self.done = False
        self.error: Exception | None = None
        self.changed = asyncio.Condition()
        self.task: asyncio.Task[None] | None = None

    async def wait_for(self, count: int) -> list[bytes]:
        """Block until ``count`` packets exist (or the encode ended); returns the packets."""
        async with self.changed:
            await self.changed.wait_for(lambda: len(self.stream.packets) >= count or self.done)
        if len(self.stream.packets) < count:
            raise self.error or RuntimeError("audio encode ended early")
        return self.stream.packets


class LocalMedia:
    """Probe, plan and produce HLS pieces for local files. One instance per process."""

    def __init__(self, cache_root: pathlib.Path | None = None, max_bytes: int | None = None):
        self.root = cache_root or default_cache_root()
        self.max_bytes = max_bytes if max_bytes is not None else cache_limit_bytes()
        self._infos: dict[str, MediaInfo] = {}
        self._kfs: dict[str, KeyframeIndex] = {}
        self._pres: dict[str, Presentation] = {}
        self._encoder_inits: dict[tuple[int, int], bytes] = {}
        self._chunk_jobs: dict[str, _ChunkJob] = {}
        self._chunk_slots: asyncio.Semaphore | None = None
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
        grids = [
            plan_audio_grid(duration, a.sample_rate if hls_audio_is_copy(a) else OUT_SAMPLE_RATE)
            for a in info.audio
        ]
        pres = Presentation(
            info, variant, t0, duration, video, grids, key, self.root / key / variant, str(path)
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

    async def video_segment(self, pres: Presentation, k: int) -> pathlib.Path:
        seg = pres.video[k]

        async def produce() -> dict[str, bytes]:
            if pres.variant == "copy":
                cmd = video_copy_cmd(pres.path, pres.info, seg)
            else:
                cmd = video_x264_cmd(pres.path, pres.info, seg, pres.t0)
            init, media = split_fmp4(await self._run(cmd))
            ticks = mp4_timescale(init)
            target = round((seg.start + TIMELINE_OFFSET) * ticks) - first_composition_offset(media)
            return {f"v/{k}.m4s": set_first_base_time(media, max(0, target)), "v/init.mp4": init}

        return await self._cached(pres, f"v/{k}.m4s", produce)

    async def video_init(self, pres: Presentation) -> pathlib.Path:
        target = pres.dir / "v" / "init.mp4"
        if not target.is_file():
            await self.video_segment(pres, 0)
        return target

    async def _run_chunk(self, job: _ChunkJob, cmd: list[str], target: pathlib.Path, name: str):
        """Encode a chunk, publishing packets as they come, then cache the whole stream."""
        if self._chunk_slots is None:
            self._chunk_slots = asyncio.Semaphore(2)
        proc = None
        try:
            async with self._chunk_slots:
                self.ffmpeg_runs += 1
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                assert proc.stdout is not None
                while data := await proc.stdout.read(1 << 16):
                    job.stream.feed(data)
                    async with job.changed:
                        job.changed.notify_all()
                if await proc.wait() != 0:
                    raise RuntimeError(f"audio chunk encode failed ({proc.returncode})")
            if job.stream.packets:
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
                tmp.write_bytes(b"".join(job.stream.packets))
                tmp.replace(target)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            job.error = exc
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            job.done = True
            async with job.changed:
                job.changed.notify_all()
            self._chunk_jobs.pop(name, None)

    async def _audio_packets(self, pres: Presentation, n: int, k: int) -> tuple[list[bytes], int]:
        """Encoded AAC packets of audio segment k (and their timestamp shift).

        Segments of one chunk come from ONE continuous ffmpeg encode (no seam between them);
        the first packets are usable while ffmpeg is still encoding the rest of the chunk.
        """
        a = pres.info.audio[n]
        grid = pres.audio_grids[n]
        c = k // AUDIO_CHUNK_SEGMENTS
        first = c * AUDIO_CHUNK_SEGMENTS
        start, stop, shift = audio_chunk_range(grid, first, k)
        cached = pres.dir / f"a{n}" / f"c{c}.adts"
        if cached.is_file():
            with contextlib.suppress(OSError):
                os.utime(cached)
            packets = adts_packets(await asyncio.to_thread(cached.read_bytes))
            return packets[start:stop], shift
        name = f"{pres.key}:{pres.variant}:a{n}:c{c}"
        job = self._chunk_jobs.get(name)
        if job is None:
            count = min(AUDIO_CHUNK_SEGMENTS, grid.count - first)
            cmd = audio_encode_cmd(pres.path, pres.info, a, grid, first, count, pres.t0)
            job = self._chunk_jobs[name] = _ChunkJob()
            job.task = asyncio.create_task(self._run_chunk(job, cmd, cached, name))
        packets = await job.wait_for(stop)
        return packets[start:stop], shift

    async def audio_segment(self, pres: Presentation, n: int, k: int) -> pathlib.Path:
        a = pres.info.audio[n]
        grid = pres.audio_grids[n]

        async def produce() -> dict[str, bytes]:
            copy = hls_audio_is_copy(a)
            if copy:
                raw = await self._run(audio_copy_cmd(pres.path, pres.info, a, grid, k))
                init, media = split_fmp4(raw)
                shift = round((a.start - pres.t0) * grid.sample_rate)
            else:
                packets, shift = await self._audio_packets(pres, n, k)
                if not packets:
                    raise RuntimeError("audio chunk has no packets for this segment")
                raw = await self._run(audio_mux_cmd(), stdin=b"".join(packets))
                init = await self._encoder_init(grid.sample_rate, 1 if a.channels == 1 else 2)
                media = split_fmp4(raw)[1]
            target = grid.seg_start_samples(k) + shift + round(TIMELINE_OFFSET * grid.sample_rate)
            return {
                f"a{n}/{k}.m4s": set_first_base_time(media, max(0, target)),
                f"a{n}/init.mp4": init,
            }

        return await self._cached(pres, f"a{n}/{k}.m4s", produce)

    async def _encoder_init(self, sample_rate: int, channels: int) -> bytes:
        key = (sample_rate, channels)
        if key not in self._encoder_inits:
            raw = await self._run(audio_init_cmd(sample_rate, channels))
            self._encoder_inits[key] = split_fmp4(raw)[0]
        return self._encoder_inits[key]

    async def audio_init(self, pres: Presentation, n: int) -> pathlib.Path:
        target = pres.dir / f"a{n}" / "init.mp4"
        if not target.is_file():
            await self.audio_segment(pres, n, 0)
        return target

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
        for p in [*self.root.glob("*/*/*/*.m4s"), *self.root.glob("*/*/*/*.adts")]:
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
        return FileResponse(
            path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=600"}
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
        return build_plan(download_id, info, duration)

    @router.get("/local/{download_id}/{variant}/master.m3u8", dependencies=auth)
    async def local_master(download_id: str, variant: str):
        pres = await presentation(download_id, variant)
        return playlist(master_playlist(pres.info, pres.variant))

    @router.get("/local/{download_id}/{variant}/v.m3u8", dependencies=auth)
    async def local_video_playlist(download_id: str, variant: str):
        pres = await presentation(download_id, variant)
        segs = [(f"v/{i}.m4s", s.duration) for i, s in enumerate(pres.video)]
        return playlist(media_playlist("v/init.mp4", segs))

    @router.get("/local/{download_id}/{variant}/a{n}.m3u8", dependencies=auth)
    async def local_audio_playlist(download_id: str, variant: str, n: int):
        pres = await presentation(download_id, variant)
        if not 0 <= n < len(pres.audio_grids):
            raise HTTPException(404, "No such audio track")
        grid = pres.audio_grids[n]
        segs = [(f"a{n}/{k}.m4s", grid.seg_duration(k)) for k in range(grid.count)]
        return playlist(media_playlist(f"a{n}/init.mp4", segs))

    @router.get("/local/{download_id}/{variant}/v/init.mp4", dependencies=auth)
    async def local_video_init(download_id: str, variant: str):
        pres = await presentation(download_id, variant)
        return await produce(get_service().video_init(pres))

    @router.get("/local/{download_id}/{variant}/v/{k}.m4s", dependencies=auth)
    async def local_video_segment(download_id: str, variant: str, k: int):
        pres = await presentation(download_id, variant)
        if not 0 <= k < len(pres.video):
            raise HTTPException(404, "No such segment")
        return await produce(get_service().video_segment(pres, k))

    @router.get("/local/{download_id}/{variant}/a{n}/init.mp4", dependencies=auth)
    async def local_audio_init(download_id: str, variant: str, n: int):
        pres = await presentation(download_id, variant)
        if not 0 <= n < len(pres.audio_grids):
            raise HTTPException(404, "No such audio track")
        return await produce(get_service().audio_init(pres, n))

    @router.get("/local/{download_id}/{variant}/a{n}/{k}.m4s", dependencies=auth)
    async def local_audio_segment(download_id: str, variant: str, n: int, k: int):
        pres = await presentation(download_id, variant)
        if not 0 <= n < len(pres.audio_grids) or not 0 <= k < pres.audio_grids[n].count:
            raise HTTPException(404, "No such segment")
        return await produce(get_service().audio_segment(pres, n, k))

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
