"""Local range-caching HTTP proxy for VOD inputs of ffmpeg.

Why: ffmpeg reads an MP4 whose moov atom sits at the end with several short
HTTP requests (bytes=0-, moov at the end, bytes=48-, then the seek target),
each one a fresh connection through the provider's 302 redirect, and it opens
the next connection *before* closing the previous one.  Some providers stall
the last of those requests forever, and ffmpeg then waits for response
headers without any timeout.

The proxy sits between ffmpeg and the provider and:

- keeps at most ONE upstream connection per ffmpeg input (a new request closes
  the previous upstream first, so the provider never sees overlapping reads);
- opens upstream connections lazily, only when the client really consumes
  bytes that are not cached;
- caches the head of the file and the tail (moov) once per URL, so later
  seeks only hit the provider for the seek target itself;
- gives every upstream request a header timeout and retries a hung request
  with a fresh connection (re-resolving the redirect), and resumes a body that
  stalls from the last byte received.
"""

from __future__ import annotations

from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import contextlib
import http.client
import logging
import os
import re
import socket
import socketserver
import threading
import time
import urllib.parse
import uuid


log = logging.getLogger(__name__)

HEAD_CACHE_BYTES = 1 << 20
TAIL_CACHE_MAX_BYTES = 32 << 20
HEADER_TIMEOUTS_SEC = (6.0, 8.0, 12.0)
BODY_STALL_TIMEOUT_SEC = 20.0
MAX_BODY_RESUMES = 3
MAX_REDIRECTS = 5
_MAX_ORIGINS = 64
_MAX_HANDLES = 512
_CHUNK = 64 * 1024

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class UpstreamError(Exception):
    pass


