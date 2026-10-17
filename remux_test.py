"""Tests for remux.py (track choice, command, real ffmpeg run) and its use by downloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import shutil
import subprocess

import pytest

from downloads import DownloadManager
from remux import Plan, RemuxError, build_cmd, choose_audio, lang2, list_audio, needs_remux, remux

import downloads


def track(idx: int, lang: str, title: str = "", default: bool = False) -> dict[str, Any]:
    return {"idx": idx, "lang": lang, "title": title, "codec": "aac", "profile": "LC",
            "channels": 2, "default": default}  # fmt: skip


ALL = [
    track(0, "ca"), track(1, "en"), track(2, "es", "Latin American"), track(3, "es", "European"),
    track(4, "fr", "Canadian"), track(5, "fr", "European"), track(6, "ja"),
]  # fmt: skip


def test_lang2() -> None:
    assert lang2("fre") == "fr" and lang2("fra") == "fr" and lang2("eng") == "en"
    assert lang2("und") == "" and lang2("") == "" and lang2("xx") == "xx"


@pytest.mark.parametrize("vo", ["en", "EN"])
def test_english_vo_keeps_english(vo: str) -> None:
    assert choose_audio(ALL, vo)[0] == 1


def test_french_vo_keeps_french_and_prefers_european() -> None:
    assert choose_audio(ALL, "fr")[0] == 5


@pytest.mark.parametrize("vo", ["ja", "ko", "es", "zz"])
def test_other_vo_gets_the_french_track(vo: str) -> None:
    assert choose_audio(ALL, vo)[0] == 5


def test_unknown_vo_keeps_english_else_french() -> None:
    assert choose_audio(ALL, "") == (1, "en (vo unknown)")
    assert choose_audio([track(0, "de"), track(1, "fr")], "")[0] == 1


def test_pref_overrides_the_rule() -> None:
    assert choose_audio(ALL, "en", pref=6) == (6, "chosen")
    assert choose_audio(ALL, "en", pref=99)[0] == 1  # out of range: back to the rule


def test_missing_french_falls_back_to_vo_then_default_then_first() -> None:
    no_fr = [track(0, "en"), track(1, "ja"), track(2, "de", default=True)]
    assert choose_audio(no_fr, "ja")[0] == 1
    assert choose_audio(no_fr, "ko")[0] == 2
    assert choose_audio([track(0, "de"), track(1, "it")], "ko")[0] == 0


def test_single_and_no_audio() -> None:
    assert choose_audio([track(0, "ja")], "en")[0] == 0
    assert choose_audio([], "en")[0] == -1


def test_french_commentary_is_a_last_resort() -> None:
    tracks = [track(0, "fr", "Commentary"), track(1, "fr", "Audio Description"), track(2, "fr")]
    assert choose_audio(tracks, "ja")[0] == 2
    assert choose_audio(tracks[:2], "ja")[0] == 0


def test_needs_remux() -> None:
    one = {"streams": [{"codec_type": "audio"}]}
    two = {"streams": [{"codec_type": "audio"}, {"codec_type": "audio"}]}
    assert not needs_remux(one, "mp4")
    assert needs_remux(one, "mkv") and needs_remux(two, "mp4")


def probe_of(video: str = "h264", audio_codec: str = "aac") -> dict[str, Any]:
    return {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": video},
            {"index": 1, "codec_type": "audio", "codec_name": audio_codec, "profile": "LC",
             "channels": 2, "tags": {"language": "eng"}},
            {"index": 2, "codec_type": "audio", "codec_name": "ac3", "channels": 6,
             "tags": {"language": "fre"}},
            {"index": 3, "codec_type": "subtitle", "codec_name": "subrip",
             "tags": {"language": "eng", "title": "SDH"}},
            {"index": 4, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
        ]
    }  # fmt: skip


def test_build_cmd_copies_aac_encodes_the_rest_and_skips_image_subs() -> None:
    probe = probe_of()
    tracks = list_audio(probe)
    cmd = build_cmd(Path("in.mkv"), Path("out.mp4"), probe, tracks[0])
    assert ["-map", "0:1"] == cmd[cmd.index("0:1") - 1 : cmd.index("0:1") + 1]
    assert "0:2" not in cmd and "0:4" not in cmd and "0:3" in cmd
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert "handler_name=SDH" in cmd[cmd.index("-metadata:s:s:0") + 1]
    cmd = build_cmd(Path("in.mkv"), Path("out.mp4"), probe, tracks[1])
    assert cmd[cmd.index("-c:a") + 1] == "aac" and cmd[cmd.index("-b:a") + 1] == "192k"
    assert cmd[cmd.index("-ac") + 1] == "2"


def test_build_cmd_refuses_unsupported_video() -> None:
    probe = probe_of(video="vp9")
    with pytest.raises(RemuxError):
        build_cmd(Path("a"), Path("b"), probe, list_audio(probe)[0])


needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")


def make_mkv(path: Path) -> None:
    """3 s test movie: H.264, three audio tracks (en, fr, ja) and one text subtitle."""
    srt = path.with_suffix(".srt")
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nhello\n")
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=3"]
    for f in (440, 660, 880):
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={f}:duration=3"]
    cmd += ["-i", str(srt), "-map", "0:v", "-map", "1", "-map", "2", "-map", "3", "-map", "4:s"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "srt"]
    for i, lang in enumerate(("eng", "fre", "jpn")):
        cmd += [f"-metadata:s:a:{i}", f"language={lang}"]
    subprocess.run([*cmd, str(path)], check=True)


@needs_ffmpeg
def test_remux_real_file(tmp_path: Path) -> None:
    src, dst = tmp_path / "m.mkv", tmp_path / "m.mp4"
    make_mkv(src)
    plan = remux(src, dst, vo="ja")
    assert (plan.kept, plan.audio[plan.kept]["lang"]) == (1, "fr")
    assert [t["lang"] for t in plan.audio] == ["en", "fr", "ja"]
    kinds = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0", str(dst)],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert sorted(kinds) == ["aac,audio", "h264,video", "mov_text,subtitle"]
    assert src.exists() and not list(tmp_path.glob("*.remux"))
    assert remux(src, tmp_path / "pref.mp4", vo="en", pref=2).kept == 2


@needs_ffmpeg
def test_remux_failure_leaves_no_output(tmp_path: Path) -> None:
    src = tmp_path / "bad.mkv"
    src.write_bytes(b"not a video")
    with pytest.raises((RemuxError, subprocess.SubprocessError)):
        remux(src, tmp_path / "bad.mp4")
    assert not (tmp_path / "bad.mp4").exists() and not list(tmp_path.glob("*.remux"))


# ---- manager integration (fake optimizer, no ffmpeg) ----


def fake_optimizer(entry: downloads.Entry, src: Path, dst: Path) -> Plan:
    dst.write_bytes(b"mp4" * 10)
    return Plan(audio=[track(0, "en"), track(1, "fr")], kept=1, reason="test")


def done_entry(mgr: DownloadManager, name: str = "Film") -> downloads.Entry:
    (mgr.dir / f"{name}.mkv").write_bytes(b"x" * 1000)
    e = downloads.Entry(
        id="e1", kind="movie", item_id="7", source_id="s", title=name, filename=f"{name}.mkv",
        ext="mkv", status="done", bytes_done=1000, bytes_total=1000,
    )  # fmt: skip
    mgr._entries[e.id] = e
    return e


def wait_idle(e: downloads.Entry) -> None:
    import time

    for _ in range(100):
        if not e.processing:
            return
        time.sleep(0.05)
    raise AssertionError("optimisation did not finish")


def test_optimize_swaps_the_file(tmp_path: Path) -> None:
    mgr = DownloadManager(tmp_path, lambda e: "", optimizer=fake_optimizer)
    e = done_entry(mgr)
    assert mgr.optimize(e.id)
    wait_idle(e)
    assert (e.filename, e.ext, e.src_ext, e.audio_kept) == ("Film.mp4", "mp4", "mkv", 1)
    assert e.src_bytes == 1000 and e.bytes_done == 30
    assert not (tmp_path / "Film.mkv").exists() and (tmp_path / "Film.mp4").exists()
    assert not mgr.optimize(e.id)  # already done
    assert mgr.public(e)["audio_kept"] == 1


def test_optimize_failure_keeps_the_original(tmp_path: Path) -> None:
    def boom(*_a: Any) -> Plan:
        raise RemuxError("nope")

    mgr = DownloadManager(tmp_path, lambda e: "", optimizer=boom)
    e = done_entry(mgr)
    mgr.optimize(e.id)
    wait_idle(e)
    assert (e.filename, e.ext, e.source_audio) == ("Film.mkv", "mkv", [])
    assert (tmp_path / "Film.mkv").exists()


def test_no_optimizer_is_a_noop(tmp_path: Path) -> None:
    mgr = DownloadManager(tmp_path, lambda e: "")
    assert not mgr.optimize(done_entry(mgr).id)


def test_redownload_with_another_audio_track(tmp_path: Path) -> None:
    mgr = DownloadManager(tmp_path, lambda e: "", optimizer=fake_optimizer)
    mgr.schedule = lambda: None  # type: ignore[method-assign]  # do not start a real download
    e = done_entry(mgr)
    mgr.optimize(e.id)
    wait_idle(e)
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        mgr.redownload_with_audio(e.id, 1)  # already the kept track
    with pytest.raises(HTTPException):
        mgr.redownload_with_audio(e.id, 5)
    new = mgr.redownload_with_audio(e.id, 0)
    assert new.audio_pref == 0 and new.ext == "mkv" and new.status == "queued"
    assert new.item_id == "7" and e.id not in mgr._entries
    assert not (tmp_path / "Film.mp4").exists()
