"""Original language (VO) of a movie or episode.

Sources, most reliable first (each optional, every failure swallowed):

1. ``tmdb``: TMDB ``original_language`` (needs a key), cached on disk 30 days.
2. ``flag``: ffprobe disposition ``original`` (or a track title "original"/"VO")
   on a local file.
3. ``country``: guess from the provider's ``info.country`` string.

``lookup`` returns ``(lang, source)`` with lang a lowercase ISO 639-1 code, or
``("", "")`` when unknown.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import json
import logging
import os
import pathlib
import re
import subprocess
import threading
import time
import urllib.request


log = logging.getLogger(__name__)

TTL = 30 * 86400
TMDB_TIMEOUT = 4.0
DEFAULT_WAIT = 1.5

# Country / language names (lowercase) to ISO 639-1
COUNTRY_LANG: dict[str, str] = {
    "united states": "en", "usa": "en", "us": "en", "united kingdom": "en", "uk": "en",
    "great britain": "en", "england": "en", "canada": "en", "australia": "en",
    "new zealand": "en", "ireland": "en", "english": "en",
    "france": "fr", "belgium": "fr", "french": "fr", "francais": "fr", "français": "fr",
    "japan": "ja", "japanese": "ja",
    "south korea": "ko", "korea": "ko", "korean": "ko",
    "germany": "de", "austria": "de", "german": "de", "deutsch": "de",
    "italy": "it", "italian": "it",
    "spain": "es", "mexico": "es", "argentina": "es", "colombia": "es", "chile": "es",
    "spanish": "es",
    "portugal": "pt", "brazil": "pt", "portuguese": "pt",
    "netherlands": "nl", "dutch": "nl",
    "russia": "ru", "russian": "ru", "ukraine": "uk",
    "china": "zh", "hong kong": "zh", "taiwan": "zh", "chinese": "zh", "mandarin": "zh",
    "india": "hi", "hindi": "hi",
    "turkey": "tr", "turkish": "tr",
    "sweden": "sv", "swedish": "sv",
    "denmark": "da", "danish": "da",
    "norway": "no", "norwegian": "no",
    "finland": "fi", "finnish": "fi",
    "poland": "pl", "polish": "pl",
    "greece": "el", "greek": "el",
    "thailand": "th", "thai": "th",
    "iran": "fa", "persian": "fa",
    "israel": "he", "hebrew": "he",
    "czech republic": "cs", "czechia": "cs", "hungary": "hu", "romania": "ro",
    "indonesia": "id", "vietnam": "vi", "saudi arabia": "ar", "egypt": "ar",
    "arabic": "ar",
}  # fmt: skip
_ENGLISH_COUNTRIES = {
    "united states", "usa", "us", "united kingdom", "uk", "great britain", "england",
    "canada", "australia", "new zealand", "ireland",
}  # fmt: skip
_LANG_NAMES = {
    n
    for n in ["english", "french", "japanese", "korean", "german", "italian", "spanish", "portuguese", "dutch", "russian", "chinese", "mandarin", "hindi", "turkish", "swedish", "danish", "norwegian", "finnish", "polish", "greek", "thai", "persian", "hebrew", "arabic", "francais", "français", "deutsch"]
}

# ISO 639-2 to 639-1 for ffprobe tags
LANG_2 = {
    "eng": "en", "fre": "fr", "fra": "fr", "jpn": "ja", "spa": "es", "ger": "de", "deu": "de",
    "ita": "it", "por": "pt", "rus": "ru", "chi": "zh", "zho": "zh", "kor": "ko", "ara": "ar",
    "dut": "nl", "nld": "nl", "pol": "pl", "tur": "tr", "hin": "hi", "swe": "sv", "dan": "da",
    "nor": "no", "fin": "fi", "gre": "el", "ell": "el", "heb": "he", "cze": "cs", "ces": "cs",
    "rum": "ro", "ron": "ro", "hun": "hu", "ukr": "uk", "tha": "th", "vie": "vi", "ind": "id",
    "per": "fa", "fas": "fa", "cat": "ca",
}  # fmt: skip


def country_to_lang(value: Any) -> str:
    """Guess a language from a provider ``country`` string ("" when unknown)."""
    if not isinstance(value, str) or not value.strip():
        return ""
    parts = [p.strip().lower() for p in re.split(r"[,/;|&]+", value) if p.strip()]
    if not parts:
        return ""
    first = COUNTRY_LANG.get(parts[0], "")
    if first == "en" and parts[0] in _ENGLISH_COUNTRIES:
        # A language name in the list wins over a generic English-speaking country
        for p in parts[1:]:
            if p in _LANG_NAMES and COUNTRY_LANG.get(p) not in ("", "en"):
                return COUNTRY_LANG[p]
    if first:
        return first
    # Unknown first entry: maybe a multi-word value, try the whole string
    return COUNTRY_LANG.get(" ".join(parts), "")


# ---------------------------------------------------------------------------
# File flag (ffprobe)
# ---------------------------------------------------------------------------

_ORIGINAL_TITLE = re.compile(r"\b(original|vo|v\.o\.|ov)\b", re.I)


def parse_flag(data: dict[str, Any]) -> str:
    """Language of the audio track flagged original in ffprobe JSON ("" if none)."""
    for s in data.get("streams") or []:
        if s.get("codec_type") != "audio":
            continue
        tags = {str(k).lower(): v for k, v in (s.get("tags") or {}).items()}
        disp = s.get("disposition") or {}
        flagged = bool(disp.get("original")) or bool(
            _ORIGINAL_TITLE.search(str(tags.get("title", "")))
        )
        if not flagged:
            continue
        lang = str(tags.get("language", "")).strip().lower()
        if len(lang) == 3:
            lang = LANG_2.get(lang, "")
        if len(lang) == 2 and lang.isalpha():
            return lang
    return ""


def probe_flag(path: pathlib.Path, cache_dir: pathlib.Path) -> str:
    """ffprobe a local file for an original flagged audio track, cached by path+mtime."""
    try:
        st = path.stat()
    except OSError:
        return ""
    key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
    store = _Store(cache_dir / "original_lang_flag.json")
    hit = store.get(key)
    if hit is not None:
        return hit
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
             "-select_streams", "a", str(path)],
            capture_output=True, timeout=30, check=True,
        ).stdout  # fmt: skip
        lang = parse_flag(json.loads(out or b"{}"))
    except Exception as e:
        log.debug("original flag probe failed for %s: %s", path, e)
        return ""
    store.put(key, lang)
    return lang


# ---------------------------------------------------------------------------
# JSON store with TTL
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()


class _Store:
    """Tiny JSON file ``{key: {"v": value, "ts": epoch}}``, entries expire after TTL."""

    def __init__(self, path: pathlib.Path, ttl: float = TTL) -> None:
        self.path = path
        self.ttl = ttl

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key: str, now: float | None = None) -> str | None:
        e = self._load().get(key)
        if not isinstance(e, dict) or not isinstance(e.get("v"), str):
            return None
        if (now if now is not None else time.time()) - float(e.get("ts", 0)) > self.ttl:
            return None
        return e["v"]

    def put(self, key: str, value: str, now: float | None = None) -> None:
        with _store_lock:
            data = self._load()
            data[key] = {"v": value, "ts": now if now is not None else time.time()}
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data))
                tmp.replace(self.path)
            except OSError as e:
                log.debug("original_lang store write failed: %s", e)


# ---------------------------------------------------------------------------
# TMDB
# ---------------------------------------------------------------------------


def tmdb_fetch(kind: str, tmdb_id: str, api_key: str) -> str:
    """Real TMDB call, returns original_language ("" when absent). Raises on errors."""
    url = f"https://api.themoviedb.org/3/{kind}/{tmdb_id}"
    headers = {"Accept": "application/json"}
    if len(api_key) > 60 or api_key.startswith("eyJ"):
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        url += f"?api_key={api_key}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=TMDB_TIMEOUT) as resp:
        data = json.loads(resp.read())
    return str(data.get("original_language") or "").lower()


def get_api_key(settings: dict[str, Any] | None = None) -> str:
    return (
        os.environ.get("TMDB_API_KEY")
        or os.environ.get("TMDB_READ_TOKEN")
        or str((settings or {}).get("tmdb_api_key") or "")
    ).strip()


_inflight: dict[str, threading.Event] = {}
_inflight_lock = threading.Lock()


def tmdb_lang(
    kind: str,
    tmdb_id: Any,
    api_key: str,
    cache_dir: pathlib.Path,
    *,
    fetch: Callable[[str, str, str], str] = tmdb_fetch,
    wait: float = DEFAULT_WAIT,
    now: float | None = None,
) -> str:
    """Cached TMDB original language. Fetches in a thread, waits at most ``wait`` s."""
    tid = str(tmdb_id or "").strip()
    if not api_key or not tid.isdigit() or tid == "0" or kind not in ("movie", "tv"):
        return ""
    key = f"{kind}:{tid}"
    store = _Store(cache_dir / "original_lang_tmdb.json")
    hit = store.get(key, now)
    if hit is not None:
        return hit
    with _inflight_lock:
        done = _inflight.get(key)
        starter = done is None
        if starter:
            done = _inflight[key] = threading.Event()
    if starter:

        def work() -> None:
            try:
                store.put(key, fetch(kind, tid, api_key), now)
            except Exception as e:
                # Network errors are not cached (retry next visit); a 404 is negative
                if getattr(e, "code", None) == 404:
                    store.put(key, "", now)
                log.debug("TMDB %s failed: %s", key, e)
            finally:
                with _inflight_lock:
                    _inflight.pop(key, None)
                done.set()  # type: ignore[union-attr]

        threading.Thread(target=work, daemon=True).start()
    assert done is not None
    done.wait(wait)
    return store.get(key, now) or ""


# ---------------------------------------------------------------------------
# Combined lookup
# ---------------------------------------------------------------------------


def lookup(
    *,
    cache_dir: pathlib.Path,
    tmdb_kind: str = "movie",
    tmdb_id: Any = None,
    api_key: str = "",
    local_path: pathlib.Path | None = None,
    country: Any = None,
    fetch: Callable[[str, str, str], str] = tmdb_fetch,
    probe: Callable[[pathlib.Path, pathlib.Path], str] = probe_flag,
    wait: float = DEFAULT_WAIT,
) -> tuple[str, str]:
    """First available of tmdb, flag, country as ``(lang, source)``."""
    try:
        lang = tmdb_lang(tmdb_kind, tmdb_id, api_key, cache_dir, fetch=fetch, wait=wait)
        if lang:
            return lang, "tmdb"
    except Exception as e:
        log.debug("tmdb lookup failed: %s", e)
    if local_path is not None:
        try:
            lang = probe(local_path, cache_dir)
            if lang:
                return lang, "flag"
        except Exception as e:
            log.debug("flag lookup failed: %s", e)
    lang = country_to_lang(country)
    return (lang, "country") if lang else ("", "")
