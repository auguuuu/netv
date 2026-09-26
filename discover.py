"""Netflix style discovery rows for the Movies landing page.

Pure module (no I/O, no FastAPI). Rows are computed once per catalog object,
day and language, then served from a small in-memory cache.

Rows: recent additions, top rated (daily seeded), one row per streaming
platform, French rows and cinema. Adult, sports/PPV, concerts and broken items
are excluded everywhere, and the same film across languages is deduplicated
(tmdb id, else normalized title), preferring the selected language, else FR.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import random
import re
import threading

import catalog_lang


ROW_SIZE = 24
MIN_ROW_ITEMS = 6  # rows shorter than this are dropped from the API response
TOP_POOL = 160  # candidates the daily seed samples from
TOP_MIN_RATING = 7.0
TOP_MAX_RATING = 9.6  # a perfect 10 is nearly always a handful of votes
TOP_MIN_YEAR = 2000
FR_TOP_MIN_RATING = 6.5

_EXCLUDED_CAT = re.compile(
    r"ADULT|XXX|SPORT|PPV|\bF1\b|CONCERT|MUSIC|MUSIQUE|SPECTACLE|UFC|FIGHT|MMA|"
    r"\bLIVE\b|MEDIATHEK|CAM VERSIE|\bCAM\b|\(TEST\)|SCAN",
    re.I,
)
_EXCLUDED_NAME = re.compile(
    r"\[XXX\]|\bXXX\b|\bUFC\b|\bWWE\b|formule 1|formula 1|\bGP\d+\b|grand prix|"
    r"\bmotogp\b|\bppv\b|\blive (?:at|in|from)\b|\bin concert\b|\bconcert\b|\bhttps?:",
    re.I,
)
_PREFIX = re.compile(r"^\s*\[\[?[^\]\[]{1,12}\]\]?\s*")
_YEAR = re.compile(r"\((\d{4})\)\s*$|\b((?:19|20)\d{2})\s*$")
_NON_ALNUM = re.compile(r"[\W_]+", re.UNICODE)

PLATFORMS: list[tuple[str, str, re.Pattern[str]]] = [
    ("netflix", "Nouveautés Netflix", re.compile(r"NETFLIX", re.I)),
    ("prime", "Nouveautés Amazon Prime", re.compile(r"AMAZON", re.I)),
    ("disney", "Nouveautés Disney+", re.compile(r"DISNEY", re.I)),
    ("apple", "Nouveautés Apple TV+", re.compile(r"APPLE", re.I)),
    ("hbo", "Nouveautés HBO Max", re.compile(r"HBO", re.I)),
]
_CINEMA = re.compile(r"CINEMA|CINÉMA|BIOSCOOP", re.I)


@dataclass(slots=True)
class Rec:
    """Precomputed, filter-ready view of one catalog stream."""

    item: dict
    key: str
    lang: str
    added: int
    rating: float | None
    year: int | None
    cat: str


def parse_rating(value: Any) -> float | None:
    """'7,9' -> 7.9; empty, zero or garbage -> None."""
    if value is None:
        return None
    try:
        f = float(str(value).strip().replace(",", "."))
    except ValueError:
        return None
    return f if 0 < f <= 10 else None


def parse_year(name: str) -> int | None:
    m = _YEAR.search(name or "")
    if not m:
        return None
    y = int(m.group(1) or m.group(2))
    return y if 1900 <= y <= 2100 else None


def clean_title(name: str) -> str:
    return _PREFIX.sub("", name or "").strip()


def _dedupe_key(item: dict, title: str, year: int | None) -> str:
    tmdb = str(item.get("tmdb") or "").strip()
    if tmdb and tmdb != "0":
        return "t:" + tmdb
    base = _NON_ALNUM.sub("", re.sub(r"\(\d{4}\)\s*$", "", title).lower())
    return f"n:{base}:{year or ''}"


def _is_broken(title: str) -> bool:
    if len(title) < 2:
        return True
    alnum = len(_NON_ALNUM.sub("", title))
    return alnum < 2 or alnum < len(title) * 0.4


def build_records(streams: Iterable[dict], categories: Iterable[Mapping[str, Any]]) -> list[Rec]:
    """Filter junk and precompute fields. Sorted by `added` descending."""
    cat_names = catalog_lang.category_names(categories)
    cat_bad: dict[str, bool] = {}
    recs: list[Rec] = []
    for s in streams:
        if s.get("is_adult") not in (0, "0", None, False):
            continue
        name = s.get("name") or ""
        if not s.get("stream_icon") or not s.get("stream_id"):
            continue
        cat = cat_names.get((str(s.get("source_id", "")), str(s.get("category_id"))), "")
        bad_cat = cat_bad.get(cat)
        if bad_cat is None:
            bad_cat = cat_bad[cat] = bool(_EXCLUDED_CAT.search(cat))
        if bad_cat or _EXCLUDED_NAME.search(name):
            continue
        title = clean_title(name)
        if _is_broken(title):
            continue
        year = parse_year(title)
        try:
            added = int(s.get("added") or 0)
        except (TypeError, ValueError):
            added = 0
        recs.append(
            Rec(
                item=s,
                key=_dedupe_key(s, title, year),
                lang=catalog_lang.stream_language(s, cat_names),
                added=added,
                rating=parse_rating(s.get("rating")),
                year=year,
                cat=cat,
            )
        )
    recs.sort(key=lambda r: r.added, reverse=True)
    return recs


def _dedupe(recs: Iterable[Rec], lang: str) -> list[Rec]:
    """One record per film, at the position of its first occurrence.

    The variant kept is the one in the selected language, else FR, else the
    first seen.
    """
    first_pos: dict[str, int] = {}
    best: dict[str, tuple[int, Rec]] = {}
    for i, r in enumerate(recs):
        if lang and catalog_lang.matches(lang, r.lang):
            pref = 0
        else:
            pref = 1 if r.lang == "FR" else 2
        cur = best.get(r.key)
        if cur is None:
            first_pos[r.key] = i
            best[r.key] = (pref, r)
        elif pref < cur[0]:
            best[r.key] = (pref, r)
    ordered = sorted(best, key=first_pos.__getitem__)
    return [best[k][1] for k in ordered]


def _to_item(r: Rec, poster_fn: Callable[[str], str]) -> dict:
    s = r.item
    return {
        "stream_id": s.get("stream_id"),
        "name": s.get("name") or "",
        "poster": poster_fn(s.get("stream_icon") or ""),
        "rating": f"{r.rating:.1f}" if r.rating else "",
        "year": r.year,
        "added": r.added,
        "ext": s.get("container_extension") or "mkv",
        "tmdb": str(s.get("tmdb") or ""),
    }


def _top(recs: list[Rec], lang: str, seed: str, min_rating: float, min_year: int) -> list[Rec]:
    """Well rated, recent-ish titles, sampled from a pool with a daily seed."""
    pool = [
        r
        for r in recs
        if r.rating is not None
        and min_rating <= r.rating <= TOP_MAX_RATING
        and r.year is not None
        and r.year >= min_year
        and str(r.item.get("tmdb") or "") not in ("", "0")
    ]
    pool = _dedupe(pool, lang)
    pool.sort(key=lambda r: (r.rating or 0) + max(0, (r.year or 0) - 2010) * 0.05, reverse=True)
    pool = pool[:TOP_POOL]
    picked = random.Random(seed).sample(pool, min(ROW_SIZE, len(pool)))
    picked.sort(key=lambda r: r.rating or 0, reverse=True)
    return picked


def build_rows(
    recs: list[Rec],
    lang: str,
    day: str,
    poster_fn: Callable[[str], str] = lambda u: u,
    size: int = ROW_SIZE,
) -> list[dict]:
    """All rows (possibly short or empty), in display order."""
    if lang:
        recs = [r for r in recs if catalog_lang.matches(lang, r.lang)]
    rows: list[tuple[str, str, list[Rec]]] = []

    def newest(pred: Callable[[Rec], bool]) -> list[Rec]:
        return _dedupe((r for r in recs if pred(r)), lang)[:size]

    rows.append(("recent", "Ajoutés récemment", newest(lambda r: True)))
    rows.append(
        (
            "top",
            "Les mieux notés",
            _top(recs, lang, f"{day}|{lang}|top", TOP_MIN_RATING, TOP_MIN_YEAR),
        )
    )
    for pid, title, rx in PLATFORMS:
        rows.append((pid, title, newest(lambda r, rx=rx: bool(rx.search(r.cat)))))
    if not lang:  # with a language selected, the generic rows are already that language
        fr = [r for r in recs if r.lang == "FR"]
        rows.append(("fr_new", "Nouveautés en français", _dedupe(fr, "FR")[:size]))
        rows.append(
            (
                "fr_top",
                "Les mieux notés en français",
                _top(fr, "FR", f"{day}|fr|top", FR_TOP_MIN_RATING, 1990),
            )
        )
    rows.append(("cinema", "Cinéma", newest(lambda r: bool(_CINEMA.search(r.cat)))))
    return [
        {"id": rid, "title": title, "items": [_to_item(r, poster_fn) for r in items[:size]]}
        for rid, title, items in rows
    ]


# --- cache -----------------------------------------------------------------

_lock = threading.Lock()
_state: dict[str, Any] = {"streams": None, "recs": None, "rows": {}}


def get_rows(
    streams: list[dict],
    categories: list[dict],
    lang: str = "",
    day: str | None = None,
    poster_fn: Callable[[str], str] = lambda u: u,
    unavailable_groups: frozenset[str] = frozenset(),
    vod_filter: frozenset[str] = frozenset(),
) -> list[dict]:
    """Rows for the API, cached per (catalog object, day, lang, visibility).

    `unavailable_groups` holds "movies:<source_id>" entries and `vod_filter`
    the allowed category ids (empty = all), like the /vod route. Rows shorter
    than MIN_ROW_ITEMS are dropped.
    """
    day = day or date.today().isoformat()
    key = (day, lang, unavailable_groups, vod_filter)
    with _lock:
        if _state["streams"] is not streams:
            _state.update(streams=streams, recs=None, rows={})
        hit = _state["rows"].get(key)
        if hit is not None:
            return hit
        if _state["recs"] is None:
            _state["recs"] = build_records(streams, categories)
        recs = _state["recs"]
        if unavailable_groups or vod_filter:
            recs = [
                r
                for r in recs
                if f"movies:{r.item.get('source_id', '')}" not in unavailable_groups
                and (not vod_filter or str(r.item.get("category_id")) in vod_filter)
            ]
        rows = [
            r for r in build_rows(recs, lang, day, poster_fn) if len(r["items"]) >= MIN_ROW_ITEMS
        ]
        if len(_state["rows"]) > 32:
            _state["rows"].clear()
        _state["rows"][key] = rows
        return rows


def reset_cache() -> None:
    with _lock:
        _state.update(streams=None, recs=None, rows={})
