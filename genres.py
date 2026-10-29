"""Film and series genres ("Genre" filter), from Wikidata.

The provider lists no genre per film, only its own categories (Netflix, Amazon...). Most
entries carry a TMDB id, and Wikidata links TMDB ids (P4947 films, P4983 series) to genres
(P136). Once a week, in the background, the TMDB ids of the catalog are sent to the Wikidata
query service in batches; the hundreds of Wikidata genres ("film ou téléfilm d'horreur",
"slasher"...) are folded into a short list from their French label.

Stored in ``.cache/genres.json``. Without it (first run, offline) the filter is just empty.
"""

from __future__ import annotations

from typing import Any

import json
import logging
import pathlib
import threading
import time
import urllib.parse
import urllib.request


log = logging.getLogger(__name__)

SPARQL = "https://query.wikidata.org/sparql"
USER_AGENT = "neTV/1.0 (self-hosted IPTV player)"
BATCH = 2000
TIMEOUT_SEC = 90.0
REFRESH_SEC = 7 * 24 * 3600
PROP = {"movies": "P4947", "series": "P4983"}

# (key, label, keywords found in the French or English Wikidata genre label)
GENRES: list[tuple[str, str, tuple[str, ...]]] = [
    ("action", "Action", ("action", "arts martiaux", "martial arts")),
    ("aventure", "Aventure", ("aventure", "adventure")),
    ("animation", "Animation", ("animation", "animé", "animated", "anime")),
    ("comedie", "Comédie", ("comédie", "comedy", "humoristique", "parodie", "parody", "sitcom")),
    ("drame", "Drame", ("drame", "dramatique", "drama", "mélodrame")),
    ("thriller", "Thriller", ("thriller", "suspense", "néo-noir", "neo-noir", "espionnage", "spy")),
    ("policier", "Policier", ("criminel", "policier", "crime", "casse", "heist", "gangster", "mafia", "procès")),
    ("horreur", "Horreur", ("horreur", "horror", "slasher", "fantôme", "monstre", "zombie", "épouvante")),
    ("sf", "Science-fiction", ("science-fiction", "science fiction", "post-apocalyptique", "dystop", "cyberpunk")),
    ("fantasy", "Fantasy / Fantastique", ("fantasy", "fantastique", "conte de fées", "fairy tale", "surnaturel", "supernatural")),
    ("romance", "Romance", ("amour", "romanti", "romance")),
    ("famille", "Famille / Enfants", ("familial", "famille", "enfants", "children", "family", "noël", "christmas")),
    ("documentaire", "Documentaire", ("documentaire", "documentary")),
    ("biographie", "Biographie / Histoire", ("biograph", "historique", "historical", "history")),
    ("guerre", "Guerre", ("guerre", "war film", "war ")),
    ("western", "Western", ("western",)),
    ("musical", "Musical", ("musical", "musique", "music")),
    ("superheros", "Super-héros", ("super-héros", "superhero")),
    ("sport", "Sport", ("sport",)),
]
LABELS = {key: label for key, label, _ in GENRES}

_lock = threading.Lock()
_data: dict[str, Any] | None = None
_path: pathlib.Path | None = None
_running = False


def buckets_for(label: str) -> list[str]:
    low = label.lower()
    return [key for key, _, words in GENRES if any(w in low for w in words)]


