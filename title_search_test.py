import title_search as ts


def test_bare_title():
    assert ts.bare_title("[FR] Allegiant (2016)") == "allegiant"
    assert ts.bare_title("[DE] Divergent - 2014") == "divergent"
    assert ts.bare_title("Insurgent (2015)") == "insurgent"
    assert ts.bare_title("[FR] Les Énigmes de l'Atlantide (2003)") == "les enigmes de l atlantide"


def test_matches_by_tmdb_or_title():
    rel = ts.Related({"262500"}, {"allegiant", "divergente 3 allegiant"})
    assert ts.matches({"name": "Insurgent (2015)", "tmdb": "262500"}, rel)
    assert ts.matches({"name": "[FR] Allegiant (2016)", "tmdb": ""}, rel)
    assert not ts.matches({"name": "[FR] Insurgentes (2012)", "tmdb": "501725"}, rel)


def test_related_keeps_works_with_every_word(monkeypatch):
    def fake_get(params):
        if params["action"] == "query":
            return {"query": {"search": [{"title": "Q1"}, {"title": "Q2"}]}}
        return {
            "entities": {
                "Q1": {
                    "labels": {"fr": {"value": "Divergente 2 : L'Insurrection"},
                               "en": {"value": "The Divergent Series: Insurgent"}},
                    "claims": {"P4947": [{"mainsnak": {"datavalue": {"value": "262500"}}}]},
                },
                "Q2": {  # matched only through its description
                    "labels": {"fr": {"value": "Autre film"}},
                    "claims": {"P4947": [{"mainsnak": {"datavalue": {"value": "1"}}}]},
                },
            }
        }

    monkeypatch.setattr(ts, "_get", fake_get)
    ts._cache.clear()
    rel = ts.related("divergente", "movie")
    assert rel.tmdb_ids == {"262500"}
    assert "insurgent" in rel.titles


def test_related_offline(monkeypatch):
    def boom(params):
        raise OSError("offline")

    monkeypatch.setattr(ts, "_get", boom)
    ts._cache.clear()
    assert ts.related("divergente", "movie") == ts.Related()
