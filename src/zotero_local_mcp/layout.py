"""The layout of the Sub-Sub folder (ZOTERO_VAULT): TitleCase folders and names (Inbox/, "Zotero
import queue.md") or the lowercase style (inbox/, zotero-import-queue.md). The style is read from the
folder names on disk. Same rules as Subsub (src/layout.ts)."""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Literal

Style = Literal["title", "lower"]

LOWER_DIRS = ("inbox", "literature", "syntheses", "research", "zotero")
GREEK = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "κ": "kappa", "μ": "mu"}
SETTINGS_LOWER = {"tags": "zotero-tags.md", "rules": "zotero-agent.md", "alerts": "literature-alerts.md"}
SETTINGS_TITLE = {"tags": "Zotero tags.md", "rules": "Zotero agent.md", "alerts": "Literature alerts.md"}


def layout_style(vault: Path) -> Style:
    """"lower" when the folder holds a subfolder with an exact lowercase name. The names come from
    os.listdir: on macOS, Path.exists finds Inbox/ for inbox/."""
    try:
        names = set(os.listdir(vault))
    except OSError:
        return "title"
    return "lower" if names.intersection(LOWER_DIRS) else "title"


def dir_path(vault: Path, name: str) -> Path:
    """The path of a subfolder (given in TitleCase, e.g. "Inbox"). A folder that exists with the exact
    name, or with the lowercase name, is used as it is. Only when neither exists, the style decides."""
    try:
        names = set(os.listdir(vault))
    except OSError:
        names = set()
    if name in names:
        return vault / name
    if name.lower() in names:
        return vault / name.lower()
    return vault / (name.lower() if layout_style(vault) == "lower" else name)


def _slug(text: str, limit: int) -> str:
    for g, w in GREEK.items():
        text = text.replace(g, w)
    text = re.sub("[ºª'’‘`]", "", text)
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()
    lead = "_" if text.startswith("_") else ""
    text = re.sub(r"[^a-z0-9_]+", "-", text)
    text = re.sub(r"^[-_]+|[-_]+$", "", text)
    text = re.sub(r"-{2,}", "-", text)
    if len(text) > limit:
        cut = text.rfind("-", 0, limit + 1)
        text = re.sub(r"[-_]+$", "", text[:cut] if cut > 0 else text[:limit])
    return lead + text


def note_name(vault: Path, title: str) -> str:
    """The name (no extension) of a note this server writes. Title style: the title as it is. Lower
    style: a slug; a leading date YYYY-MM-DD becomes YYYY-MM-DD_slug."""
    if layout_style(vault) == "title":
        return title
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?:[\s_-]+(.+))?", title, flags=re.S)
    if m:
        rest = _slug(m.group(2), 69) if m.group(2) else ""
        return f"{m.group(1)}_{rest}" if rest else m.group(1)
    return _slug(title, 80)


def settings_path(vault: Path, which: str) -> Path | None:
    """Default place of a settings file (tags, rules, alerts). Lower style: zotero/<name>. Title
    style: Systems/<name> for alerts (today's default); no default for the others."""
    if layout_style(vault) == "lower":
        return vault / "zotero" / SETTINGS_LOWER[which]
    if which == "alerts":
        return vault / "Systems" / SETTINGS_TITLE[which]
    return None
