"""Tests for discover.py (Movies discovery rows)."""

from __future__ import annotations

import discover


CATS = [
    {"category_id": "1", "category_name": "ALL | NETFLIX", "source_id": "s"},
    {"category_id": "2", "category_name": "FR | NETFLIX", "source_id": "s"},
    {"category_id": "3", "category_name": "FOR ADULTS", "source_id": "s"},
    {"category_id": "4", "category_name": "ALL | SPORTS F1 / PPV", "source_id": "s"},
    {"category_id": "5", "category_name": "FR | FILMS", "source_id": "s"},
    {"category_id": "6", "category_name": "NL | BIOSCOOP", "source_id": "s"},
    {"category_id": "7", "category_name": "NL | CONCERT AND MUSIC", "source_id": "s"},
    {"category_id": "8", "category_name": "US | AMAZON PRIME", "source_id": "s"},
    {"category_id": "9", "category_name": "FR I DISNEY", "source_id": "s"},
    {"category_id": "10", "category_name": "US | HBO", "source_id": "s"},
    {"category_id": "11", "category_name": "US | APPLE TV+", "source_id": "s"},
]


def mk(sid, name, cat, added=1000, rating="7,5", tmdb=None, icon="http://x/p.jpg", **kw):
    return {
        "stream_id": sid,
        "name": name,
        "category_id": cat,
        "added": str(added),
        "rating": rating,
        "tmdb": str(tmdb if tmdb is not None else sid),
        "stream_icon": icon,
        "container_extension": "mp4",
        "source_id": "s",
        "is_adult": 0,
        **kw,
    }


def recs(streams):
    return discover.build_records(streams, CATS)


def ids(rows, rid):
    return [i["stream_id"] for r in rows if r["id"] == rid for i in r["items"]]


def rows_for(streams, lang="", day="2026-01-01"):
    return discover.build_rows(recs(streams), lang, day)


def test_parse_rating_comma_and_garbage():
    assert discover.parse_rating("7,9") == 7.9
    assert discover.parse_rating("8.25") == 8.25
    assert discover.parse_rating("") is None
    assert discover.parse_rating("0") is None
    assert discover.parse_rating("abc") is None
    assert discover.parse_rating(None) is None


def test_excludes_adult_sport_concert_and_broken():
    s = [
        mk(1, "[EN] Good Movie (2020)", "1"),
        mk(2, "[XXX] Something (2020)", "1"),
        mk(3, "Adult film (2020)", "3"),
        mk(4, "Formule 1 - GP15 (2026)", "1"),
        mk(5, "Race night (2026)", "4"),
        mk(6, "Big Band (2020)", "7"),
        mk(7, "Nice (2020)", "1", icon=""),
        mk(8, "[EN] !!!", "1"),
        mk(9, "Flagged (2020)", "1", is_adult=1),
        mk(10, "Live in Paris Concert", "1"),
    ]
    assert [r.item["stream_id"] for r in recs(s)] == [1]


def test_recent_ordering_and_dedupe_by_tmdb_prefers_fr():
    s = [
        mk(1, "[EN] Avatar (2009)", "1", added=300, tmdb=19995),
        mk(2, "[FR] Avatar (2009)", "2", added=100, tmdb=19995),
        mk(3, "[NL] Avatar (2009)", "6", added=200, tmdb=19995),
        mk(4, "[EN] Alien (1979)", "1", added=250, tmdb=348),
        mk(5, "[EN] Zulu (1964)", "1", added=50, tmdb=777),
    ]
    rows = rows_for(s)
    # Avatar kept once, FR variant, at the position of its newest copy; then Alien, Zulu.
    assert ids(rows, "recent") == [2, 4, 5]


def test_dedupe_prefers_selected_language():
    s = [
        mk(1, "[EN] Heat (1995)", "1", added=300, tmdb=1),
        mk(2, "[FR] Heat (1995)", "2", added=100, tmdb=1),
        mk(3, "[NL] Heat (1995)", "6", added=200, tmdb=1),
    ]
    assert ids(rows_for(s), "recent") == [2]
    assert ids(rows_for(s, lang="NL"), "recent") == [3]
    assert ids(rows_for(s, lang="EN"), "recent") == [1]


