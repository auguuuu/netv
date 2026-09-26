"""Tests for local_media.py: instant seekable playback of finished downloads.

Pure parts (codec decision, keyframe / segment plan, playlists, fMP4 patching) are unit tested.
The rest runs against small synthetic media generated with ffmpeg: every rendition is fetched
through the real routes, decoded again with ffmpeg and checked for continuity, and random seeks
are served without any restarted process.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import array
import asyncio
import json
import random
import re
import shutil
import struct
import subprocess
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

from local_media import (
    AAC_FRAME,
    TIMELINE_OFFSET,
    AdtsStream,
    AudioInfo,
    KeyframeIndex,
    LocalMedia,
    MediaInfo,
    SubInfo,
    VideoInfo,
    adts_packets,
    audio_chunk_range,
    audio_chunk_slice,
    audio_copy_cmd,
    audio_encode_cmd,
    audio_track_names,
    build_plan,
    direct_play_decision,
    first_base_time,
    first_composition_offset,
    hls_audio_is_copy,
    master_playlist,
    media_playlist,
    parse_probe,
    parse_scan,
    plan_audio_grid,
    plan_video_copy,
    plan_video_grid,
    set_first_base_time,
    shift_vtt,
    split_fmp4,
    video_codec_string,
    video_copy_cmd,
)

import downloads
import local_media


HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")


# =============================================================================
# Helpers for the pure tests
# =============================================================================


def video(codec: str = "h264", **kw: Any) -> VideoInfo:
    base: dict[str, Any] = dict(
        index=0, codec=codec, profile="High", level=40, width=1920, height=1080,
        pix_fmt="yuv420p", fps=24.0, start=0.0, bit_depth=8,
    )  # fmt: skip
    base.update(kw)
    return VideoInfo(**base)


def audio(codec: str = "aac", ordinal: int = 0, **kw: Any) -> AudioInfo:
    base: dict[str, Any] = dict(
        index=ordinal + 1, ordinal=ordinal, codec=codec, profile="LC", channels=2,
        sample_rate=48000, start=0.0, lang="eng", title="", default=ordinal == 0,
    )  # fmt: skip
    base.update(kw)
    return AudioInfo(**base)


def media(
    container: str = "mkv",
    v: VideoInfo | None = None,
    a: tuple[AudioInfo, ...] = (),
    subs: tuple[SubInfo, ...] = (),
    **kw: Any,
) -> MediaInfo:
    base: dict[str, Any] = dict(
        container=container, ext=container, duration=600.0, start=0.0, bit_rate=8_000_000,
        size=600_000_000, video=v if v is not None else video(), audio=a, subs=subs,
    )  # fmt: skip
    base.update(kw)
    return MediaInfo(**base)


# =============================================================================
# Probe parsing
# =============================================================================

PROBE = {
    "format": {"format_name": "matroska,webm", "duration": "7200.5", "start_time": "-0.021",
               "bit_rate": "9000000", "size": "8900000000"},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc", "profile": "Main 10", "level": 153,
         "width": 3840, "height": 1600, "pix_fmt": "yuv420p10le", "avg_frame_rate": "24000/1001",
         "start_time": "0.000000", "bits_per_raw_sample": "10"},
        {"index": 1, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1},
         "width": 600, "height": 900},
        {"index": 2, "codec_type": "audio", "codec_name": "eac3", "channels": 6, "sample_rate": "48000",
         "start_time": "0.000000", "tags": {"language": "eng", "title": "Original"},
         "disposition": {"default": 1}},
        {"index": 3, "codec_type": "audio", "codec_name": "aac", "profile": "LC", "channels": 2,
         "sample_rate": "48000", "start_time": "0.010", "tags": {"LANGUAGE": "fre"}},
        {"index": 4, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
        {"index": 5, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "fre"}},
    ],
}  # fmt: skip


def test_parse_probe_reads_streams_and_ignores_cover_art():
    info = parse_probe(PROBE, ".mkv", 8_900_000_000)
    assert info.container == "mkv"
    assert info.duration == 7200.5
    assert info.start == pytest.approx(-0.021)
    assert info.video is not None
    assert info.video.index == 0 and info.video.codec == "hevc"  # not the mjpeg cover
    assert info.video.bit_depth == 10
    assert info.video.fps == pytest.approx(23.976, abs=0.001)
    assert [a.codec for a in info.audio] == ["eac3", "aac"]
    assert [a.ordinal for a in info.audio] == [0, 1]
    assert info.audio[0].default and info.audio[0].title == "Original"
    assert info.audio[1].lang == "fre"  # tag keys are case insensitive
    assert [(s.ordinal, s.is_text) for s in info.subs] == [(0, True), (1, False)]


def test_parse_probe_survives_empty_input():
    info = parse_probe({}, "avi")
    assert info.video is None and info.audio == () and info.container == "avi"


@pytest.mark.parametrize(
    ("format_name", "ext", "expected"),
    [
        ("mov,mp4,m4a,3gp,3g2,mj2", "mp4", "mp4"),
        ("matroska,webm", "mkv", "mkv"),
        ("matroska,webm", "webm", "webm"),
        ("avi", "avi", "avi"),
        ("mpegts", "ts", "mpegts"),
    ],
)
def test_container_kind(format_name: str, ext: str, expected: str):
    info = parse_probe({"format": {"format_name": format_name}}, ext)
    assert info.container == expected


# =============================================================================
# Direct play decision and codec strings
# =============================================================================


def test_direct_play_mp4_h264_aac_offers_mime():
    d = direct_play_decision(media("mp4", a=(audio(),)))
    assert d.ok
    assert d.mimes == ('video/mp4; codecs="avc1.640028, mp4a.40.2"',)


def test_direct_play_mkv_also_offers_webm_label():
    d = direct_play_decision(media("mkv", v=video("vp9"), a=(audio("opus"),)))
    assert d.ok and len(d.mimes) == 2
    assert d.mimes[0].startswith("video/x-matroska")


@pytest.mark.parametrize("codec", ["ac3", "eac3", "dts", "truehd", "pcm_s16le"])
def test_direct_play_rejects_audio_browsers_cannot_decode(codec: str):
    d = direct_play_decision(media("mkv", a=(audio("aac"), audio(codec, 1))))
    assert not d.ok and codec in d.reason


@pytest.mark.parametrize("container", ["avi", "mpegts", "flv", "asf"])
def test_direct_play_rejects_unfriendly_containers(container: str):
    assert not direct_play_decision(media(container, a=(audio(),))).ok


def test_direct_play_rejects_old_video_codecs_and_10bit_h264():
    assert not direct_play_decision(media("mp4", v=video("mpeg4"), a=(audio(),))).ok
    assert not direct_play_decision(media("mkv", v=video("vc1"), a=(audio(),))).ok
    ten = video("h264", pix_fmt="yuv420p10le", bit_depth=10, profile="High 10")
    assert not direct_play_decision(media("mkv", v=ten, a=(audio(),))).ok
    # 10 bit HEVC is fine (hardware decoders), the client still checks canPlayType
    assert direct_play_decision(media("mkv", v=video("hevc", bit_depth=10), a=(audio(),))).ok


def test_direct_play_needs_a_video_stream():
    assert not direct_play_decision(media("mp4", v=None, video=None)).ok  # type: ignore[arg-type]


def test_video_codec_strings():
    assert video_codec_string(video("h264", profile="High", level=41)) == "avc1.640029"
    assert video_codec_string(video("h264", profile="Main", level=31)) == "avc1.4D001F"
    assert (
        video_codec_string(video("h264", profile="Constrained Baseline", level=30)) == "avc1.42401E"
    )
    assert video_codec_string(video("hevc", level=120)) == "hvc1.1.6.L120.B0"
    assert video_codec_string(video("hevc", level=153, bit_depth=10)) == "hvc1.2.4.L153.B0"
    assert video_codec_string(video("vp9")) == "vp09.00.10.08"
    assert video_codec_string(video("av1", bit_depth=10)) == "av01.0.08M.10"


def test_hls_audio_copy_only_for_plain_aac_lc_up_to_stereo():
    assert hls_audio_is_copy(audio("aac"))
    assert not hls_audio_is_copy(audio("aac", channels=6))
    assert not hls_audio_is_copy(audio("aac", profile="HE-AAC"))
    assert not hls_audio_is_copy(audio("ac3"))


def test_build_plan_lists_every_way_and_text_subtitles_only():
    info = media(
        "mkv",
        a=(audio("aac", 0, lang="jpn"), audio("aac", 1, lang="fre")),
        subs=(
            SubInfo(3, 0, "subrip", "eng", "", True, False),
            SubInfo(4, 1, "hdmv_pgs_subtitle", "fre", "", False, False),
        ),
    )
    plan = build_plan("abc", info, 600.0)
    assert plan["duration"] == 600.0
    assert [s["url"] for s in plan["subs"]] == ["/local/abc/sub/0.vtt"]
    assert [h["variant"] for h in plan["hls"]] == ["copy", "x264"]
    assert plan["hls"][0]["master"] == "/local/abc/copy/master.m3u8"
    assert plan["direct"]["url"] == "/downloads/files/abc"
    assert [a["lang"] for a in plan["audio"]] == ["jpn", "fre"]
    json.dumps(plan)  # serialisable


def test_build_plan_without_copyable_video_only_offers_x264():
    plan = build_plan("x", media("avi", v=video("mpeg4"), a=(audio("mp3"),)), 100.0)
    assert [h["variant"] for h in plan["hls"]] == ["x264"]
    assert plan["direct"]["ok"] is False


# =============================================================================
# Keyframe scan and segment plan
# =============================================================================


def kfs(times: list[float], end: float, frame: float = 0.04, per: int = 100) -> KeyframeIndex:
    return KeyframeIndex(
        tuple((t, i * per) for i, t in enumerate(times)), len(times) * per, end, frame
    )


def test_parse_scan_keeps_decode_order_indexes_and_frame_duration():
    lines = [
        "0.000000,-0.083000,K__",
        "0.167000,-0.042000,___",
        "0.083000,0.000000,___",
        "0.042000,0.041000,___",
        "5.000000,4.958000,K__",
        "N/A,N/A,___",  # broken packet still counts as a packet
    ]
    kf = parse_scan(lines)
    assert kf.keyframes == ((0.0, 0), (5.0, 4))
    assert kf.packets == 6
    assert kf.frame_dur == pytest.approx(0.041, abs=0.002)
    assert kf.end == pytest.approx(5.0 + kf.frame_dur)


def test_plan_video_copy_cuts_on_keyframes_up_to_the_target():
    segs = plan_video_copy(kfs([0, 5, 10, 15, 20], 25))
    assert [(s.start, s.duration, s.packets) for s in segs] == [(0, 5, 100)] * 0 + [
        (0, 5, 100), (5, 5, 100), (10, 5, 100), (15, 5, 100), (20, 5, 100),
    ]  # fmt: skip
    # short GOPs are merged up to 6 s
    segs = plan_video_copy(kfs([0, 2, 4, 6, 8, 10, 12, 14], 16))
    assert [s.duration for s in segs] == [6, 6, 4]
    assert [s.packets for s in segs] == [300, 300, 200]
    # a GOP longer than the target is one long segment
    segs = plan_video_copy(kfs([0, 10, 20], 30))
    assert [s.duration for s in segs] == [10, 10, 10]


def test_plan_video_copy_segments_tile_the_timeline_without_gaps():
    rng = random.Random(3)
    times = [0.0]
    for _ in range(400):
        times.append(times[-1] + rng.choice([1.0, 2.0, 2.5, 4.0, 8.0]))
    kf = kfs(times, times[-1] + 3)
    segs = plan_video_copy(kf)
    assert segs[0].start == 0
    for a, b in zip(segs, segs[1:], strict=False):
        assert a.start + a.duration == pytest.approx(b.start, abs=1e-5)
        assert a.duration <= 8.0 + 1e-6  # never longer than the longest GOP
    assert segs[-1].start + segs[-1].duration == pytest.approx(times[-1] + 3, abs=1e-5)
    assert sum(s.packets for s in segs) == kf.packets  # every packet belongs to a segment


def test_plan_video_copy_single_keyframe_and_empty():
    segs = plan_video_copy(kfs([0], 100))
    assert len(segs) == 1 and segs[0].duration == 100
    assert plan_video_copy(KeyframeIndex((), 0, 0, 0.04)) == []


def test_plan_video_grid_and_audio_grid_cover_the_duration():
    grid = plan_video_grid(20.5, 6.0)
    assert [s.start for s in grid] == [0, 6, 12, 18]
    assert sum(s.duration for s in grid) == pytest.approx(20.5)
    for rate in (48000, 44100, 32000):
        g = plan_audio_grid(600.0, rate)
        total = sum(g.seg_duration(k) for k in range(g.count))
        assert total >= 600.0 and total < 600.0 + AAC_FRAME / rate + 1e-9
        assert g.frames_per_segment * AAC_FRAME / rate == pytest.approx(6.0, abs=0.03)
        assert g.seg_frames(0) == g.frames_per_segment


# =============================================================================
# Playlists
# =============================================================================


def extinf(text: str) -> list[float]:
    return [float(m) for m in re.findall(r"#EXTINF:([0-9.]+),", text)]


def test_media_playlist_is_a_complete_vod_playlist():
    text = media_playlist("v/init.mp4", [("v/0.m4s", 5.0), ("v/1.m4s", 6.4), ("v/2.m4s", 1.0)])
    lines = text.splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[-1] == "#EXT-X-ENDLIST"
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in lines
    assert '#EXT-X-MAP:URI="v/init.mp4"' in lines
    assert "#EXT-X-TARGETDURATION:7" in lines  # ceil of the longest segment
    assert extinf(text) == [5.0, 6.4, 1.0]
    assert lines.count("v/1.m4s") == 1


def test_master_playlist_declares_one_rendition_per_audio_track():
    info = media(
        "mkv",
        a=(audio("ac3", 0, lang="jpn", channels=6), audio("aac", 1, lang="fre", title="VF")),
    )
    text = master_playlist(info, "copy")
    media_lines = [line for line in text.splitlines() if line.startswith("#EXT-X-MEDIA")]
    assert len(media_lines) == 2
    assert 'LANGUAGE="ja"' in media_lines[0] and "DEFAULT=YES" in media_lines[0]
    assert 'NAME="VF"' in media_lines[1] and 'LANGUAGE="fr"' in media_lines[1]
    assert 'URI="a0.m3u8"' in media_lines[0] and 'URI="a1.m3u8"' in media_lines[1]
    assert 'CHANNELS="2"' in media_lines[0]  # 5.1 is downmixed
    stream = next(line for line in text.splitlines() if line.startswith("#EXT-X-STREAM-INF"))
    assert 'AUDIO="aud"' in stream and "avc1.640028" in stream and "mp4a.40.2" in stream
    assert master_playlist(info, "x264").count("avc1.640028") == 1


def test_master_playlist_without_audio_has_no_audio_group():
    text = master_playlist(media("mp4"), "copy")
    assert "#EXT-X-MEDIA" not in text and "AUDIO=" not in text


def test_audio_track_names_are_unique_and_never_empty():
    names = audio_track_names(
        (audio(lang="eng"), audio(ordinal=1, lang="eng"), audio(ordinal=2, lang=""))
    )
    assert len(set(names)) == 3 and all(names)


# =============================================================================
# fMP4 helpers
# =============================================================================


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def fake_fragment(base: int, cts_first: int, version: int = 1) -> bytes:
    tfdt = box(b"tfdt", bytes([1, 0, 0, 0]) + struct.pack(">Q", base))
    tfhd = box(b"tfhd", bytes([0, 2, 0, 0]) + struct.pack(">I", 1))
    fmt = ">i" if version else ">I"
    trun = box(
        b"trun",
        bytes([version, 0, 0x0B, 0x00])  # duration + size + cts
        + struct.pack(">I", 2)
        + struct.pack(">II", 100, 10) + struct.pack(fmt, cts_first)
        + struct.pack(">II", 100, 10) + struct.pack(fmt, 0),
    )  # fmt: skip
    moof = box(b"moof", box(b"mfhd", bytes(8)) + box(b"traf", tfhd + tfdt + trun))
    return moof + box(b"mdat", bytes(20))


def test_fmp4_helpers_read_and_shift_timestamps():
    init = box(b"ftyp", b"isom") + box(b"moov", b"x" * 8)
    data = init + fake_fragment(1000, 1328) + fake_fragment(1200, 0)
    head, frags = split_fmp4(data)
    assert head == init
    assert first_base_time(frags) == 1000
    assert first_composition_offset(frags) == 1328
    moved = set_first_base_time(frags, 20000)
    assert first_base_time(moved) == 20000
    assert len(moved) == len(frags)
    # the second fragment keeps its distance to the first one
    again = set_first_base_time(moved, 1000)
    assert again == frags


def test_fmp4_signed_and_unsigned_composition_offsets():
    assert first_composition_offset(fake_fragment(0, -672, version=1)) == -672
    assert first_composition_offset(fake_fragment(0, 672, version=0)) == 672
    assert first_composition_offset(box(b"mdat", b"")) == 0


def test_split_fmp4_rejects_garbage():
    with pytest.raises(ValueError):
        split_fmp4(box(b"ftyp", b"isom"))
    with pytest.raises(ValueError):
        split_fmp4(b"\x00\x00\x00\xff" + b"moof" + b"\x00" * 3)


def test_adts_stream_parses_frames_split_anywhere():
    def frame(payload: int) -> bytes:
        length = 7 + payload
        hdr = bytes(
            [
                0xFF,
                0xF1,
                0x4C,
                0x80 | (length >> 11),
                (length >> 3) & 0xFF,
                ((length & 7) << 5) | 0x1F,
                0xFC,
            ]
        )
        return hdr + bytes(payload)

    data = b"".join(frame(n) for n in (10, 300, 5, 40))
    for step in (1, 3, 7, 50, len(data)):
        stream = AdtsStream()
        for i in range(0, len(data), step):
            stream.feed(data[i : i + step])
        assert [len(p) for p in stream.packets] == [17, 307, 12, 47], step
    with pytest.raises(ValueError):
        AdtsStream().feed(b"\x00" * 20)


def test_audio_chunk_range_matches_slice_and_tiles_the_chunk():
    grid = plan_audio_grid(60.0, 48000)
    f = grid.frames_per_segment
    pk = [bytes([i % 251]) * 8 for i in range(4 * f + 10)]
    for first in (0, 4):
        stops = []
        for k in range(first, min(first + 4, grid.count)):
            start, stop, shift = audio_chunk_range(grid, first, k)
            assert audio_chunk_slice(pk, grid, first, k) == (pk[start:stop], shift)
            stops.append((start, stop))
        # consecutive segments are consecutive packets: no packet lost or repeated
        for (_, a), (b, _) in zip(stops, stops[1:], strict=False):
            assert a == b


def test_adts_packets_splits_frames():
    def frame(payload: int) -> bytes:
        length = 7 + payload
        hdr = bytes(
            [
                0xFF,
                0xF1,
                0x4C,
                0x80 | (length >> 11),
                (length >> 3) & 0xFF,
                ((length & 7) << 5) | 0x1F,
                0xFC,
            ]
        )
        return hdr + bytes(payload)

    data = frame(10) + frame(20) + frame(5)
    assert [len(p) for p in adts_packets(data)] == [17, 27, 12]
    with pytest.raises(ValueError):
        adts_packets(b"\x00" * 20)


# =============================================================================
# WebVTT and command builders
# =============================================================================


def test_shift_vtt_moves_cues_and_keeps_the_rest():
    text = "WEBVTT\n\n00:00:01.000 --> 00:00:04.500 line:90%\nHello 12:00:00.000\n\n01:02:03.004 --> 01:02:05.000\nBye\n"
    out = shift_vtt(text, -0.021)
    assert "00:00:00.979 --> 00:00:04.479 line:90%" in out
    assert "01:02:02.983 --> 01:02:04.979" in out
    assert "Hello 12:00:00.000" in out  # cue text is not a timing line
    assert shift_vtt(text, 0.0004) == text
    assert "00:00:00.000 --> 00:00:00.500" in shift_vtt("00:00:00.100 --> 00:00:01.000\n", -0.5)


def test_video_copy_cmd_seeks_before_input_and_counts_packets():
    info = media("mkv", v=video("hevc"), start=-0.021)
    seg = plan_video_copy(kfs([0.0, 5.0, 10.0], 15.0))[1]
    cmd = video_copy_cmd("/x/f.mkv", info, seg)
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "5.021000"  # relative to the container start
    assert cmd[cmd.index("-frames:v") + 1] == "100"
    assert "-copypriorss" in cmd and "hvc1" in cmd and "-c:v" in cmd
    first = video_copy_cmd("/x/f.mkv", media(), plan_video_copy(kfs([0.0, 5.0], 10.0))[0])
    assert "-ss" not in first  # segment 0 needs no seek


def test_audio_commands_use_frame_exact_grids():
    info = media("mkv", a=(audio("ac3", channels=6),), start=-0.02)
    a = info.audio[0]
    grid = plan_audio_grid(600.0, 48000)
    cmd = audio_encode_cmd("/x/f.mkv", info, a, grid, 16, 8, 0.0)
    # priming packet + 2 warm up frames, then the frames of the 8 segments
    assert cmd[cmd.index("-frames:a") + 1] == str(3 + 8 * grid.frames_per_segment)
    assert "-ac" in cmd and cmd[cmd.index("-ac") + 1] == "2"  # 5.1 is downmixed
    seek = float(cmd[cmd.index("-ss") + 1])
    start = 16 * grid.frames_per_segment * AAC_FRAME / 48000
    assert seek == pytest.approx(0.02 + start - 2 * AAC_FRAME / 48000)
    first_chunk = audio_encode_cmd("/x/f.mkv", info, a, grid, 0, 8, 0.0)
    assert first_chunk[first_chunk.index("-frames:a") + 1] == str(1 + 8 * grid.frames_per_segment)
    copy_cmd = audio_copy_cmd("/x/f.mkv", info, audio("aac"), grid, 2)
    assert "copy" in copy_cmd and copy_cmd[copy_cmd.index("-frames:a") + 1] == str(
        grid.frames_per_segment
    )


def test_audio_chunk_slice_maps_segments_to_packets():
    grid = plan_audio_grid(30.0, 48000)  # 5 segments, the last one shorter
    f = grid.frames_per_segment
    assert grid.count == 6 and grid.seg_frames(5) < f
    pk = [bytes([i % 256]) * 8 for i in range(3 * f + 10)]
    # chunk starting at segment 0: priming packet first, segment 0 carries it
    seg0, shift0 = audio_chunk_slice(pk, grid, 0, 0)
    assert seg0 == pk[: f + 1] and shift0 == -AAC_FRAME
    seg1, shift1 = audio_chunk_slice(pk, grid, 0, 1)
    assert seg1 == pk[f + 1 : 2 * f + 1] and shift1 == 0
    # chunk starting later: 3 lead in packets (priming + 2 warm up) are skipped
    seg3, shift3 = audio_chunk_slice(pk, grid, 3, 3)
    assert seg3 == pk[3 : 3 + f] and shift3 == 0
    seg4, _ = audio_chunk_slice(pk, grid, 3, 4)
    assert seg4 == pk[3 + f : 3 + 2 * f]
    last, _ = audio_chunk_slice(pk[: 3 + 2 * f + 5], grid, 3, 5)
    assert len(last) == 5 or len(last) == grid.seg_frames(5)


# =============================================================================
# Synthetic media
# =============================================================================

DURATION = 24
SRC = f"testsrc2=size=320x180:rate=24:duration={DURATION}"
# tones plus noise, so that an alignment against the source is unambiguous
TONE = (
    "aevalsrc='0.3*sin(2*PI*(200+150*sin(2*PI*0.3*t))*t)+0.2*sin(2*PI*880*t)+0.15*(random(0)-0.5)'"
    f":s=48000:d={DURATION}"
)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True, timeout=180)


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("dl")
    if not HAVE_FFMPEG:
        return d
    x264 = ["-c:v", "libx264", "-preset", "veryfast", "-bf", "3", "-g", "48", "-keyint_min", "24"]
    x264 += ["-pix_fmt", "yuv420p"]
    src = ["-f", "lavfi", "-i", SRC, "-f", "lavfi", "-i", TONE]
    (d / "s1.srt").write_text(
        "1\n00:00:01,000 --> 00:00:04,000\nHello\n\n2\n00:00:20,000 --> 00:00:22,000\nBye\n"
    )
    (d / "s2.srt").write_text("1\n00:00:01,000 --> 00:00:04,000\nBonjour\n")
    ffmpeg(
        *src,
        *x264,
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-shortest",
        "-movflags",
        "+faststart",
        str(d / "plain.mp4"),
    )
    ffmpeg(
        *src, "-f", "lavfi", "-i", TONE.replace("sin(2*PI*880", "sin(2*PI*660"),
        "-i", str(d / "s1.srt"), "-i", str(d / "s2.srt"),
        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3", "-map", "4",
        *x264, "-c:a", "aac", "-b:a", "96k", "-c:s", "srt",
        "-metadata:s:a:0", "language=jpn", "-metadata:s:a:1", "language=fre",
        "-metadata:s:s:0", "language=eng", "-metadata:s:s:1", "language=fre",
        str(d / "multi.mkv"),
    )  # fmt: skip
    ffmpeg(*src, *x264, "-c:a", "ac3", "-b:a", "192k", "-shortest", str(d / "ac3.mkv"))
    ffmpeg(
        *src, "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "keyint=48:min-keyint=24:open-gop=1",
        "-tag:v", "hvc1", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(d / "hevc.mkv"),
    )  # fmt: skip
    ffmpeg(
        *src, "-c:v", "mpeg4", "-g", "50", "-c:a", "libmp3lame", "-shortest", str(d / "legacy.avi")
    )
    for name in ("s1.srt", "s2.srt"):
        (d / name).unlink()
    return d


ENTRIES = {
    "plain": "plain.mp4", "multi": "multi.mkv", "ac3": "ac3.mkv", "hevc": "hevc.mkv",
    "legacy": "legacy.avi",
}  # fmt: skip


@pytest.fixture
def env(media_dir: Path, tmp_path: Path) -> Iterator[tuple[TestClient, LocalMedia]]:
    mgr = downloads.DownloadManager(media_dir, lambda e: "", source_limits=lambda s: (1, 1))
    for did, name in ENTRIES.items():
        p = media_dir / name
        mgr._entries[did] = downloads.Entry(
            id=did, kind="movie", item_id="1", source_id="", title=did, filename=name,
            ext=p.suffix[1:], status="done", bytes_done=p.stat().st_size, bytes_total=p.stat().st_size,
        )  # fmt: skip
    # a download still in progress: only its .part file exists
    (media_dir / "unfinished.mkv.part").write_bytes(b"partial")
    mgr._entries["busy"] = downloads.Entry(
        id="busy", kind="movie", item_id="2", source_id="", title="busy",
        filename="unfinished.mkv", ext="mkv", status="downloading",
    )  # fmt: skip
    downloads.set_manager(mgr)
    svc = LocalMedia(tmp_path / "cache", max_bytes=512 * 1024 * 1024)
    local_media.set_service(svc)
    app = FastAPI()
    app.include_router(local_media.build_router(lambda: {"sub": "tester"}))
    app.include_router(downloads.build_router(lambda: {"sub": "tester"}))
    with TestClient(app) as client:
        yield client, svc
    downloads.set_manager(None)
    local_media.set_service(None)


def probe_json(path: Path, *args: str) -> list[list[str]]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", *args, "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return [line.split(",") for line in out.strip().splitlines() if line]


def fetch_all(client: TestClient, did: str, variant: str, out: Path) -> dict[str, Any]:
    """Download the master, every playlist, init and segment into ``out``; returns stats."""
    base = f"/local/{did}/{variant}"
    master = client.get(f"{base}/master.m3u8")
    assert master.status_code == 200, master.text
    (out / "master.m3u8").write_text(master.text)
    playlists = ["v.m3u8", *re.findall(r'URI="(a\d+\.m3u8)"', master.text)]
    stats: dict[str, Any] = {"master": master.text, "playlists": {}}
    for name in playlists:
        text = client.get(f"{base}/{name}").text
        (out / name).write_text(text)
        stats["playlists"][name] = text
        init = re.search(r'#EXT-X-MAP:URI="([^"]+)"', text).group(1)  # type: ignore[union-attr]
        for uri in [init, *[m for m in re.findall(r"^([^#].*\.m4s)$", text, re.M)]]:
            r = client.get(f"{base}/{uri}")
            assert r.status_code == 200, (uri, r.status_code)
            (out / uri).parent.mkdir(parents=True, exist_ok=True)
            (out / uri).write_bytes(r.content)
    return stats


HLS_ARGS = ["-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto,data"]


def decode_clean(playlist: Path) -> str:
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "warning", *HLS_ARGS, "-i", str(playlist), "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    return r.stderr.strip()


def packet_times(playlist: Path, stream: str) -> list[float]:
    rows = probe_json(
        playlist, *HLS_ARGS, "-select_streams", stream, "-show_entries", "packet=pts_time"
    )
    return sorted(float(r[0]) for r in rows if r[0] not in ("N/A", ""))


# =============================================================================
# Integration: plan, playlists, segments
# =============================================================================


@needs_ffmpeg
def test_plan_describes_multi_audio_and_text_subtitles(env):
    client, _ = env
    plan = client.get("/api/local/multi/plan").json()
    assert plan["container"] == "mkv"
    assert [(a["lang"], a["codec"]) for a in plan["audio"]] == [("jpn", "aac"), ("fre", "aac")]
    assert [s["lang"] for s in plan["subs"]] == ["eng", "fre"]
    assert plan["duration"] == pytest.approx(DURATION, abs=0.3)
    assert plan["direct"]["ok"] and [h["variant"] for h in plan["hls"]] == ["copy", "x264"]
    # the raw file route keeps serving Range requests for direct play
    r = client.get("/downloads/files/multi", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100


@needs_ffmpeg
def test_plan_marks_incompatible_files(env):
    client, _ = env
    ac3 = client.get("/api/local/ac3/plan").json()
    assert ac3["direct"]["ok"] is False and "ac3" in ac3["direct"]["reason"]
    legacy = client.get("/api/local/legacy/plan").json()
    assert legacy["direct"]["ok"] is False
    assert [h["variant"] for h in legacy["hls"]] == ["x264"]  # mpeg4 cannot be copied
    plain = client.get("/api/local/plain/plan").json()
    assert plain["direct"]["ok"] and plain["direct"]["mimes"][0].startswith("video/mp4")


@needs_ffmpeg
@pytest.mark.parametrize("did", ["multi", "ac3", "hevc", "legacy", "plain"])
def test_complete_vod_playlists_and_gapless_segments(env, tmp_path: Path, did: str):
    client, _ = env
    stats = fetch_all(client, did, "copy", tmp_path)
    for name, text in stats["playlists"].items():
        assert text.rstrip().endswith("#EXT-X-ENDLIST"), name
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in text
    v_total = sum(extinf(stats["playlists"]["v.m3u8"]))
    assert v_total == pytest.approx(DURATION, abs=0.3)
    for name, text in stats["playlists"].items():
        if name != "v.m3u8":
            assert sum(extinf(text)) == pytest.approx(v_total, abs=0.1)

    # every rendition decodes without a single warning
    for name in stats["playlists"]:
        assert decode_clean(tmp_path / name) == "", name

    # video: one continuous timeline starting at the shared offset, no gaps or overlaps
    times = packet_times(tmp_path / "v.m3u8", "v:0")
    assert times[0] == pytest.approx(TIMELINE_OFFSET, abs=0.002)
    steps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert max(steps) < 0.1 and min(steps) > 0
    assert len(times) == DURATION * 24  # every source frame, RASL leading pictures included

    # audio: contiguous AAC frames from the very same origin
    for name in stats["playlists"]:
        if name == "v.m3u8":
            continue
        atimes = packet_times(tmp_path / name, "a:0")
        astep = [b - a for a, b in zip(atimes, atimes[1:], strict=False)]
        assert atimes[0] == pytest.approx(TIMELINE_OFFSET, abs=0.05)
        assert max(astep) < 0.0235 and min(astep) > 0.0205, name  # 1024 samples at 44.1/48 kHz


@needs_ffmpeg
def test_multi_audio_master_has_one_rendition_per_track(env):
    client, _ = env
    master = client.get("/local/multi/copy/master.m3u8").text
    tracks = re.findall(r"#EXT-X-MEDIA:TYPE=AUDIO.*", master)
    assert len(tracks) == 2
    assert 'LANGUAGE="ja"' in tracks[0] and "DEFAULT=YES" in tracks[0]
    assert 'LANGUAGE="fr"' in tracks[1]
    for uri in ("a0.m3u8", "a1.m3u8"):
        assert client.get(f"/local/multi/copy/{uri}").status_code == 200
    assert client.get("/local/multi/copy/a2.m3u8").status_code == 404
    assert client.get("/local/multi/copy/a2/0.m4s").status_code == 404
    assert client.get("/local/multi/copy/a0/9999.m4s").status_code == 404
    assert client.get("/local/multi/copy/v/9999.m4s").status_code == 404


@needs_ffmpeg
def test_x264_variant_is_a_playable_fallback_for_copyable_video(env, tmp_path: Path):
    client, _ = env
    stats = fetch_all(client, "plain", "x264", tmp_path)
    assert "avc1.640028" in stats["master"]
    assert decode_clean(tmp_path / "v.m3u8") == ""
    times = packet_times(tmp_path / "v.m3u8", "v:0")
    assert len(times) == DURATION * 24
    assert times[0] == pytest.approx(TIMELINE_OFFSET, abs=0.05)


def read_pcm(playlist_or_file: Path, *extra: str) -> array.array:
    args = ["ffmpeg", "-nostdin", "-v", "error", *extra, "-i", str(playlist_or_file)]
    args += ["-map", "0:a:0", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"]
    raw = subprocess.run(args, capture_output=True, check=True, timeout=120).stdout
    out = array.array("h")
    out.frombytes(raw[: len(raw) // 2 * 2])
    return out


@needs_ffmpeg
def test_encoded_audio_segments_join_without_audible_seams(
    env, media_dir: Path, tmp_path: Path, monkeypatch
):
    """Encoded AAC must decode like one continuous stream, also across chunk joins."""
    monkeypatch.setattr(local_media, "AUDIO_CHUNK_SEGMENTS", 2)  # chunk joins at segment 2
    client, _ = env
    fetch_all(client, "ac3", "copy", tmp_path)
    got = read_pcm(tmp_path / "a0.m3u8", *HLS_ARGS)
    want = read_pcm(media_dir / "ac3.mkv")
    n = min(len(got), len(want)) - 6000
    assert n > 40 * 1024
    seg = 281 * AAC_FRAME

    def mse(lo: int, hi: int, shift: int, step: int = 1) -> float:
        return sum((got[i + shift] - want[i]) ** 2 for i in range(lo, hi, step)) / len(
            range(lo, hi, step)
        )

    def best(lo: int, around: int, span: int) -> int:
        return min(range(around - span, around + span + 1), key=lambda s: mse(lo, lo + 1500, s, 3))

    # one alignment for the whole track (source noise makes it unambiguous) ...
    shift = min(range(-4000, 4000), key=lambda s: mse(30000, 30600, s, 3))
    power = sum(w * w for w in want[30000:60000:5]) / 6000
    assert mse(30000, 60000, shift, 5) < 0.02 * power  # codec noise only (white noise: worst case)
    # chunk 1 (segments 2 and 3) is a separate encode: it may sit a few samples off chunk 0
    # (Matroska timestamps are rounded to 1 ms, a seek lands on those), but never by more
    other = best(2 * seg + 3000, shift, 40)
    assert abs(other - shift) <= 32, other - shift
    assert mse(2 * seg + 3000, 2 * seg + 4500, other) < 0.1 * power
    # ... and inside a chunk (joins at segments 1 and 3) the offset does not move at all: no
    # missing or repeated sample, no click
    for join, ref in ((seg, shift), (3 * seg, other)):
        for lo in (join - 3200, join - 1700, join + 200, join + 3000):
            assert best(lo, ref, 2) == ref, (join, lo)
            assert mse(lo, lo + 1500, ref) < 0.1 * power, (join, lo)
    # the alignment is absolute: same origin as the timeline the video uses
    first = packet_times(tmp_path / "a0.m3u8", "a:0")[0]
    assert first == pytest.approx(TIMELINE_OFFSET - AAC_FRAME / 48000, abs=0.002)  # priming frame


@needs_ffmpeg
def test_subtitles_are_complete_webvtt_on_the_video_timeline(env):
    client, _ = env
    en = client.get("/local/multi/sub/0.vtt")
    assert en.status_code == 200 and en.headers["content-type"].startswith("text/vtt")
    assert en.text.startswith("WEBVTT")
    assert "Hello" in en.text and "Bye" in en.text
    assert "00:00:01.000 --> 00:00:04.000" in en.text or "00:00:00.9" in en.text
    assert "Bonjour" in client.get("/local/multi/sub/1.vtt").text
    assert client.get("/local/multi/sub/2.vtt").status_code == 404
    assert client.get("/local/plain/sub/0.vtt").status_code == 404


# =============================================================================
# Integration: seeking never restarts anything
# =============================================================================


@needs_ffmpeg
def test_random_seeks_are_answered_from_independent_segments(env):
    client, svc = env
    pres = asyncio.run(svc.presentation(Path(downloads.get_manager().file_for("multi")[1]), "copy"))
    count = len(pres.video)
    assert count >= 4
    assert client.get("/local/multi/copy/v.m3u8").status_code == 200  # planning is done
    order = list(range(count)) * 2
    random.Random(7).shuffle(order)
    lat = []
    for k in order:
        t = time.perf_counter()
        r = client.get(f"/local/multi/copy/v/{k}.m4s")
        lat.append(time.perf_counter() - t)
        assert r.status_code == 200 and len(r.content) > 1000
    # each distinct segment is produced exactly once, order and repetition do not matter
    assert svc.ffmpeg_runs <= 2 + count + 2  # probe + scan + one run per segment (+ init)
    assert max(lat) < 3.0


@needs_ffmpeg
def test_concurrent_requests_share_one_ffmpeg_run(env):
    client, svc = env
    client.get("/local/ac3/copy/v.m3u8")
    before = svc.ffmpeg_runs
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: client.get("/local/ac3/copy/v/2.m4s"), range(8)))
    assert all(r.status_code == 200 for r in results)
    assert len({r.content for r in results}) == 1
    assert svc.ffmpeg_runs - before == 1


@needs_ffmpeg
def test_encoded_audio_segments_of_a_chunk_share_one_encode(env):
    client, svc = env
    client.get("/local/ac3/copy/a0.m3u8")
    before = svc.ffmpeg_runs
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda k: client.get(f"/local/ac3/copy/a0/{k}.m4s"), range(4)))
    assert all(r.status_code == 200 and len(r.content) > 1000 for r in results)
    # 1 chunk encode + 1 decoder config + 4 cheap muxes: not one encode per segment
    assert svc.ffmpeg_runs - before == 6
    assert list((svc.root).glob("*/*/a0/c0.adts"))  # the chunk is cached for later seeks
    runs = svc.ffmpeg_runs
    assert client.get("/local/ac3/copy/a0/2.m4s").status_code == 200
    assert svc.ffmpeg_runs == runs


@needs_ffmpeg
def test_segments_are_served_from_cache_the_second_time(env):
    client, svc = env
    client.get("/local/plain/copy/v.m3u8")
    assert client.get("/local/plain/copy/v/1.m4s").status_code == 200
    runs = svc.ffmpeg_runs
    assert client.get("/local/plain/copy/v/1.m4s").status_code == 200
    assert svc.ffmpeg_runs == runs


@needs_ffmpeg
def test_cache_is_trimmed_to_its_limit(media_dir: Path, tmp_path: Path):
    mgr = downloads.DownloadManager(media_dir, lambda e: "", source_limits=lambda s: (1, 1))
    downloads.set_manager(mgr)
    try:
        svc = LocalMedia(tmp_path / "c", max_bytes=1)
        path = media_dir / "plain.mp4"

        async def run() -> int:
            pres = await svc.presentation(path, "copy")
            for k in range(len(pres.video)):
                await svc.video_segment(pres, k)
            return svc.trim_cache()

        assert asyncio.run(run()) > 0
        left = list((tmp_path / "c").glob("*/*/*/*.m4s"))
        assert left == [] or len(left) < 4
        assert list((tmp_path / "c").glob("*/keyframes.json"))  # the small index survives
    finally:
        downloads.set_manager(None)


@needs_ffmpeg
def test_keyframe_index_is_persisted_and_reused(media_dir: Path, tmp_path: Path):
    path = media_dir / "hevc.mkv"

    async def plan(svc: LocalMedia) -> int:
        return len((await svc.presentation(path, "copy")).video)

    a = LocalMedia(tmp_path / "idx")
    n = asyncio.run(plan(a))
    b = LocalMedia(tmp_path / "idx")  # new process: no scan, just the stored index
    assert asyncio.run(plan(b)) == n
    assert b.ffmpeg_runs == 1  # only the ffprobe of the streams


# =============================================================================
# Security
# =============================================================================


@needs_ffmpeg
def test_only_finished_downloads_are_served(env):
    client, _ = env
    for url in (
        "/api/local/busy/plan",
        "/local/busy/copy/master.m3u8",
        "/local/busy/copy/v/0.m4s",
        "/local/busy/sub/0.vtt",
    ):
        assert client.get(url).status_code == 409, url
    for url in ("/api/local/nope/plan", "/local/nope/copy/master.m3u8", "/local/nope/sub/0.vtt"):
        assert client.get(url).status_code == 404, url
    assert client.get("/local/plain/hax/master.m3u8").status_code == 404


@needs_ffmpeg
def test_paths_never_come_from_the_request(env):
    client, _ = env
    for did in ("..%2Fplain", "..%2Fplain.mp4", "%2Fetc%2Fpasswd", "plain.mp4", "%2e%2e%2fplain"):
        assert client.get(f"/api/local/{did}/plan").status_code in (404, 405, 422), did
        assert client.get(f"/local/{did}/copy/master.m3u8").status_code in (404, 405, 422), did
    assert client.get("/local/plain/copy/v/-1.m4s").status_code in (404, 422)
    assert client.get("/local/plain/copy/v/abc.m4s").status_code == 422


def test_router_requires_authentication():
    from fastapi import HTTPException

    def deny() -> dict:
        raise HTTPException(401, "no")

    app = FastAPI()
    app.include_router(local_media.build_router(deny))
    with TestClient(app) as client:
        for url in (
            "/api/local/x/plan",
            "/local/x/copy/master.m3u8",
            "/local/x/copy/v.m3u8",
            "/local/x/copy/v/0.m4s",
            "/local/x/copy/a0/0.m4s",
            "/local/x/sub/0.vtt",
        ):
            assert client.get(url).status_code == 401, url


def test_unreadable_media_is_a_clean_error_not_a_crash(tmp_path: Path):
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not installed")
    (tmp_path / "junk.mkv").write_bytes(b"this is not a video" * 100)
    mgr = downloads.DownloadManager(tmp_path, lambda e: "", source_limits=lambda s: (1, 1))
    mgr._entries["junk"] = downloads.Entry(
        id="junk", kind="movie", item_id="1", source_id="", title="junk", filename="junk.mkv",
        ext="mkv", status="done",
    )  # fmt: skip
    downloads.set_manager(mgr)
    local_media.set_service(LocalMedia(tmp_path / "cache"))
    try:
        app = FastAPI()
        app.include_router(local_media.build_router(lambda: {"sub": "t"}))
        with TestClient(app) as client:
            assert client.get("/api/local/junk/plan").status_code == 500
            assert client.get("/local/junk/copy/master.m3u8").status_code == 500
    finally:
        downloads.set_manager(None)
        local_media.set_service(None)


# =============================================================================
# The page: local downloads never use the transcode session
# =============================================================================


@pytest.fixture
def page_client(tmp_path: Path, media_dir: Path):
    from unittest.mock import patch

    from fastapi.testclient import TestClient as TC

    import cache as cache_module

    with (
        patch("cache.CACHE_DIR", tmp_path),
        patch("cache.SERVER_SETTINGS_FILE", tmp_path / "server_settings.json"),
        patch("cache.USERS_DIR", tmp_path / "users"),
        patch("auth.CACHE_DIR", tmp_path),
        patch("auth.SERVER_SETTINGS_FILE", tmp_path / "server_settings.json"),
        patch("auth.USERS_DIR", tmp_path / "users"),
        patch("epg.init"),
        patch("ffmpeg_command.init"),
        patch("ffmpeg_session.cleanup_and_recover_sessions"),
    ):
        (tmp_path / "users").mkdir(exist_ok=True)
        import auth
        import main

        cache_module.get_cache().clear()
        client = TC(main.app)  # no lifespan: nothing is started or killed
        auth.create_user("testuser", "testpass123")
        client.cookies.set("token", auth.create_token({"sub": "testuser"}))
        mgr = downloads.DownloadManager(media_dir, lambda e: "", source_limits=lambda s: (1, 1))
        mgr._entries["plain"] = downloads.Entry(
            id="plain", kind="movie", item_id="9", source_id="", title="Plain", filename="plain.mp4",
            ext="mp4", status="done",
        )  # fmt: skip
        downloads.set_manager(mgr)
        yield client
        downloads.set_manager(None)


def test_download_page_is_driven_by_the_local_id_not_the_transcoder(page_client):
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not installed")
    r = page_client.get("/play/download/plain")
    assert r.status_code == 200
    assert 'localDownloadId: "plain"' in r.text
    assert 'transcodeMode: "never"' in r.text  # never "always": no /transcode/start
    assert "/static/js/local-playback.js" in r.text
    assert r.text.index("/static/js/local-playback.js") < r.text.index("/static/js/player.js")
    assert 'id="error-raw"' in r.text and "Ouvrir le fichier brut" in r.text
    # streamed (non download) pages are untouched
    other = page_client.get("/play/movie/123")
    assert "localDownloadId: null" in other.text or other.status_code != 200


def test_transcode_start_still_refuses_files_outside_the_download_folder(page_client):
    r = page_client.get("/transcode/start", params={"url": "/etc/hosts", "content_type": "movie"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- regression: audio sync


def test_audio_copy_drops_packets_before_the_seek_time():
    grid = plan_audio_grid(600.0, 48000)
    cmd = audio_copy_cmd("/x/f.mkv", media("mkv", a=(audio("aac"),)), audio("aac"), grid, 2)
    assert cmd[cmd.index("-copypriorss") + 1] == "0"


def _dominant_hz(pcm: bytes, rate: int) -> float:
    """Frequency of a pure sweep over a short window, from its zero crossings."""
    samples = array.array("f", pcm)
    crossings = sum(1 for a, b in zip(samples, samples[1:], strict=False) if (a < 0) != (b < 0))
    return crossings / 2 / (len(samples) / rate)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_copied_audio_segments_hold_the_audio_of_their_own_time(tmp_path: Path):
    """A real mkv has sparse cue points (one per 10 s GOP here). An input seek lands on the cue
    BEFORE the target, and the copied audio then started seconds too early (sound lagging the
    picture by up to 2 s on real downloads). The sweep makes every instant identifiable: the
    frequency at time t is 200 + 40 t Hz."""
    src = tmp_path / "sweep.mkv"
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=60",
        "-f", "lavfi", "-i", "aevalsrc=sin(2*PI*(200*t+20*t*t)):s=48000:d=60",
        "-c:v", "libx264", "-g", "250", "-keyint_min", "250", "-sc_threshold", "0",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", str(src),
    )  # fmt: skip

    async def run() -> list[tuple[float, float]]:
        lm = LocalMedia(cache_root=tmp_path / "cache", max_bytes=1 << 30)
        pres = await lm.presentation(src, "copy")
        out = []
        for k in (3, 5, 8):  # 18 s, 30 s, 48 s: inside a GOP, not on a cue
            seg = await lm.audio_segment(pres, 0, k)
            joined = tmp_path / f"a{k}.mp4"
            joined.write_bytes((seg.parent / "init.mp4").read_bytes() + seg.read_bytes())
            raw = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(joined), "-t", "0.3", "-ac", "1",
                 "-ar", "48000", "-f", "f32le", "-"],
                capture_output=True, check=True,
            ).stdout  # fmt: skip
            start = pres.audio_grids[0].seg_start_samples(k) / 48000
            out.append((start, (_dominant_hz(raw, 48000) - 200) / 40))
        return out

    # the 0.3 s window is centered 0.15 s after the segment start; the old bug was 1 to 2 s off
    for claimed, heard in asyncio.run(run()):
        assert heard == pytest.approx(claimed + 0.15, abs=0.1), (claimed, heard)