def _sparql(query: str) -> list[dict[str, Any]]:
    req = urllib.request.Request(
        SPARQL,
        data=urllib.parse.urlencode({"query": query}).encode(),
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:  # noqa: S310 (fixed host)
        return json.loads(resp.read())["results"]["bindings"]


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def fetch_genres(kind: str, tmdb_ids: list[str]) -> dict[str, list[str]]:
    """{tmdb id: [genre keys]} for the ids Wikidata knows a genre of."""
    prop = PROP[kind]
    raw: dict[str, set[str]] = {}
    for i in range(0, len(tmdb_ids), BATCH):
        values = " ".join(json.dumps(t) for t in tmdb_ids[i : i + BATCH])
        rows = _sparql(f"SELECT ?t ?g WHERE {{ VALUES ?t {{ {values} }} ?f wdt:{prop} ?t ; wdt:P136 ?g . }}")
        for row in rows:
            raw.setdefault(row["t"]["value"], set()).add(_qid(row["g"]["value"]))
    qids = sorted({q for gs in raw.values() for q in gs})
    labels: dict[str, list[str]] = {}
    for i in range(0, len(qids), BATCH):
        values = " ".join("wd:" + q for q in qids[i : i + BATCH])
        rows = _sparql(
            f"SELECT ?g ?l WHERE {{ VALUES ?g {{ {values} }} ?g rdfs:label ?l "
            f"FILTER(lang(?l) = 'fr' || lang(?l) = 'en') }}"
        )
        for row in rows:
            labels.setdefault(_qid(row["g"]["value"]), []).append(row["l"]["value"])
    bucket_of = {q: sorted({b for lb in labels.get(q, []) for b in buckets_for(lb)}) for q in qids}
    out: dict[str, list[str]] = {}
    for tmdb, gs in raw.items():
        keys = sorted({b for q in gs for b in bucket_of.get(q, [])})
        if keys:
            out[tmdb] = keys
    return out


def tmdb_ids(items: list[dict[str, Any]]) -> list[str]:
    ids = {str(it.get("tmdb") or "").strip() for it in items}
    ids.discard("")
    ids.discard("0")
    return sorted(i for i in ids if i.isdigit())


def init(cache_dir: pathlib.Path) -> None:
    global _path, _data
    _path = cache_dir / "genres.json"
    try:
        _data = json.loads(_path.read_text())
    except (OSError, ValueError):
        _data = None


def _load() -> dict[str, Any]:
    return _data or {"timestamp": 0, "movies": {}, "series": {}}


def stale() -> bool:
    return time.time() - float(_load().get("timestamp") or 0) > REFRESH_SEC


def refresh(movies: list[dict[str, Any]], series: list[dict[str, Any]]) -> None:
    """Fetch the genres of the whole catalog (blocking, ~1 min)."""
    global _data
    result: dict[str, Any] = {"timestamp": time.time()}
    for kind, items in (("movies", movies), ("series", series)):
        ids = tmdb_ids(items)
        result[kind] = fetch_genres(kind, ids)
        log.info("Genres: %d of %d %s tmdb ids", len(result[kind]), len(ids), kind)
    with _lock:
        _data = result
    if _path is not None:
        tmp = _path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result))
        tmp.replace(_path)


def refresh_in_background(movies: list[dict[str, Any]], series: list[dict[str, Any]]) -> None:
    """Start a refresh unless one is running or the data is recent."""
    global _running
    with _lock:
        if _running or not stale() or not (movies or series):
            return
        _running = True

    def run() -> None:
        global _running
        try:
            refresh(movies, series)
        except Exception as exc:
            log.warning("Genre refresh failed: %s", exc)
        finally:
            with _lock:
                _running = False

    threading.Thread(target=run, daemon=True, name="genres").start()


def genres_of(kind: str, item: dict[str, Any]) -> list[str]:
    tmdb = str(item.get("tmdb") or "").strip()
    return list(_load().get(kind, {}).get(tmdb, [])) if tmdb else []


def options(kind: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{key, label, count}] for the genres present among ``items``, in GENRES order."""
    table = _load().get(kind, {})
    counts: dict[str, int] = {}
    for it in items:
        for g in table.get(str(it.get("tmdb") or "").strip(), []):
            counts[g] = counts.get(g, 0) + 1
    return [{"key": k, "label": lb, "count": counts[k]} for k, lb, _ in GENRES if counts.get(k)]


def filter_items(kind: str, items: list[dict[str, Any]], genre: str) -> list[dict[str, Any]]:
    if not genre:
        return items
    table = _load().get(kind, {})
    return [it for it in items if genre in table.get(str(it.get("tmdb") or "").strip(), [])]