def test_dedupe_without_tmdb_uses_title():
    s = [
        mk(1, "[EN] Heat (1995)", "1", tmdb=0, added=2),
        mk(2, "[FR] Heat (1995)", "2", tmdb=0, added=1),
    ]
    assert ids(rows_for(s), "recent") == [2]


def test_platform_and_fr_rows():
    s = [
        mk(1, "[EN] A (2024)", "1", added=5),
        mk(2, "[FR] B (2024)", "2", added=6),
        mk(3, "[EN] C (2024)", "8", added=7),
        mk(4, "[FR] D (2024)", "9", added=8),
        mk(5, "[EN] E (2024)", "10", added=9),
        mk(6, "[EN] F (2024)", "11", added=10),
        mk(7, "[NL] G (2024)", "6", added=11),
        mk(8, "[FR] H (2023)", "5", added=4, rating="7,1"),
    ]
    rows = rows_for(s)
    assert set(ids(rows, "netflix")) == {1, 2}
    assert ids(rows, "prime") == [3]
    assert ids(rows, "disney") == [4]
    assert ids(rows, "hbo") == [5]
    assert ids(rows, "apple") == [6]
    assert ids(rows, "cinema") == [7]
    assert ids(rows, "fr_new") == [4, 2, 8]
    assert set(ids(rows, "fr_top")) == {2, 4, 8}
    assert "fr_new" not in {r["id"] for r in rows_for(s, lang="FR")}


def test_top_rated_threshold_recency_and_comma():
    s = [
        mk(1, "[EN] Great (2022)", "1", rating="8,4"),
        mk(2, "[EN] Ok (2022)", "1", rating="6,9"),
        mk(3, "[EN] Old (1975)", "1", rating="9,0"),
        mk(4, "[EN] Perfect (2022)", "1", rating="10"),
        mk(5, "[EN] NoTmdb (2022)", "1", rating="8,0", tmdb=""),
        mk(6, "[EN] Nice (2021)", "1", rating="7,0"),
    ]
    assert set(ids(rows_for(s), "top")) == {1, 6}


def test_top_daily_seed_stable_within_day_and_varies_across_days():
    s = [mk(i, f"[EN] Movie {i} (2022)", "1", rating="8,0", tmdb=1000 + i) for i in range(100, 220)]
    r = recs(s)
    a = ids(discover.build_rows(r, "", "2026-01-01"), "top")
    b = ids(discover.build_rows(r, "", "2026-01-01"), "top")
    c = ids(discover.build_rows(r, "", "2026-01-02"), "top")
    assert a == b
    assert len(a) == discover.ROW_SIZE
    assert set(a) != set(c)


def test_get_rows_caches_and_drops_short_rows():
    discover.reset_cache()
    s = [mk(i, f"[EN] M{i} (2020)", "1", added=i, tmdb=i) for i in range(1, 12)]
    r1 = discover.get_rows(s, CATS, day="2026-01-01")
    r2 = discover.get_rows(s, CATS, day="2026-01-01")
    assert r1 is r2
    assert {r["id"] for r in r1} <= {"recent", "top", "netflix"}
    assert all(len(r["items"]) >= discover.MIN_ROW_ITEMS for r in r1)
    assert discover.get_rows(s, CATS, day="2026-01-02") is not r1
    discover.reset_cache()


def test_visibility_filters():
    discover.reset_cache()
    s = [mk(i, f"[EN] M{i} (2020)", "1", added=i, tmdb=i) for i in range(1, 12)]
    assert discover.get_rows(s, CATS, day="d", unavailable_groups=frozenset({"movies:s"})) == []
    rows = discover.get_rows(s, CATS, day="d", vod_filter=frozenset({"2"}))
    assert rows == []
    discover.reset_cache()


def test_item_shape_and_poster_fn():
    s = [mk(1, "[EN] Alien (1979)", "1", rating="8,1")]
    row = discover.build_rows(recs(s), "", "d", poster_fn=lambda u: "/api/logo?url=" + u)[0]
    it = row["items"][0]
    assert it == {
        "stream_id": 1,
        "name": "[EN] Alien (1979)",
        "poster": "/api/logo?url=http://x/p.jpg",
        "rating": "8.1",
        "year": 1979,
        "added": 1000,
        "ext": "mp4",
        "tmdb": "1",
    }
