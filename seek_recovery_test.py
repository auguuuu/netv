"""Seek recovery: retry with a fresh ffmpeg, no limit-killer casualty, clean errors."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import time

from fastapi import HTTPException

import pytest

from ffmpeg_session import (
    _transcode_lock,
    _transcode_sessions,
    _url_to_session,
    enforce_stream_limits,
)

import ffmpeg_session


class Proc:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.pid = 1

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _session(tmp_path, **kw):
    s = {
        "url": "http://u/x.mp4",
        "dir": str(tmp_path),
        "process": Proc(),
        "is_vod": True,
        "username": "admin",
        "source_id": "src",
        "started": time.time(),
        "last_access": time.time(),
    }
    s.update(kw)
    return s


@pytest.fixture(autouse=True)
def _clean():
    with _transcode_lock:
        _transcode_sessions.clear()
        _url_to_session.clear()
    ffmpeg_session._seek_generation.clear()
    yield
    with _transcode_lock:
        _transcode_sessions.clear()
        _url_to_session.clear()


def _patches(launched, waits):
    async def fake_launch(cmd):
        launched.append(cmd)
        return Proc()

    return (
        patch("ffmpeg_session.get_settings", return_value={"probe_movies": False}),
        patch("ffmpeg_session.resolve_hls_master_playlist", side_effect=lambda u: u),
        patch("ffmpeg_session._launch_ffmpeg", new=fake_launch),
        patch("ffmpeg_session._wait_for_playlist", new=AsyncMock(side_effect=waits)),
        patch("ffmpeg_session._spawn_background_task", side_effect=lambda c: c.close()),
    )


async def _seek(tmp_path, waits, t=48.4):
    launched: list[list[str]] = []
    with (
        patch.dict(_transcode_sessions, {"s": _session(tmp_path)}, clear=True),
        patch.dict(_url_to_session, {}, clear=True),
    ):
        ps = _patches(launched, waits)
        for p in ps:
            p.start()
        try:
            return launched, await ffmpeg_session.seek_transcode("s", t)
        finally:
            for p in ps:
                p.stop()


class TestSeekRetry:
    @pytest.mark.asyncio
    async def test_first_attempt_ok_launches_once(self, tmp_path):
        launched, out = await _seek(tmp_path, [True])
        assert len(launched) == 1 and out["ok"]

    @pytest.mark.asyncio
    async def test_stalled_first_attempt_is_retried_with_fresh_ffmpeg(self, tmp_path):
        (tmp_path / "seg016.ts").write_bytes(b"x" * 10)  # partial output of the stalled try
        launched, out = await _seek(tmp_path, [False, True])
        assert len(launched) == 2 and out["ok"]
        assert launched[0] == launched[1]
        assert not (tmp_path / "seg016.ts").exists()

    @pytest.mark.asyncio
    async def test_all_attempts_fail_gives_clean_504(self, tmp_path):
        with pytest.raises(HTTPException) as exc:
            await _seek(tmp_path, [False, False])
        assert exc.value.status_code == 504
        assert "retry" in exc.value.detail

    @pytest.mark.asyncio
    async def test_superseded_seek_does_not_retry_or_stomp(self, tmp_path):
        launched: list[list[str]] = []

        async def newer_seek_arrives(*a, **k):
            ffmpeg_session._seek_generation["s"] += 1
            return False

        with (
            patch.dict(_transcode_sessions, {"s": _session(tmp_path)}, clear=True),
            patch.dict(_url_to_session, {}, clear=True),
        ):
            ps = _patches(launched, [False])
            for p in ps:
                p.start()
            try:
                with (
                    patch("ffmpeg_session._wait_for_playlist", new=newer_seek_arrives),
                    pytest.raises(HTTPException) as exc,
                ):
                    await ffmpeg_session.seek_transcode("s", 10.0)
            finally:
                for p in ps:
                    p.stop()
        assert exc.value.status_code == 409
        assert len(launched) == 1

    @pytest.mark.asyncio
    async def test_seek_touches_session(self, tmp_path):
        with patch.dict(_transcode_sessions, {"s": _session(tmp_path, last_access=0)}, clear=True):
            launched: list[list[str]] = []
            ps = _patches(launched, [True])
            for p in ps:
                p.start()
            try:
                with patch.dict(_url_to_session, {}, clear=True):
                    await ffmpeg_session.seek_transcode("s", 5.0)
                assert time.time() - _transcode_sessions["s"]["last_access"] < 5
            finally:
                for p in ps:
                    p.stop()


class TestSeekReusesSessionProbe:
    @pytest.mark.asyncio
    async def test_no_reprobe_when_session_holds_media_info(self, tmp_path):
        from ffmpeg_command import MediaInfo

        mi = MediaInfo("h264", "aac", "yuv420p", 2, 44100, "LC", None, 100.0, 720, 1_000_000)
        launched: list[list[str]] = []
        with (
            patch.dict(_transcode_sessions, {"s": _session(tmp_path, media_info=mi)}, clear=True),
            patch.dict(_url_to_session, {}, clear=True),
            patch("ffmpeg_session.probe_media") as probe,
        ):
            ps = _patches(launched, [True])
            for p in ps:
                p.start()
            try:
                # _patches sets probe_movies False; force it on to prove the cache wins
                with patch("ffmpeg_session.get_settings", return_value={"probe_movies": True}):
                    await ffmpeg_session.seek_transcode("s", 200.0)
            finally:
                for p in ps:
                    p.stop()
        probe.assert_not_called()
        assert "copy" in launched[0]  # probed info drives stream copy


class TestLimitKillerSparesReusedSession:
    def test_spare_session_is_not_stopped(self, tmp_path):
        with _transcode_lock:
            _transcode_sessions["s1"] = _session(tmp_path, last_access=time.time())
        assert enforce_stream_limits("admin", "src", 1, 1, spare_session_id="s1") is None
        assert "s1" in _transcode_sessions

    def test_without_spare_the_own_oldest_is_stopped(self, tmp_path):
        with _transcode_lock:
            _transcode_sessions["s1"] = _session(tmp_path, last_access=0)
        assert enforce_stream_limits("admin", "src", 1, 1) is None
        assert "s1" not in _transcode_sessions

    @pytest.mark.asyncio
    async def test_reload_reuses_session_mid_seek(self, tmp_path):
        with _transcode_lock:
            _transcode_sessions["s1"] = _session(tmp_path)
            _url_to_session["http://u/x.mp4"] = "s1"
        reuse = AsyncMock(return_value={"session_id": "s1"})
        with patch("ffmpeg_session._try_reuse_session", new=reuse):
            out = await ffmpeg_session.start_transcode(
                "http://u/x.mp4",
                "movie",
                username="admin",
                source_id="src",
                user_max_streams=1,
                source_max_streams=1,
            )
        assert out == {"session_id": "s1"}
        assert "s1" in _transcode_sessions
