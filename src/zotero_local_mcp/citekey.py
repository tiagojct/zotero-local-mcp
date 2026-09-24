"""Citation keys in the form jacinto2026, jacinto2026a, jacinto2026b.

Base key: surname of the first author (editor if there is no author, else the
first creator), lower case, accents removed, only a-z and 0-9, plus the
four-digit year ("nd" when there is no year). Institutional names are joined
("World Health Organization" -> worldhealthorganization).

Where the key is stored: in the native 'citationKey' field when the item
JSON has one, otherwise as a 'Citation Key: ...' line in Extra. Better BibTeX
reads both, so installing it later keeps these keys.
"""

from __future__ import annotations

import re
import string
import unicodedata

EXTRA_LINE = re.compile(r"^\s*citation key\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
YEAR = re.compile(r"(?<!\d)(1[5-9]\d\d|20\d\d|21\d\d)(?!\d)")


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("ß", "ss").replace("ø", "o").replace("æ", "ae").replace("ł", "l")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def year_of(data: dict) -> str:
    m = YEAR.search(data.get("date") or "")
    return m.group(1) if m else ""


def first_creator_name(data: dict) -> str:
    creators = data.get("creators") or []
    for role in ("author", "editor", None):
        for c in creators:
            if role is None or c.get("creatorType") == role:
                return c.get("lastName") or c.get("name") or ""
    return ""


def base_key(data: dict) -> str:
    name = slug(first_creator_name(data))
    if not name:
        words = [w for w in re.split(r"\s+", data.get("title") or "") if w]
        name = slug(words[0]) if words else "anon"
    return f"{name or 'anon'}{year_of(data) or 'nd'}"


def current_key(data: dict) -> str | None:
    if data.get("citationKey"):
        return data["citationKey"]
    m = EXTRA_LINE.search(data.get("extra") or "")
    return m.group(1) if m else None


def suffixes():
    for c in string.ascii_lowercase:
        yield c
    for a in string.ascii_lowercase:
        for b in string.ascii_lowercase:
            yield a + b


def unique_key(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    for s in suffixes():
        if base + s not in taken:
            return base + s
    raise ValueError(f"no free suffix for {base}")


def with_key(data: dict, key: str) -> dict:
    """Return the changed fields that store `key` on this item."""
    if "citationKey" in data:
        return {"citationKey": key}
    extra = data.get("extra") or ""
    lines = [ln for ln in extra.splitlines() if not EXTRA_LINE.match(ln)]
    return {"extra": "\n".join([f"Citation Key: {key}", *lines]).strip()}
