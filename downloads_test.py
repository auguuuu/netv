"""Tests for downloads.py using a local fake provider (Range, slow, drops)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import asyncio
import json
import socket
import threading
import time

import pytest

from downloads import DownloadManager, SlowWatch

import downloads


SIZE = 1_000_000
DATA = bytes(i % 251 for i in range(SIZE))


class FakeProvider:
    """Tiny HTTP server: /f/<name> serves DATA, honours Range, can be slow or flaky."""

    def __init__(self) -> None:
        self.hits: list[tuple[str, str | None]] = []  # (path, Range header)
        self.delay = 0.0  # seconds between 50 KB chunks
        self.drop_after: int | None = None  # cut the connection once after N bytes
        self.ignore_range = False
        self.slow_after: int | None = None  # first connection only: throttle after N bytes
        self.slow_delay = 0.1
        self.chunk = 50_000
        self.burst_frac: float | None = (
            None  # every response: fast for this share of it, then throttled
        )
        self.slow_bytes = 0  # bytes served in the throttled phase (all connections)
        self.status_override: int | None = None
        self.content_type = "video/x-matroska"
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                rng = self.headers.get("Range")
                provider.hits.append((self.path, rng))
                if provider.status_override:
                    self.send_response(provider.status_override)
                    if provider.status_override == 416:
                        self.send_header("Content-Range", f"bytes */{SIZE}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = 0
                code = 200
                if rng and not provider.ignore_range:
                    start = int(rng.split("=")[1].split("-")[0])
                    code = 206
                body = DATA[start:]
                self.send_response(code)
                self.send_header("Content-Type", provider.content_type)
                self.send_header("Content-Length", str(len(body)))
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{SIZE - 1}/{SIZE}")
                self.end_headers()
                sent = 0
                limit = provider.drop_after
                if limit is not None:
                    provider.drop_after = None  # only once
                slow_after = provider.slow_after
                if slow_after is not None:
                    provider.slow_after = None  # only the first connection is throttled
                try:
                    while sent < len(body):
                        n = min(provider.chunk, len(body) - sent)
                        if limit is not None and sent + n > limit:
                            self.wfile.write(body[sent : sent + (limit - sent)])
                            self.wfile.flush()
                            self.close_connection = True
                            self.connection.shutdown(socket.SHUT_RDWR)
                            return
                        throttled = provider.burst_frac is not None and sent >= (
                            provider.burst_frac * len(body)
                        )
                        self.wfile.write(body[sent : sent + n])
                        self.wfile.flush()
                        sent += n
                        if throttled:
                            provider.slow_bytes += n
                            time.sleep(provider.slow_delay)
                        if provider.delay:
                            time.sleep(provider.delay)
                        if slow_after is not None and sent >= slow_after:
                            time.sleep(provider.slow_delay)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, name: str = "a") -> str:
        return f"http://127.0.0.1:{self.port}/f/{name}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def provider() -> Iterator[FakeProvider]:
    p = FakeProvider()
    yield p
    p.close()


def wait_for(cond: Callable[[], bool], timeout: float = 10.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def make_manager(tmp_path: Path, provider: FakeProvider, **kw: Any) -> DownloadManager:
    kw.setdefault("min_free", 0)
    kw.setdefault("backoff", 0.02)
    kw.setdefault("tick_interval", 0.1)
    return DownloadManager(tmp_path / "dl", lambda e: provider.url(e.item_id), **kw)


def test_download_completes(tmp_path: Path, provider: FakeProvider) -> None:
    mgr = make_manager(tmp_path, provider)
    e = mgr.add("movie", "1", title="Big Movie (2020)", ext="mp4")
    assert e.filename == "Big Movie (2020).mp4"
    wait_for(lambda: e.status == "done")
    assert (mgr.dir / e.filename).read_bytes() == DATA
    assert not list(mgr.dir.glob("*.part"))
    pub = mgr.public(e)
    assert pub["bytes_done"] == pub["bytes_total"] == SIZE
    assert pub["finished_at"] and pub["error"] is None
    assert set(pub) >= {
        "id", "kind", "title", "filename", "ext", "status", "bytes_done", "bytes_total",
        "speed_bps", "eta_seconds", "error", "created_at", "finished_at", "poster",
    }  # fmt: skip


def test_resume_after_connection_drop(tmp_path: Path, provider: FakeProvider) -> None:
    provider.drop_after = 400_000
    mgr = make_manager(tmp_path, provider)
    e = mgr.add("movie", "1", title="Flaky")
    wait_for(lambda: e.status == "done")
    assert (mgr.dir / e.filename).read_bytes() == DATA
    ranges = [r for _, r in provider.hits]
    assert ranges[0] is None
    assert ranges[1] and ranges[1].startswith("bytes=") and ranges[1] != "bytes=0-"


def test_pause_resume_uses_range(tmp_path: Path, provider: FakeProvider) -> None:
    provider.delay = 0.05
    mgr = make_manager(tmp_path, provider)
    e = mgr.add("movie", "1", title="P")
    wait_for(lambda: e.bytes_done > 100_000)
    mgr.pause(e.id)
    assert e.status == "paused"
    wait_for(lambda: e.id not in mgr._running)
    part_size = (mgr.dir / (e.filename + ".part")).stat().st_size
    assert 0 < part_size < SIZE
    provider.delay = 0
    mgr.resume(e.id)
    wait_for(lambda: e.status == "done")
    assert (mgr.dir / e.filename).read_bytes() == DATA
    assert provider.hits[-1][1] == f"bytes={part_size}-"


def test_server_ignoring_range_restarts(tmp_path: Path, provider: FakeProvider) -> None:
    provider.delay = 0.05
    provider.ignore_range = True
    mgr = make_manager(tmp_path, provider)
    e = mgr.add("movie", "1", title="NoRange")
    wait_for(lambda: e.bytes_done > 100_000)
    mgr.pause(e.id)
    wait_for(lambda: e.id not in mgr._running)
    provider.delay = 0
    mgr.resume(e.id)
    wait_for(lambda: e.status == "done")
    assert (mgr.dir / e.filename).read_bytes() == DATA  # not corrupted by appending


def test_fifo_queue_one_per_source(tmp_path: Path, provider: FakeProvider) -> None:
    provider.delay = 0.01
    mgr = make_manager(tmp_path, provider)
    a = mgr.add("movie", "1", source_id="s1", title="A")
    b = mgr.add("movie", "2", source_id="s1", title="B")
    c = mgr.add("movie", "3", source_id="s1", title="C")
    other = mgr.add("movie", "4", source_id="s2", title="D")
    assert (a.status, b.status, c.status) == ("downloading", "queued", "queued")
    assert other.status == "downloading"  # another source runs in parallel
    wait_for(lambda: all(x.status == "done" for x in (a, b, c, other)), 30)
    order = [p.rsplit("/", 1)[1] for p, _ in provider.hits if p.endswith(("1", "2", "3"))]
    assert order == ["1", "2", "3"]
    finished = [x.finished_at for x in (a, b, c)]
    assert finished == sorted(finished)  # type: ignore[arg-type]


def test_dedupe(tmp_path: Path, provider: FakeProvider) -> None:
    provider.delay = 0.05
    mgr = make_manager(tmp_path, provider)
    a = mgr.add("movie", "1", title="A")
    assert mgr.add("movie", "1", title="other title") is a
    assert mgr.add("episode", "1", title="Ep") is not a  # kind is part of the key
    mgr.pause(a.id)
    assert mgr.add("movie", "1") is a  # paused still counts
    assert len(mgr.list_all()) == 2
    mgr.cancel(a.id)
    again = mgr.add("movie", "1", title="A")  # cancelled entries are replaced
    assert again is not a and len(mgr.list_all()) == 2


def test_done_dedupe_and_stale_file(tmp_path: Path, provider: FakeProvider) -> None:
    mgr = make_manager(tmp_path, provider)
    a = mgr.add("movie", "1", title="A")
    wait_for(lambda: a.status == "done")
    assert mgr.add("movie", "1") is a
    (mgr.dir / a.filename).unlink()
    b = mgr.add("movie", "1", title="A")
    assert b is not a
    wait_for(lambda: b.status == "done")


def test_cancel_removes_part_and_keeps_entry(tmp_path: Path, provider: FakeProvider) -> None:
    provider.delay = 0.05
    mgr = make_manager(tmp_path, provider)
    e = mgr.add("movie", "1", title="C")
    wait_for(lambda: e.bytes_done > 100_000)
    mgr.cancel(e.id)
    wait_for(lambda: e.id not in mgr._running)
    assert e.status == "cancelled"
    assert not list(mgr.dir.glob("C.*"))
    assert mgr.public(e)["bytes_done"] == 0


def test_delete_removes_file_and_entry(tmp_path: Path, provider: FakeProvider) -> None:
    mgr = make_manager(tmp_path, provider)
    e = mgr.add("movie", "1", title="D")
    wait_for(lambda: e.status == "done")
    path = mgr.dir / e.filename
    assert path.exists()
    mgr.delete(e.id)
    assert not path.exists() and mgr.list_all() == []
    # deleting a running download removes its part too
    provider.delay = 0.05
    f = mgr.add("movie", "2", title="E")
    wait_for(lambda: f.bytes_done > 50_000)
    mgr.delete(f.id)
    wait_for(lambda: not mgr._running)
    assert not [p for p in mgr.dir.iterdir() if not p.name.startswith(".")]


def test_restart_recovery(tmp_path: Path, provider: FakeProvider) -> None:
    provider.delay = 0.05
    mgr = make_manager(tmp_path, provider)
    done = mgr.add("movie", "9", source_id="s2", title="Done")
    wait_for(lambda: done.bytes_done > 0)  # keep both running slow
    e = mgr.add("movie", "1", source_id="s1", title="R")
    q = mgr.add("movie", "2", source_id="s1", title="Q")
    wait_for(lambda: e.bytes_done > 100_000)
    mgr.shutdown()
    wait_for(lambda: not mgr._running)
    hits_before = len(provider.hits)

    provider.delay = 0
    mgr2 = make_manager(tmp_path, provider)
    mgr2.start()
    time.sleep(0.4)
    assert len(provider.hits) == hits_before  # nothing auto started
    by_title = {x["title"]: x for x in mgr2.list_all()}
    assert by_title["R"]["status"] == "paused"
    assert by_title["Q"]["status"] == "paused"
    assert 0 < by_title["R"]["bytes_done"] < SIZE
    mgr2.resume(by_title["R"]["id"])
    wait_for(lambda: mgr2.get(by_title["R"]["id"]).status == "done")  # type: ignore[union-attr]
    assert (mgr2.dir / "R.mkv").read_bytes() == DATA
    mgr2.shutdown()
    assert q.filename


def test_index_never_stores_provider_url(tmp_path: Path, provider: FakeProvider) -> None:
    mgr = make_manager(tmp_path, provider)
    e = mgr.add("movie", "1", title="X")
    wait_for(lambda: e.status == "done")
    raw = (mgr.dir / downloads.INDEX_NAME).read_text()
    assert "127.0.0.1" not in raw and json.loads(raw)["downloads"][0]["title"] == "X"


def test_disk_space_refusal(tmp_path: Path, provider: FakeProvider) -> None:
    mgr = make_manager(tmp_path, provider, min_free=10**18)
    e = mgr.add("movie", "1", title="Huge")
    wait_for(lambda: e.status == "error")
    assert "espace disque" in (e.error or "").lower()
    assert not list(mgr.dir.glob("*.part")) or (mgr.dir / "Huge.mkv.part").stat().st_size == 0


def test_http_error_and_html_page(tmp_path: Path, provider: FakeProvider) -> None:
    provider.status_override = 404
    mgr = make_manager(tmp_path, provider)
    e = mgr.add("movie", "1", title="Gone")
    wait_for(lambda: e.status == "error")
    assert "404" in (e.error or "")
    provider.status_override = None
    provider.content_type = "text/html"
    f = mgr.add("movie", "2", title="Html")
    wait_for(lambda: f.status == "error")
    assert "page d'erreur" in (f.error or "")
    provider.content_type = "video/mp4"
    mgr.resume(e.id)  # error entries can be retried
    wait_for(lambda: e.status == "done")


def test_network_down_ends_in_error(tmp_path: Path, provider: FakeProvider) -> None:
    mgr = make_manager(tmp_path, provider, max_retries=2)
    provider.close()
    e = mgr.add("movie", "1", title="Down")
    wait_for(lambda: e.status == "error")
    assert "Erreur réseau" in (e.error or "")


def test_path_traversal(tmp_path: Path, provider: FakeProvider) -> None:
    mgr = make_manager(tmp_path, provider)
    e = mgr.add("movie", "1", title="../../../etc/passwd", ext="../sh")
    assert "/" not in e.filename and ".." not in e.filename.replace("...", "")
    wait_for(lambda: e.status == "done")
    assert (mgr.dir / e.filename).parent == mgr.dir
    assert not (tmp_path / "etc").exists()
    for bad in ("../x", "a/b", "..", "", "/etc/passwd", "a\\b"):
        with pytest.raises(ValueError):
            mgr.safe_path(bad)
    # tampered index entries are dropped on load
    idx = mgr.dir / downloads.INDEX_NAME
    data = json.loads(idx.read_text())
    data["downloads"].append({**data["downloads"][0], "id": "evil", "filename": "../../evil.mkv"})
    idx.write_text(json.dumps(data))
    mgr2 = make_manager(tmp_path, provider)
    assert [d["id"] for d in mgr2.list_all()] == [e.id]
    assert not mgr2.is_inside("/etc/passwd")
    assert mgr2.is_inside(str(mgr.dir / e.filename))
    assert not mgr2.is_inside(str(mgr.dir / ".." / "x"))
    assert mgr2.is_inside("file://" + str(mgr.dir / e.filename))


def test_yields_to_playback(tmp_path: Path, provider: FakeProvider) -> None:
    provider.delay = 0.02
    playing = {"n": 1}
    mgr = make_manager(tmp_path, provider, source_limits=lambda sid: (1, playing["n"]))
    mgr.start()
    e = mgr.add("movie", "1", source_id="s1", title="Y")
    assert e.status == "queued"  # playback holds the only slot
    time.sleep(0.3)
    assert e.status == "queued"
    playing["n"] = 0
    wait_for(lambda: e.bytes_done > 100_000)
    playing["n"] = 1  # user starts watching: download yields
    wait_for(lambda: e.status == "queued")
    assert (mgr.dir / "Y.mkv.part").exists()
    playing["n"] = 0
    wait_for(lambda: e.status == "done", 20)
    assert (mgr.dir / "Y.mkv").read_bytes() == DATA
    mgr.shutdown()


def test_speed_and_eta(tmp_path: Path, provider: FakeProvider) -> None:
    provider.delay = 0.05
    mgr = make_manager(tmp_path, provider)
    e = mgr.add("movie", "1", title="S")
    wait_for(lambda: mgr.public(e)["speed_bps"] > 0)
    pub = mgr.public(e)
    assert pub["eta_seconds"] is not None and pub["eta_seconds"] >= 0
    mgr.cancel(e.id)


# --------------------------------------------------------------------- HTTP


@pytest.fixture
def api(tmp_path: Path, provider: FakeProvider) -> Iterator[Any]:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mgr = make_manager(tmp_path, provider)
    downloads.set_manager(mgr)

    def describe(kind: str, item_id: str, source_id: str, ext: str, series_id: int | None):
        return {"source_id": "s1", "ext": ext or "mkv", "title": f"T{item_id}", "poster": "p.jpg"}

    app = FastAPI()
    app.include_router(downloads.build_router(lambda: {"sub": "u"}, describe))
    c = TestClient(app)
    c.mgr = mgr  # type: ignore[attr-defined]
    yield c
    downloads.set_manager(None)


def test_api_flow(api: Any, provider: FakeProvider) -> None:
    provider.delay = 0.02
    r = api.post("/api/downloads", json={"kind": "movie", "id": "7", "title": "Seven"})
    assert r.status_code == 200
    d = r.json()["download"]
    assert d["title"] == "Seven" and d["poster"] == "p.jpg"
    again = api.post("/api/downloads", json={"kind": "movie", "id": "7"}).json()["download"]
    assert again["id"] == d["id"]

    lst = api.get("/api/downloads").json()
    assert lst["dir"] == str(api.mgr.dir) and lst["free_bytes"] > 0
    assert [x["id"] for x in lst["downloads"]] == [d["id"]]

    key = "movie:7"
    lk = api.get("/api/downloads/lookup", params={"keys": f"{key},episode:1"}).json()
    assert lk[key]["download_id"] == d["id"] and "episode:1" not in lk
    st = api.get("/api/downloads/status", params={"kind": "movie", "id": "7"}).json()
    assert st["download_id"] == d["id"]
    assert api.get("/api/downloads/status", params={"kind": "movie", "id": "8"}).json() == {
        "status": None,
        "download_id": None,
    }
    assert api.get("/api/downloads/status", params={"ids": key}).json()[key]["status"]

    assert api.post(f"/api/downloads/{d['id']}/pause").json()["download"]["status"] == "paused"
    assert api.post(f"/api/downloads/{d['id']}/resume").json()["download"]["status"] in (
        "queued",
        "downloading",
    )
    wait_for(lambda: api.mgr.get(d["id"]).status == "done")

    full = api.get(f"/downloads/files/{d['id']}")
    assert full.status_code == 200 and full.content == DATA
    assert full.headers["content-type"] == "video/x-matroska"
    part = api.get(f"/downloads/files/{d['id']}", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == DATA[10:20]
    assert part.headers["content-range"] == f"bytes 10-19/{SIZE}"
    tail = api.get(f"/downloads/files/{d['id']}", headers={"Range": "bytes=-5"})
    assert tail.content == DATA[-5:]

    assert api.delete(f"/api/downloads/{d['id']}").json() == {"ok": True}
    assert api.get(f"/downloads/files/{d['id']}").status_code == 404
    assert api.post("/api/downloads/nope/pause").status_code == 404


def test_api_validation_and_unfinished_file(api: Any, provider: FakeProvider) -> None:
    assert api.post("/api/downloads", json={"kind": "tv", "id": "1"}).status_code == 422
    assert api.post("/api/downloads", json={"kind": "movie", "id": "../x"}).status_code == 400
    assert api.get("/downloads/files/..%2f..%2fetc%2fpasswd").status_code == 404
    provider.delay = 0.05
    d = api.post("/api/downloads", json={"kind": "episode", "id": "5"}).json()["download"]
    assert api.get(f"/downloads/files/{d['id']}").status_code == 409
    c = api.post(f"/api/downloads/{d['id']}/cancel").json()["download"]
    assert c["status"] == "cancelled"


# ------------------------------------------------------------------- online


def test_online_timeout_never_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def black_hole(host: str, port: int) -> Any:
        await asyncio.sleep(60)

    monkeypatch.setattr(asyncio, "open_connection", black_hole)
    t0 = time.time()
    assert asyncio.run(downloads.check_online([("10.255.255.1", 81)], timeout=0.5)) is False
    assert time.time() - t0 < 1.5


def test_online_reachable_unreachable_and_no_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    try:
        assert asyncio.run(downloads.check_online([("127.0.0.1", port)])) is True
        assert asyncio.run(downloads.check_online([("127.0.0.1", dead_port)])) is False
        assert asyncio.run(downloads.check_online([("127.0.0.1", dead_port), ("127.0.0.1", port)]))
        assert asyncio.run(downloads.check_online([("nonexistent.invalid", 80)])) is False
        assert asyncio.run(downloads.check_online([])) is True

        def boom() -> list:
            raise RuntimeError("x")

        monkeypatch.setattr(downloads, "_source_endpoints", boom)
        assert asyncio.run(downloads.check_online()) is False
    finally:
        srv.close()


def test_api_online_route(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def fake(*a: Any, **k: Any) -> bool:
        return False

    monkeypatch.setattr(downloads, "check_online", fake)
    app = FastAPI()
    app.include_router(downloads.build_router(lambda: {}))
    assert TestClient(app).get("/api/online").json() == {"online": False}


def test_safe_name_helpers() -> None:
    assert downloads.safe_name('a/b\\c:d*e?"f<g>h|i', "x") == "a b c d e f g h i"
    assert downloads.safe_name("...", "fallback") == "fallback"
    assert len(downloads.safe_name("x" * 500, "f")) <= 150
    assert downloads.safe_ext("../MKV!") == "mkv"
    assert downloads.safe_ext("") == "mkv"


# ---------------------------------------------------------------- slow connection watchdog


def _feed(w: SlowWatch, start: float, seconds: int, rate: float, done: float) -> tuple[float, bool]:
    """Feed one sample per second at ``rate`` bytes/s until it asks to reconnect.

    Returns (done, second of the reconnect request or -1), like the real read loop
    which drops the connection at the first request.
    """
    for i in range(1, seconds + 1):
        done += rate
        if w.feed(start + i, int(done)):
            return done, i
    return done, -1


def test_slowwatch_steady_speed_never_reconnects() -> None:
    w = SlowWatch(window=1, patience=3, fast_enough=1e12)
    w.new_connection(0, 0)
    _, fired = _feed(w, 0, 120, 10e6, 0)
    assert fired < 0 and w.reconnects == 0


def test_slowwatch_detects_throttle_after_patience() -> None:
    w = SlowWatch(window=1, patience=3, fast_enough=1e12)
    w.new_connection(0, 0)
    done, fired = _feed(w, 0, 60, 15e6, 0)  # fast for a minute
    assert fired < 0
    _, fired = _feed(w, 60, 20, 1.4e6, done)  # then throttled to 1.4 MB/s
    assert 3 <= fired <= 5  # about `patience` seconds after the drop
    assert w.reconnects == 1


def test_slowwatch_ignores_speeds_that_are_already_fast() -> None:
    w = SlowWatch(window=1, patience=3)  # default fast_enough = 8 MB/s
    w.new_connection(0, 0)
    done, _ = _feed(w, 0, 30, 40e6, 0)
    _, fired = _feed(w, 30, 30, 12e6, done)  # 3x slower than best, but still fast
    assert fired < 0


def test_slowwatch_backs_off_when_reconnect_does_not_help_and_resets_when_it_does() -> None:
    w = SlowWatch(window=1, patience=3, fast_enough=1e12, max_patience=100)
    w.new_connection(0, 0)
    done, _ = _feed(w, 0, 30, 15e6, 0)
    done, fired = _feed(w, 30, 20, 1.4e6, done)
    assert fired > 0 and w.patience == 3
    # reconnect, still slow: patience doubles (6), then 12
    w.new_connection(50, done)
    done, fired = _feed(w, 50, 30, 1.4e6, done)
    assert fired >= 6 and w.patience == 6
    w.new_connection(80, done)
    done, fired = _feed(w, 80, 40, 1.4e6, done)
    assert fired >= 12 and w.patience == 12
    # a reconnect that restores the speed resets the patience
    w.new_connection(120, done)
    done, _ = _feed(w, 120, 10, 15e6, done)
    assert w.patience == 3


def test_slowwatch_reference_rate_decays_when_provider_is_just_slow() -> None:
    w = SlowWatch(window=1, patience=3, fast_enough=1e12)
    w.new_connection(0, 0)
    done, _ = _feed(w, 0, 10, 15e6, 0)
    fired_at = []
    t = 10
    for _ in range(40):  # a long stretch at 3 MB/s (still 5x slower than the old best)
        done, fired = _feed(w, t, 30, 3e6, done)
        fired_at.append(fired)
        t += 30
        if fired > 0:
            w.new_connection(t, done)
    assert fired_at[-1] < 0 and fired_at[-2] < 0  # eventually it stops churning


def test_slow_connection_is_replaced_and_file_stays_intact(
    tmp_path: Path, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(downloads, "CHUNK_SIZE", 50_000)  # one read per server chunk
    provider.delay = 0.01
    provider.slow_after = 300_000
    provider.slow_delay = 0.15
    mgr = make_manager(
        tmp_path,
        provider,
        slow_watch=lambda: SlowWatch(window=0.06, patience=0.15, fast_enough=1e12),
    )
    e = mgr.add("movie", "1", title="Throttled")
    wait_for(lambda: e.status == "done")
    assert (mgr.dir / e.filename).read_bytes() == DATA
    hits = [r for _, r in provider.hits]
    assert len(hits) >= 2 and hits[0] is None
    assert hits[1] and hits[1].startswith("bytes=") and hits[1] != "bytes=0-"


def _conn(w: SlowWatch, now: float, done: float, length: int) -> None:
    w.new_connection(now, int(done), length)


def _run_conn(
    w: SlowWatch,
    now: float,
    done: float,
    length: int,
    fast_rate: float,
    burst: float,
    slow_rate: float,
) -> tuple[float, float, str]:
    """Simulate one connection sampled every 0.1 s. Returns (now, done, why it ended)."""
    _conn(w, now, done, length)
    start = done
    while done - start < length:
        now += 0.1
        rate = fast_rate if done - start < burst else slow_rate
        done += min(rate * 0.1, length - (done - start))
        if w.feed(now, int(done)):
            return now, done, "slow"
        if w.should_rotate(int(done)):
            return now, done, "rotate"
    return now, done, "eof"


def test_slowwatch_learns_burst_fraction_then_rotates_before_the_throttle() -> None:
    w = SlowWatch(window=1, patience=3, fast_enough=1e12, min_burst=1_000_000)
    total = 600_000_000
    now, done, why = _run_conn(w, 0, 0, total, 30e6, 0.15 * total, 1e6)
    assert why == "slow" and w.fraction == 0  # nothing known yet, the watchdog does the job
    now, done, why = _run_conn(w, now, done, total - int(done), 30e6, 0.15 * (total - done), 1e6)
    assert why == "rotate" and 0.12 <= w.fraction <= 0.2  # learned from the first knee
    for _ in range(5):  # every next connection is cut right before its throttle starts
        length = total - int(done)
        start = done
        now, done, why = _run_conn(w, now, done, length, 30e6, 0.15 * length, 1e6)
        assert why == "rotate"
        assert 0.10 * length <= done - start <= 0.155 * length
    assert w.rotations >= 5


def test_slowwatch_rotation_guards() -> None:
    w = SlowWatch(burst_fraction=0.15, min_burst=4_000_000)
    _conn(w, 0, 0, 0)  # unknown length: never rotate
    assert not w.should_rotate(10**9)
    _conn(w, 0, 0, 20_000_000)  # 15 % of 20 MB is 3 MB, below min_burst
    assert not w.should_rotate(3_000_000)
    _conn(w, 0, 0, 100_000_000)
    assert not w.should_rotate(10_000_000)  # burst not spent yet
    assert w.should_rotate(15_000_000)
    off = SlowWatch(burst_fraction=0.15, rotate=False)
    _conn(off, 0, 0, 100_000_000)
    assert not off.should_rotate(50_000_000)
    unknown = SlowWatch()  # provider that never throttles: fraction stays unknown
    _conn(unknown, 0, 0, 100_000_000)
    for i in range(1, 30):
        assert not unknown.feed(i, i * 10_000_000)
    assert not unknown.should_rotate(300_000_000) and unknown.fraction == 0


def test_slowwatch_corrects_a_fraction_that_is_too_optimistic() -> None:
    w = SlowWatch(window=1, patience=3, fast_enough=1e12, burst_fraction=0.3, min_burst=1_000_000)
    total = 600_000_000
    # the real burst is 15 %: the planned cut (28 %) comes after the throttle started
    now, done, why = _run_conn(w, 0, 0, total, 30e6, 0.15 * total, 1e6)
    assert why == "slow"
    _conn(w, now, done, total - int(done))
    assert 0.12 <= w.fraction <= 0.2


def _burst_run(
    tmp_path: Path, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch, rotate: bool
) -> tuple[DownloadManager, Any]:
    monkeypatch.setattr(downloads, "CHUNK_SIZE", 5_000)
    provider.chunk = 5_000
    provider.burst_frac = 0.3
    provider.slow_delay = 0.03
    provider.slow_bytes = 0
    provider.hits.clear()
    mgr = make_manager(
        tmp_path / str(rotate),
        provider,
        slow_watch=lambda: SlowWatch(
            window=0.05,
            patience=0.3,
            fast_enough=1e12,
            knee_window=0.03,
            min_burst=20_000,
            rotate=rotate,
        ),
    )
    e = mgr.add("movie", "1", title="Burst")
    wait_for(lambda: e.status == "done", timeout=60)
    assert (mgr.dir / e.filename).read_bytes() == DATA
    return mgr, e


def test_burst_limited_provider_is_rotated_and_file_intact(
    tmp_path: Path, provider: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _ = _burst_run(tmp_path, provider, monkeypatch, rotate=False)
    plain_slow, plain_hits = provider.slow_bytes, len(provider.hits)
    mgr, _ = _burst_run(tmp_path, provider, monkeypatch, rotate=True)
    assert len(provider.hits) >= 4 and plain_hits >= 2  # first connection, then rotations
    # rotation stops each connection right before its throttle starts
    assert provider.slow_bytes < 0.5 * plain_slow
    assert mgr._fractions  # learned fraction remembered per provider host


def test_part_file_already_complete_is_finished_not_deleted(
    tmp_path: Path, provider: FakeProvider
) -> None:
    # The .part holds the whole file but the entry was never finalized (e.g. the
    # bytes arrived through another channel): the provider answers 416 to
    # "bytes=SIZE-" and we must finish the download instead of deleting the file.
    provider.delay = 0.05
    mgr = make_manager(tmp_path, provider)
    e = mgr.add("movie", "1", title="Whole", ext="mkv")
    wait_for(lambda: e.bytes_done > 50_000)
    mgr.pause(e.id)
    wait_for(lambda: e.id not in mgr._running)
    (mgr.dir / (e.filename + ".part")).write_bytes(DATA)
    provider.status_override = 416
    mgr.resume(e.id)
    wait_for(lambda: e.status == "done")
    assert (mgr.dir / e.filename).read_bytes() == DATA
    assert not (mgr.dir / (e.filename + ".part")).exists()
