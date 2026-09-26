"""The tag vocabulary: a Markdown file that is the only source of allowed tags.

Format (see examples/zotero-tags.md):

    ---
    required_facets: topic, status
    single_facets: status
    max_per_facet: topic=4, type=2
    ---
    ## topic
    - `topic/spirometry` Lung function testing by spirometry. aliases: pft, lung function

A tag is the first backticked token of a bullet line. The facet is the part
before the first "/". Text after "aliases:" lists old or alternative names;
the agent uses them to map existing tags. Tags that start with "_" are system
tags (for example the review marker) and are always allowed.

The file is re-read when it changes, so edits in Obsidian apply at once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

BULLET = re.compile(r"^\s*[-*+]\s+`([^`]+)`(.*)$")
FRONT_KEY = re.compile(r"^\s*([a-z_]+)\s*:\s*(.*)$")


def facet_of(tag: str) -> str | None:
    return tag.split("/", 1)[0] if "/" in tag else None


def _limits(value: str) -> dict[str, int]:
    """'topic=4, type=2' (or 'topic: 4') -> {'topic': 4, 'type': 2}."""
    out: dict[str, int] = {}
    for part in _split_list(value):
        m = re.match(r"^([a-z_-]+)\s*[=:]\s*(\d+)$", part)
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def _split_list(value: str) -> list[str]:
    value = value.strip().strip("[]")
    return [v.strip().strip("'\"") for v in value.split(",") if v.strip()]


@dataclass
class Entry:
    tag: str
    description: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass
class Vocabulary:
    path: Path
    entries: dict[str, Entry]
    required_facets: list[str]
    single_facets: list[str]
    problems: list[str]
    mtime: float = 0.0
    max_per_facet: dict[str, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Vocabulary":
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        front: dict[str, str] = {}
        body_start = 0
        if lines and lines[0].strip() == "---":
            current = None
            for i, line in enumerate(lines[1:], start=1):
                if line.strip() == "---":
                    body_start = i + 1
                    break
                m = FRONT_KEY.match(line)
                if m:
                    current = m.group(1)
                    front[current] = m.group(2)
                elif current and re.match(r"^\s+-\s+", line):
                    # YAML block list, as Obsidian's property editor writes it
                    item = re.sub(r"^\s+-\s+", "", line).strip()
                    front[current] = ", ".join(x for x in (front[current].strip(), item) if x)
        entries: dict[str, Entry] = {}
        problems: list[str] = []
        for n, line in enumerate(lines[body_start:], start=body_start + 1):
            m = BULLET.match(line)
            if not m:
                continue
            tag = m.group(1).strip()
            rest = m.group(2).strip()
            aliases: list[str] = []
            am = re.search(r"aliases\s*:\s*(.*)$", rest, flags=re.IGNORECASE)
            if am:
                aliases = _split_list(am.group(1))
                rest = rest[: am.start()].strip()
            rest = rest.lstrip("-:— ").strip()
            if tag in entries:
                problems.append(f"line {n}: duplicate tag {tag} (first one kept)")
                continue
            if tag != tag.strip() or " " in tag:
                problems.append(f"line {n}: tag contains spaces: {tag!r}")
            if facet_of(tag) is None:
                problems.append(f"line {n}: tag has no facet prefix (facet/name): {tag}")
            entries[tag] = Entry(tag, rest, aliases)
        vocab = cls(
            path=path,
            entries=entries,
            required_facets=_split_list(front.get("required_facets", "")),
            single_facets=_split_list(front.get("single_facets", "")),
            problems=problems,
            mtime=path.stat().st_mtime,
            max_per_facet=_limits(front.get("max_per_facet", "")),
        )
        return vocab

    @property
    def facets(self) -> list[str]:
        seen: list[str] = []
        for tag in self.entries:
            f = facet_of(tag)
            if f and f not in seen:
                seen.append(f)
        return seen

    def allows(self, tag: str) -> bool:
        return tag.startswith("_") or tag in self.entries

    def alias_map(self) -> dict[str, str]:
        """Lower-cased alias -> canonical tag."""
        out: dict[str, str] = {}
        for e in self.entries.values():
            for a in e.aliases:
                out[a.lower()] = e.tag
        return out

    def as_dict(self) -> dict:
        grouped: dict[str, list[dict]] = {}
        for e in self.entries.values():
            grouped.setdefault(facet_of(e.tag) or "(none)", []).append(
                {"tag": e.tag, "description": e.description, "aliases": e.aliases}
            )
        return {
            "path": str(self.path),
            "required_facets": self.required_facets,
            "single_facets": self.single_facets,
            "max_per_facet": self.max_per_facet,
            "tag_count": len(self.entries),
            "facets": grouped,
            "problems": self.problems,
        }


class VocabularyStore:
    """Loads the vocabulary lazily and reloads it when the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._vocab: Vocabulary | None = None

    def get(self) -> Vocabulary | None:
        if self.path is None or not self.path.exists():
            return None
        mtime = self.path.stat().st_mtime
        if self._vocab is None or self._vocab.mtime != mtime:
            self._vocab = Vocabulary.load(self.path)
        return self._vocab

    def require(self) -> Vocabulary:
        vocab = self.get()
        if vocab is None:
            where = self.path or "(ZOTERO_VOCAB is not set)"
            raise LookupError(
                f"No tag vocabulary at {where}. Create it first; tag writes are blocked without it."
            )
        return vocab
