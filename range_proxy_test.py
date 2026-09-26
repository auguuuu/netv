"""Range proxy against a local fake provider (302 chain, stalls, single connection)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import http.client
import re
import threading
import time

import pytest

from range_proxy import RangeProxy


DATA = bytes(range(256)) * 4096  # 1 MiB
SIZE = len(DATA)


class Provider:
    def __init__(self, hang_starts: tuple[int, ...] = (), hang_always: bool = False):
        self.hang_starts = set(hang_starts)
        self.hang_always = hang_always
        self.play_requests: list[int] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/movie/"):
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{outer.port}/live/play/tok/1")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", "bytes=0-"))
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else SIZE - 1
                with outer.lock:
                    outer.play_requests.append(start)
                    outer.active += 1
                    outer.max_active = max(outer.max_active, outer.active)
                    hang = start in outer.hang_starts
                    if hang and not outer.hang_always:
                        outer.hang_starts.discard(start)
                try:
                    if hang:
                        time.sleep(3)
                        return
                    body = DATA[start : end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{SIZE}")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass
                finally:
                    with outer.lock:
                        outer.active -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/movie/u/p/1.mp4"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def fetch(local_url: str, rng: str | None, timeout: float = 15.0) -> tuple[int, bytes]:
    m = re.match(r"http://127.0.0.1:(\d+)(/.*)", local_url)
    assert m
    c = http.client.HTTPConnection("127.0.0.1", int(m.group(1)), timeout=timeout)
    c.request("GET", m.group(2), headers={"Range": rng} if rng else {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


@pytest.fixture
def proxy():
    p = RangeProxy(header_timeouts=(0.5, 0.8, 1.0), body_stall_timeout=1.0)
    yield p
    p.stop()


@pytest.fixture
def provider():
    made: list[Provider] = []

    def make(**kw):
        pr = Provider(**kw)
        made.append(pr)
        return pr

    yield make
    for pr in made:
        pr.close()


def test_serves_ranges_through_the_redirect(proxy, provider):
    pr = provider()
    url = proxy.local_url(pr.url)
    st, body = fetch(url, "bytes=0-")
    assert st == 206 and body == DATA
    st, body = fetch(url, "bytes=1000-1999")
    assert body == DATA[1000:2000]
    st, body = fetch(url, "bytes=900000-")
    assert body == DATA[900000:]


def test_head_and_tail_are_cached_across_runs(proxy, provider):
    pr = provider()
    fetch(proxy.local_url(pr.url), "bytes=0-")
    fetch(proxy.local_url(pr.url), "bytes=1000000-")
    seen = len(pr.play_requests)
    # a second ffmpeg run on the same URL: header, moov and probing reads cost nothing
    url2 = proxy.local_url(pr.url)
    assert fetch(url2, "bytes=48-1047")[1] == DATA[48:1048]
    assert fetch(url2, "bytes=1000000-")[1] == DATA[1000000:]
    assert len(pr.play_requests) == seen


def test_stalled_request_is_retried_with_a_fresh_connection(proxy, provider):
    pr = provider(hang_starts=(500000,))
    url = proxy.local_url(pr.url)
    fetch(url, "bytes=0-100")
    t0 = time.monotonic()
    st, body = fetch(url, "bytes=500000-500999")
    assert st == 206 and body == DATA[500000:501000]
    assert 0.4 < time.monotonic() - t0 < 3
    assert proxy.stats["retries"] == 1
    assert pr.play_requests.count(500000) == 2


def test_gives_up_with_502_when_provider_never_answers(proxy, provider):
    pr = provider(hang_starts=(500000,), hang_always=True)
    url = proxy.local_url(pr.url)
    fetch(url, "bytes=0-100")
    st, _ = fetch(url, "bytes=500000-500999")
    assert st == 502
