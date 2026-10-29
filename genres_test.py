import genres


def test_buckets_for():
    assert genres.buckets_for("film ou téléfilm d'horreur") == ["horreur"]
    assert genres.buckets_for("slasher") == ["horreur"]
    assert set(genres.buckets_for("comédie d'action")) == {"action", "comedie"}
    assert genres.buckets_for("cinéma de science-fiction") == ["sf"]
    assert genres.buckets_for("film LGBT") == []


def test_options_and_filter(monkeypatch):
    monkeypatch.setattr(genres, "_data", {"timestamp": 1, "movies": {"1": ["action"], "2": ["action", "sf"]}})
    items = [{"tmdb": "1"}, {"tmdb": 2}, {"tmdb": ""}, {"tmdb": "3"}]
    assert genres.options("movies", items) == [
        {"key": "action", "label": "Action", "count": 2},
        {"key": "sf", "label": "Science-fiction", "count": 1},
    ]
    assert genres.filter_items("movies", items, "sf") == [{"tmdb": 2}]
    assert genres.filter_items("movies", items, "") == items


def test_tmdb_ids():
    assert genres.tmdb_ids([{"tmdb": "12"}, {"tmdb": 12}, {"tmdb": "0"}, {"tmdb": ""}, {}]) == ["12"]
