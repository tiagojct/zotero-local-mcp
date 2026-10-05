"""Keyword pre-screen: a deterministic score from title and abstract.

Used by search_multi and by the weekly alerts. Matching is case- and
accent-insensitive and works on whole words or phrases, so "copd" does not
match "copdx" and "Lúng" matches "lung". It only orders and labels works; it
never drops them.
"""

from __future__ import annotations

import re
import unicodedata

PASS, EXCLUDE, NO_TERMS = "pass", "exclude", "no_terms_matched"


def normalize(text: str | None) -> str:
    """Lower case, no accents, every run of non-alphanumeric characters as one space."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return " ".join(re.split(r"[\W_]+", text)).strip()


def parse_terms(value: str | list[str] | None) -> list[str]:
    """Terms from a list or a comma-separated string; blanks and duplicates removed."""
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else value
    out: list[str] = []
    for item in items:
        term = (item or "").strip().strip("[]").strip().strip("'\"").strip()
        if term and normalize(term) and term not in out:
            out.append(term)
    return out


def _has(text: str, term: str) -> bool:
    t = normalize(term)
    return bool(t) and f" {t} " in f" {text} "


def prescreen(title: str | None, abstract: str | None, include: list[str] | None,
              exclude: list[str] | None) -> dict:
    """+2 per include term in the title, +1 per include term only in the abstract.
    'exclude' when an exclude term is in the title or abstract, else 'pass' when the score
    is above 0, else 'no_terms_matched'."""
    t, a = normalize(title), normalize(abstract)
    score = 0
    for term in include or []:
        if _has(t, term):
            score += 2
        elif _has(a, term):
            score += 1
    if any(_has(t, term) or _has(a, term) for term in exclude or []):
        decision = EXCLUDE
    else:
        decision = PASS if score > 0 else NO_TERMS
    return {"score": score, "prescreen": decision}
