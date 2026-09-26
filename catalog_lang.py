"""Language detection for VOD and series catalog items.

The language is the provider's label, not a real audio track list. It is derived
from the title prefix ("[FR] Avatar"), else from the category name prefix
("FR | NETFLIX"), else "OTHER". Pure module, no I/O.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

import re


OTHER = "OTHER"
GROUP_ENGLISH = "ANGLAIS"
MEMO_FIELD = "_lang"

# Ordered list of selectable options. `members` are the normalized item keys.
LANGUAGES: list[dict[str, Any]] = [
    {"key": "FR", "label": "Français (FR)", "members": ["FR"]},
    {"key": "NL", "label": "Néerlandais (NL)", "members": ["NL"]},
    {"key": "DE", "label": "Allemand (DE)", "members": ["DE"]},
    {"key": GROUP_ENGLISH, "label": "Anglais (EN/UK/US)", "members": ["EN", "UK", "US"]},
    {"key": "EN", "label": "Anglais (EN seul)", "members": ["EN"]},
    {"key": "UK", "label": "Anglais (UK seul)", "members": ["UK"]},
    {"key": "US", "label": "Anglais (US seul)", "members": ["US"]},
    {"key": "IT", "label": "Italien (IT)", "members": ["IT"]},
    {"key": "ES", "label": "Espagnol (ES)", "members": ["ES"]},
    {"key": "PL", "label": "Polonais (PL)", "members": ["PL"]},
    {"key": "GR", "label": "Grec (GR)", "members": ["GR"]},
    {"key": "EXYU", "label": "Ex-Yougoslave (EXYU)", "members": ["EXYU"]},
    {"key": "PT", "label": "Portugais (PT)", "members": ["PT"]},
    {"key": "BE", "label": "Belge (BE)", "members": ["BE"]},
    {"key": "AL", "label": "Albanais (AL)", "members": ["AL"]},
    {
        "key": "MULTI",
        "label": "Multi-langues (ALL/MULTI)",
        "members": ["MULTI"],
        "hint": "Plusieurs pistes audio en général, la langue n'est pas garantie",
    },
    {"key": OTHER, "label": "Autre", "members": [OTHER]},
]

_KNOWN = {m for lang in LANGUAGES for m in lang["members"]}
_MEMBERS = {lang["key"]: frozenset(lang["members"]) for lang in LANGUAGES}
_ALIASES = {
    "EX-YU": "EXYU",
    "EX YU": "EXYU",
    "EXYU": "EXYU",
    "D": "DE",
    "ENG": "EN",
    "ALL": "MULTI",
    "U.K": "UK",
    "U.K.": "UK",
}
_NAME_RE = re.compile(r"^\s*\[\[?\s*([^\]\[]{1,12})\]")
_CAT_RE = re.compile(r"^\s*([A-Za-z][A-Za-z.\- ]{0,5}?)[\s\xa0]*(?:[|:]|I(?=[\s\xa0]))")


def _normalize(token: str) -> str | None:
    """Map a raw prefix to a known key, or None if unknown."""
    t = re.sub(r"[\s\xa0]+", " ", token).strip().upper()
    t = re.sub(r"-4K$", "", t)
    if t in _ALIASES:
        return _ALIASES[t]
    if t in _KNOWN:
        return t
    head = re.split(r"[-:\s+]", t, maxsplit=1)[0]
    head = _ALIASES.get(head, head)
    return head if head in _KNOWN and head != OTHER else None


def language_key(name: str | None, category_name: str | None = None) -> str:
    """Normalized language key: title prefix, else category prefix, else OTHER."""
    m = _NAME_RE.match(name or "")
    if m:
        key = _normalize(m.group(1))
        if key:
            return key
    m = _CAT_RE.match(category_name or "")
    if m:
        key = _normalize(m.group(1))
        if key:
            return key
    return OTHER


def matches(lang_key: str | None, item_key: str) -> bool:
    """True if an item with `item_key` belongs to the selected option (empty = all)."""
    if not lang_key:
        return True
    members = _MEMBERS.get(lang_key)
    return item_key in members if members is not None else False


def is_valid(lang_key: str | None) -> bool:
    return not lang_key or lang_key in _MEMBERS


def category_names(categories: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    return {
        (str(c.get("source_id", "")), str(c.get("category_id"))): c.get("category_name") or ""
        for c in categories
    }


def stream_language(item: dict, cat_names: Mapping[tuple[str, str], str]) -> str:
    """Language key of a catalog item, memoized on the item dict (computed once)."""
    key = item.get(MEMO_FIELD)
    if key is None:
        cat = cat_names.get((str(item.get("source_id", "")), str(item.get("category_id"))), "")
        key = item[MEMO_FIELD] = language_key(item.get("name"), cat)
    return key


def filter_by_language(
    items: list[dict], lang_key: str | None, cat_names: Mapping[tuple[str, str], str]
) -> list[dict]:
    if not lang_key:
        return items
    return [i for i in items if matches(lang_key, stream_language(i, cat_names))]


def option_counts(
    items: Iterable[dict], cat_names: Mapping[tuple[str, str], str]
) -> dict[str, int]:
    """Number of items per selectable option, plus '' for the total."""
    per_item = Counter(stream_language(i, cat_names) for i in items)
    counts = {lang["key"]: sum(per_item[m] for m in lang["members"]) for lang in LANGUAGES}
    counts[""] = sum(per_item.values())
    return counts
