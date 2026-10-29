"""Find films and series by their title in another language (French title -> catalog entry).

Providers name most entries with their English title ("Insurgent (2015)"), so searching the
French title ("Divergente 2") finds nothing. Wikidata (free, no key) maps titles in every
language to TMDB ids, and most catalog entries carry their TMDB id: a search also returns the
entries whose TMDB id belongs to a work whose French or English title matches the query, and
those whose bare title equals one of these titles.

Network failures only cost the extra matches: ``related`` then returns nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import json
import logging
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request


log = logging.getLogger(__name__)

API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "neTV/1.0 (self-hosted IPTV player)"
TIMEOUT_SEC = 4.0
CACHE_SEC = 24 * 3600
MAX_HITS = 20
TMDB_PROP = {"movie": "P4947", "series": "P4983"}

_PREFIX = re.compile(r"^\s*(\[[^\]]*\]\s*|[A-Z]{2,3}\s*[|:-]\s*)+")
_YEAR = re.compile(r"[\s(\[-]*\b(19|20)\d{2}\b[)\]]?.*$")


@dataclass
class Related:
    tmdb_ids: set[str] = field(default_factory=set)
    titles: set[str] = field(default_factory=set)


_cache: dict[tuple[str, str], tuple[float, Related]] = {}
_lock = threading.Lock()


def fold(text: str) -> str:
    """Lowercase without accents or punctuation, for comparisons."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def bare_title(name: str) -> str:
    """'[FR] Allegiant (2016)' -> 'allegiant'."""
    return fold(_YEAR.sub("", _PREFIX.sub("", name)))


def _get(params: dict[str, Any]) -> dict[str, Any]:
    url = f"{API}?{urllib.parse.urlencode({**params, 'format': 'json'})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:  # noqa: S310 (fixed host)
        return json.loads(resp.read())


def _fetch(query: str, kind: str) -> Related:
    prop = TMDB_PROP[kind]
    found = _get(
        {
            "action": "query",
            "list": "search",
            "srsearch": f"{query} haswbstatement:{prop}",
            "srlimit": MAX_HITS,
        }
    )
    ids = [h["title"] for h in (found.get("query") or {}).get("search") or []]
    out = Related()
    if not ids:
        return out
    entities = _get(
        {"action": "wbgetentities", "ids": "|".join(ids), "props": "labels|claims", "languages": "fr|en"}
    ).get("entities") or {}
    words = fold(query).split()
    for ent in entities.values():
        labels = [str(v.get("value", "")) for v in (ent.get("labels") or {}).values()]
        # the search also hits descriptions and aliases: keep works whose title has every word
        if not any(all(w in fold(lb).split() for w in words) for lb in labels):
            continue
        for claim in (ent.get("claims") or {}).get(prop) or []:
            value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if value:
                out.tmdb_ids.add(str(value))
        for lb in labels:
            out.titles.add(fold(lb))
            if ":" in lb:  # "The Divergent Series: Allegiant" is often just "Allegiant"
                out.titles.add(fold(lb.rsplit(":", 1)[1]))
    out.titles.discard("")
    return out


def related(query: str, kind: str) -> Related:
    """Works whose French or English title contains every word of ``query`` (cached a day)."""
    key = (fold(query), kind)
    if len(key[0]) < 3 or kind not in TMDB_PROP:
        return Related()
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SEC:
        return hit[1]
    try:
        result = _fetch(query, kind)
    except Exception as exc:
        log.info("Title search for %r unavailable: %s", query, exc)
        return Related()
    with _lock:
        _cache[key] = (now, result)
    return result


def matches(item: dict[str, Any], rel: Related) -> bool:
    tmdb = str(item.get("tmdb") or "").strip()
    if tmdb and tmdb in rel.tmdb_ids:
        return True
    return bool(rel.titles) and bare_title(str(item.get("name") or "")) in rel.titles
