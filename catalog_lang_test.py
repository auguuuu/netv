import pytest

import catalog_lang as cl


@pytest.mark.parametrize(
    ("name", "cat", "expected"),
    [
        ("[FR] Avatar", "NL | X", "FR"),
        ("[US ] Avatar", "", "US"),
        ("[NL-4K] Avatar", "", "NL"),
        ("[ALL-4K] Avatar", "", "MULTI"),
        ("[ALL] Avatar", "", "MULTI"),
        ("[EX-YU] Avatar", "", "EXYU"),
        ("[EXYU] Avatar", "", "EXYU"),
        ("[D] Avatar", "", "DE"),
        ("[ENG] Avatar", "", "EN"),
        ("[nl] Avatar", "", "NL"),
        ("[U.K] Avatar", "", "UK"),
        ("[NL-BE] Avatar", "", "NL"),
        ("[BE] Avatar", "", "BE"),
        ("Avatar", "FR | CINEMA", "FR"),
        ("Avatar", "FR I BIOPIC & DOCS", "FR"),
        ("Avatar", "FR\xa0|\xa0SHOWTIME", "FR"),
        ("Avatar", "EX-YU | X", "EXYU"),
        ("Avatar", "ALL | MULTI", "MULTI"),
        ("Avatar", "UK | MULTI SUB", "UK"),
        ("Avatar", "FOR ADULTS", "OTHER"),
        ("Avatar", "", "OTHER"),
        ("Avatar", None, "OTHER"),
        ("[XXX] Avatar", "FOR ADULTS", "OTHER"),
        ("[XXX] Avatar", "GR | GREECE", "GR"),
        ("[ZZ] Avatar", "ZZ | X", "OTHER"),
        (None, None, "OTHER"),
    ],
)
def test_language_key(name, cat, expected):
    assert cl.language_key(name, cat) == expected


def test_matches_groups_and_all():
    assert cl.matches("", "DE")
    assert cl.matches(None, "DE")
    assert cl.matches(cl.GROUP_ENGLISH, "UK")
    assert cl.matches(cl.GROUP_ENGLISH, "US")
    assert cl.matches(cl.GROUP_ENGLISH, "EN")
    assert not cl.matches(cl.GROUP_ENGLISH, "FR")
    assert cl.matches("UK", "UK")
    assert not cl.matches("UK", "US")
    assert not cl.matches("BOGUS", "FR")
    assert not cl.is_valid("BOGUS")
    assert cl.is_valid("") and cl.is_valid("FR")


def test_languages_are_consistent():
    keys = [lang["key"] for lang in cl.LANGUAGES]
    assert len(keys) == len(set(keys))
    assert {"FR", "NL", "DE", "MULTI", "OTHER"} <= set(keys)


def test_stream_language_memoized_and_counts():
    cats = cl.category_names([{"category_id": "1", "category_name": "FR | X", "source_id": "s"}])
    items = [
        {"name": "[US] a", "category_id": "1", "source_id": "s"},
        {"name": "b", "category_id": "1", "source_id": "s"},
        {"name": "[UK] c", "category_id": "9", "source_id": "s"},
    ]
    assert [cl.stream_language(i, cats) for i in items] == ["US", "FR", "UK"]
    assert items[1][cl.MEMO_FIELD] == "FR"
    counts = cl.option_counts(items, cats)
    assert counts[""] == 3 and counts[cl.GROUP_ENGLISH] == 2 and counts["FR"] == 1
    assert cl.filter_by_language(items, "FR", cats) == [items[1]]
    assert cl.filter_by_language(items, "", cats) is items