class _Origin:
    """Per-URL knowledge shared by every ffmpeg run reading that URL."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.size = 0
        self.head = b""
        self.tail_start = 0
        self.tail = b""
        self.lock = threading.Lock()


class _Handle:
    """One ffmpeg input: owns the single upstream connection."""

    def __init__(self, origin: _Origin) -> None:
        self.origin = origin
        self.lock = threading.Lock()
        self.upstream: http.client.HTTPConnection | None = None
        self.client: Any = None  # socket of the request currently being served
        self.generation = 0


def _open_upstream_once(
    url: str, start: int, end: int | None, headers: dict[str, str], timeout: float
) -> tuple[http.client.HTTPResponse, http.client.HTTPConnection, int, int]:
    """GET url with a Range, following redirects.

    Returns (response, connection, first_byte, total_size).
    """
    for _ in range(MAX_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise UpstreamError(f"unsupported url scheme {parts.scheme!r}")
        conn_cls = (
            http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        )
        conn = conn_cls(parts.hostname, parts.port, timeout=timeout)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        h = dict(headers)
        h["Range"] = f"bytes={start}-{'' if end is None else end}"
        h["Connection"] = "close"
        try:
            conn.request("GET", path, headers=h)
            resp = conn.getresponse()
        except Exception:
            conn.close()
            raise
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            resp.close()
            conn.close()
            if not loc:
                raise UpstreamError("redirect without Location")
            url = urllib.parse.urljoin(url, loc)
            continue
        if resp.status not in (200, 206):
            status = resp.status
            resp.close()
            conn.close()
            raise UpstreamError(f"upstream status {status}")
        total = 0
        first = start
        cr = resp.getheader("Content-Range")
        if resp.status == 206 and cr:
            m = re.match(r"bytes (\d+)-(\d+)/(\d+|\*)", cr)
            if m:
                first = int(m.group(1))
                total = int(m.group(3)) if m.group(3) != "*" else 0
        elif resp.status == 200:
            # Provider ignored the Range: body starts at byte 0
            first = 0
            total = int(resp.getheader("Content-Length") or 0)
            if start > 0:
                _skip(resp, start)
                first = start
        return resp, conn, first, total
    raise UpstreamError("too many redirects")


def _skip(resp: http.client.HTTPResponse, count: int) -> None:
    while count > 0:
        chunk = resp.read(min(_CHUNK, count))
        if not chunk:
            raise UpstreamError("short body while skipping")
        count -= len(chunk)


class RangeProxy:
    def __init__(
        self,
        header_timeouts: tuple[float, ...] = HEADER_TIMEOUTS_SEC,
        body_stall_timeout: float = BODY_STALL_TIMEOUT_SEC,
    ) -> None:
        self.header_timeouts = header_timeouts
        self.body_stall_timeout = body_stall_timeout
        self._origins: OrderedDict[str, _Origin] = OrderedDict()
        self._handles: OrderedDict[str, _Handle] = OrderedDict()
        self._lock = threading.Lock()
        self._server: _Server | None = None
        self.stats: dict[str, int] = {"upstream_opens": 0, "retries": 0, "cache_hits": 0}

    # -- lifecycle -----------------------------------------------------
    def start(self) -> int:
        with self._lock:
            if self._server is None:
                self._server = _Server(("127.0.0.1", 0), _Handler)
                self._server.proxy = self
                threading.Thread(
                    target=self._server.serve_forever, name="range-proxy", daemon=True
                ).start()
            return self._server.server_address[1]

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()

    def local_url(self, url: str) -> str:
        """Local URL ffmpeg should read instead of `url` (one handle per ffmpeg run)."""
        port = self.start()
        with self._lock:
            origin = self._origins.pop(url, None) or _Origin(url)
            self._origins[url] = origin
            while len(self._origins) > _MAX_ORIGINS:
                self._origins.popitem(last=False)
            hid = uuid.uuid4().hex
            self._handles[hid] = _Handle(origin)
            while len(self._handles) > _MAX_HANDLES:
                self._handles.popitem(last=False)
        ext = urllib.parse.urlsplit(url).path.rsplit(".", 1)[-1]
        suffix = f".{ext}" if ext and len(ext) <= 5 and ext.isalnum() else ""
        return f"http://127.0.0.1:{port}/p/{hid}{suffix}"

    def _get_handle(self, hid: str) -> _Handle | None:
        with self._lock:
            return self._handles.get(hid)

    # -- upstream --------------------------------------------------------
    def _open(
        self,
        handle: _Handle,
        gen: int,
        start: int,
        end: int | None,
        headers: dict[str, str],
    ) -> tuple[http.client.HTTPResponse, http.client.HTTPConnection, int, int]:
        """Open an upstream body at `start`, one connection at a time, with retries."""
        last: Exception | None = None
        for attempt, timeout in enumerate(self.header_timeouts):
            if gen != handle.generation:
                raise Superseded
            self._drop_upstream(handle)
            try:
                self.stats["upstream_opens"] += 1
                resp, conn, first, total = _open_upstream_once(
                    handle.origin.url, start, end, headers, timeout
                )
            except (OSError, http.client.HTTPException, UpstreamError) as e:
                last = e
                self.stats["retries"] += 1
                log.warning(
                    "range-proxy: upstream open failed at byte %d (attempt %d/%d, %.0fs): %s",
                    start,
                    attempt + 1,
                    len(self.header_timeouts),
                    timeout,
                    e,
                )
                continue
            if gen != handle.generation:
                conn.close()
                raise Superseded
            if conn.sock is not None:
                conn.sock.settimeout(self.body_stall_timeout)
            handle.upstream = conn
            return resp, conn, first, total
        raise UpstreamError(f"upstream unavailable: {last}")

    @staticmethod
    def _drop_upstream(handle: _Handle) -> None:
        conn, handle.upstream = handle.upstream, None
        _shutdown(conn)

    @staticmethod
    def _release(handle: _Handle, conn: http.client.HTTPConnection | None) -> None:
        if conn is None:
            return
        _shutdown(conn)
        if handle.upstream is conn:
            handle.upstream = None

    def _fill_tail(self, handle: _Handle, gen: int, start: int, headers: dict[str, str]) -> None:
        """Fetch [start, EOF) once (the moov of a non faststart MP4)."""
        origin = handle.origin
        resp, conn, first, total = self._open(handle, gen, start, None, headers)
        try:
            data = resp.read()
        except (OSError, http.client.HTTPException) as e:
            raise UpstreamError(f"tail read failed: {e}") from e
        finally:
            self._release(handle, conn)
        if total and first == start and first + len(data) == total:
            with origin.lock:
                origin.size = total
                origin.tail_start, origin.tail = start, data

    # -- serving ---------------------------------------------------------
    def serve(
        self,
        hid: str,
        range_header: str | None,
        headers: dict[str, str],
        wfile: Any,
        client: Any = None,
    ) -> int:
        """Serve one client request. Returns the HTTP status sent."""
        handle = self._get_handle(hid)
        if handle is None:
            return _send_status(wfile, 404)
        origin = handle.origin
        # A newer request supersedes the older one: closing its upstream first
        # means the provider never sees two overlapping connections.
        with self._lock:
            handle.generation += 1
            gen = handle.generation
            old_client, handle.client = handle.client, client
        self._drop_upstream(handle)
        if old_client is not None:
            # The older request may be blocked writing to a client that no longer reads
            with contextlib.suppress(OSError):
                old_client.shutdown(socket.SHUT_RDWR)
        with handle.lock:
            if gen != handle.generation:
                return _send_status(wfile, 503)
            start, end = 0, None
            m = _RANGE_RE.match(range_header or "")
            if m and m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else None
            elif m and m.group(2) and origin.size:
                start = max(0, origin.size - int(m.group(2)))
            if origin.size and start >= origin.size:
                return _send_status(wfile, 416)
            try:
                return self._serve_range(handle, gen, start, end, bool(m), headers, wfile)
            except Superseded:
                return 503
            except UpstreamError as e:
                log.warning("range-proxy: %s", e)
                return _send_status(wfile, 502)

    def _cached(self, origin: _Origin, start: int) -> bytes | None:
        with origin.lock:
            if origin.tail and origin.tail_start <= start < origin.tail_start + len(origin.tail):
                return origin.tail[start - origin.tail_start :]
            if origin.head and start < len(origin.head):
                return origin.head[start:]
        return None

    def _serve_range(
        self,
        handle: _Handle,
        gen: int,
        start: int,
        end: int | None,
        had_range: bool,
        headers: dict[str, str],
        wfile: Any,
    ) -> int:
        origin = handle.origin
        if (
            origin.size
            and start >= origin.size - TAIL_CACHE_MAX_BYTES
            and self._cached(origin, start) is None
        ):
            self._fill_tail(handle, gen, start, headers)
        cached = self._cached(origin, start)
        resp: http.client.HTTPResponse | None = None
        conn: http.client.HTTPConnection | None = None
        if cached is None:
            resp, conn, first, total = self._open(handle, gen, start, end, headers)
            if total:
                with origin.lock:
                    origin.size = total
            if first != start:
                self._release(handle, conn)
                raise UpstreamError("upstream returned a different start offset")
        size = origin.size
        if not size:
            self._release(handle, conn)
            raise UpstreamError("unknown size")
        last = size - 1 if end is None else min(end, size - 1)
        if start > last:
            self._release(handle, conn)
            return _send_status(wfile, 416)
        length = last - start + 1
        if had_range:
            head = f"HTTP/1.1 206 Partial Content\r\nContent-Range: bytes {start}-{last}/{size}\r\n"
        else:
            head = "HTTP/1.1 200 OK\r\n"
        head += (
            f"Accept-Ranges: bytes\r\nContent-Length: {length}\r\n"
            "Content-Type: video/mp4\r\nConnection: close\r\n\r\n"
        )
        pos = start
        resumes = 0
        try:
            wfile.write(head.encode())
            if cached is not None:
                self.stats["cache_hits"] += 1
                chunk = cached[: last - pos + 1]
                wfile.write(chunk)
                pos += len(chunk)
            while pos <= last:
                if gen != handle.generation:
                    break  # superseded by a newer request: the client is gone
                if resp is None:
                    resp, conn, first, _ = self._open(handle, gen, pos, end, headers)
                    if first != pos:
                        raise UpstreamError("upstream returned a different start offset")
                data = self._read(resp, min(_CHUNK, last - pos + 1))
                if not data:
                    # stalled or dropped mid body: resume from the last byte
                    self._release(handle, conn)
                    resp = conn = None
                    if data is not None and pos > last:
                        break
                    resumes += 1
                    if resumes > MAX_BODY_RESUMES:
                        break  # give up: the short body makes the client reconnect
                    continue
                if len(origin.head) == pos and pos < HEAD_CACHE_BYTES:
                    with origin.lock:
                        if len(origin.head) == pos:
                            origin.head += data[: HEAD_CACHE_BYTES - pos]
                wfile.write(data)
                pos += len(data)
        except (BrokenPipeError, ConnectionError, Superseded):
            pass
        finally:
            self._release(handle, conn)
        return 206 if had_range else 200

    @staticmethod
    def _read(resp: http.client.HTTPResponse, n: int) -> bytes | None:
        """Next chunk, b"" at EOF, None on stall or connection error."""
        try:
            return resp.read1(n)
        except (OSError, http.client.HTTPException):
            return None


class Superseded(Exception):
    pass


def _shutdown(conn: Any) -> None:
    """Wake any thread blocked on the connection, then close it."""
    if conn is None:
        return
    sock = getattr(conn, "sock", conn)
    try:
        if sock is not None and hasattr(sock, "shutdown"):
            sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    with contextlib.suppress(Exception):
        conn.close()


def _send_status(wfile: Any, status: int) -> int:
    with contextlib.suppress(BrokenPipeError, ConnectionError):
        wfile.write(
            f"HTTP/1.1 {status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
    return status


class _Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    proxy: RangeProxy


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        m = re.match(r"/p/([0-9a-f]{32})", self.path)
        if not m:
            self.wfile.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            return
        fwd = {}
        ua = self.headers.get("User-Agent")
        if ua:
            fwd["User-Agent"] = ua
        started = time.monotonic()
        status = self.server.proxy.serve(  # type: ignore[attr-defined]
            m.group(1), self.headers.get("Range"), fwd, self.wfile, self.connection
        )
        log.debug(
            "range-proxy %s %s -> %s in %.2fs",
            m.group(1)[:6],
            self.headers.get("Range"),
            status,
            time.monotonic() - started,
        )


_proxy: RangeProxy | None = None
_proxy_lock = threading.Lock()


def get_proxy() -> RangeProxy:
    global _proxy
    with _proxy_lock:
        if _proxy is None:
            _proxy = RangeProxy()
        return _proxy


def proxied_input_url(url: str) -> str:
    """Local proxy URL for a plain remote VOD file, else the URL unchanged."""
    if not url.startswith(("http://", "https://")) or ".m3u8" in url.lower():
        return url
    return get_proxy().local_url(url)


def range_proxy_enabled(settings: dict[str, Any] | None = None) -> bool:
    """Opt in with the `range_proxy` setting or NETV_RANGE_PROXY=1 (default off)."""
    if os.environ.get("NETV_RANGE_PROXY", "").lower() in ("1", "true", "yes"):
        return True
    return bool((settings or {}).get("range_proxy", False))
