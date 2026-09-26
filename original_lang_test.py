"""Tests for original_lang (fakes only, no network)."""

from __future__ import annotations

import json
import pathlib
import threading
import time

import pytest

import original_lang as ol


def test_country_map():
    assert ol.country_to_lang("France") == "fr"
    assert ol.country_to_lang("Japan") == "ja"
    assert ol.country_to_lang("South Korea") == "ko"
    assert ol.country_to_lang("English") == "en"
    assert ol.country_to_lang("Hong Kong") == "zh"
    assert ol.country_to_lang("") == ""
    assert ol.country_to_lang(None) == ""
    assert ol.country_to_lang("Narnia") == ""


def test_country_first_and_language_preference():
    assert ol.country_to_lang("Germany, France") == "de"
    assert ol.country_to_lang("United States, French") == "fr"
    assert ol.country_to_lang("United States, France") == "en"
    assert ol.country_to_lang("United States / English") == "en"


def test_parse_flag():
    data = {
        "streams": [
            {"codec_type": "video"},
            {"codec_type": "audio", "tags": {"language": "fre"}, "disposition": {}},
            {
                "codec_type": "audio",
                "tags": {"language": "jpn"},
                "disposition": {"original": 1},
            },
        ]
    }
    assert ol.parse_flag(data) == "ja"
    titled = {"streams": [{"codec_type": "audio", "tags": {"language": "eng", "title": "VO"}}]}
    assert ol.parse_flag(titled) == "en"
    assert ol.parse_flag({"streams": [{"codec_type": "audio", "tags": {"language": "eng"}}]}) == ""
    assert ol.parse_flag({}) == ""


def test_tmdb_no_key_no_call(tmp_path):
    calls = []
    assert ol.tmdb_lang("movie", "1", "", tmp_path, fetch=lambda *a: calls.append(a) or "en") == ""
    assert calls == []


def test_tmdb_cache_hit_miss_and_endpoint(tmp_path):
    calls = []

    def fetch(kind, tid, key):
        calls.append((kind, tid, key))
        return "ja"

    assert ol.tmdb_lang("tv", "42", "k", tmp_path, fetch=fetch) == "ja"
    assert ol.tmdb_lang("tv", "42", "k", tmp_path, fetch=fetch) == "ja"
    assert calls == [("tv", "42", "k")]
    assert ol.tmdb_lang("movie", "42", "k", tmp_path, fetch=fetch) == "ja"
    assert len(calls) == 2


def test_tmdb_negative_cache_and_ttl(tmp_path):
    calls = []

    def fetch(*a):
        calls.append(a)
        return ""

    ol.tmdb_lang("movie", "7", "k", tmp_path, fetch=fetch)
    ol.tmdb_lang("movie", "7", "k", tmp_path, fetch=fetch)
    assert len(calls) == 1
    # Expire the entry
    p = tmp_path / "original_lang_tmdb.json"
    data = json.loads(p.read_text())
    data["movie:7"]["ts"] = time.time() - ol.TTL - 10
    p.write_text(json.dumps(data))
    ol.tmdb_lang("movie", "7", "k", tmp_path, fetch=fetch)
    assert len(calls) == 2


def test_tmdb_error_swallowed_and_not_cached(tmp_path):
    def boom(*a):
        raise TimeoutError("slow")

    assert ol.tmdb_lang("movie", "9", "k", tmp_path, fetch=boom) == ""
    assert ol.tmdb_lang("movie", "9", "k", tmp_path, fetch=lambda *a: "fr") == "fr"


def test_tmdb_slow_fetch_does_not_block_then_cached(tmp_path):
    gate = threading.Event()

    def slow(*a):
        gate.wait(5)
        return "ko"

    t0 = time.time()
    assert ol.tmdb_lang("movie", "5", "k", tmp_path, fetch=slow, wait=0.05) == ""
    assert time.time() - t0 < 1
    gate.set()
    for _ in range(50):
        if ol.tmdb_lang("movie", "5", "k", tmp_path, fetch=slow, wait=0.05) == "ko":
            break
        time.sleep(0.02)
    else:
        pytest.fail("background result never cached")


def test_tmdb_invalid_ids(tmp_path):
    f = lambda *a: "en"  # noqa: E731
    assert ol.tmdb_lang("movie", "0", "k", tmp_path, fetch=f) == ""
    assert ol.tmdb_lang("movie", "abc", "k", tmp_path, fetch=f) == ""
    assert ol.tmdb_lang("movie", None, "k", tmp_path, fetch=f) == ""


def test_lookup_ordering(tmp_path):
    f = lambda *a: "ja"  # noqa: E731
    probe = lambda *a: "fr"  # noqa: E731
    p = pathlib.Path("x.mkv")
    kw = dict(cache_dir=tmp_path, tmdb_id="1", api_key="k", fetch=f, probe=probe, local_path=p)
    assert ol.lookup(country="Germany", **kw) == ("ja", "tmdb")
    kw["api_key"] = ""
    assert ol.lookup(country="Germany", **kw) == ("fr", "flag")
    kw["probe"] = lambda *a: ""
    assert ol.lookup(country="Germany", **kw) == ("de", "country")
    assert ol.lookup(country="", **kw) == ("", "")


def test_lookup_probe_error_swallowed(tmp_path):
    def boom(*a):
        raise OSError("x")

    assert ol.lookup(
        cache_dir=tmp_path, local_path=pathlib.Path("a"), probe=boom, country="Italy"
    ) == (
        "it",
        "country",
    )


def test_get_api_key(monkeypatch):
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.delenv("TMDB_READ_TOKEN", raising=False)
    assert ol.get_api_key({"tmdb_api_key": " abc "}) == "abc"
    assert ol.get_api_key({}) == ""
    monkeypatch.setenv("TMDB_READ_TOKEN", "tok")
    assert ol.get_api_key({"tmdb_api_key": "abc"}) == "tok"
