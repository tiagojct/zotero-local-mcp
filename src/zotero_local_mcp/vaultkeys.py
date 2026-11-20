"""Citation keys in the Sub-Sub folder (ZOTERO_VAULT): the keys of the literature notes, and the
keys that notes cite. A new citekey must not take a key that a literature note has, and
set_citekeys(force=True) must not change a key that the folder uses.

The citation patterns are the same as in zotero-gam (content/lib/refs.mjs). The shared cases are in
tests/fixtures/citekey-cases.json, section "cited". zotero-gam checks the whole Obsidian vault; this
module sees only the Sub-Sub folder.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

from .client import ZoteroError
from .layout import dir_path

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
# [\w] is letters, digits and "_" (Unicode), as [\p{L}\p{N}_] in JavaScript.
PANDOC = re.compile(r"(?<![\w.@])@([^\W_][\w:.-]*)")
PANDOC_BRACED = re.compile(r"(?<![\w.@])@\{([^{}\s]+)\}")
FRONTMATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
CITEKEY_LINE = re.compile(r"^citekey:[ \t]*(.+?)[ \t]*$", re.M)


def cited_keys(text: str) -> set[str]:
    """The citekeys that a note cites: [[key]], [[folder/key.md|alias]], @key, [@key], [@{key}]."""
    src = unicodedata.normalize("NFC", text or "")
    keys: set[str] = set()
    for m in WIKILINK.finditer(src):
        target = re.sub(r"\.md$", "", m.group(1).strip().split("/")[-1])
        if target and not re.search(r"\s", target):
            keys.add(target)
    for m in PANDOC.finditer(src):
        keys.add(re.sub(r"[:.-]+$", "", m.group(1)))
    for m in PANDOC_BRACED.finditer(src):
        keys.add(m.group(1))
    return keys


def _markdown_files(root: Path):
    """The .md files under root. Hidden files and folders are left out; links to folders are not followed."""
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.endswith(".md") and not name.startswith("."):
                yield Path(folder) / name


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _frontmatter_key(text: str) -> str | None:
    fm = FRONTMATTER.match(text)
    line = CITEKEY_LINE.search(fm.group(1)) if fm else None
    if not line:
        return None
    raw = line.group(1)
    if raw.startswith('"'):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw.strip("'") or None


def _check(vault: Path) -> None:
    """A folder that is set but cannot be read gives an error, not an empty set: an empty set would
    let set_citekeys change keys that notes use."""
    if not Path(vault).is_dir() or not os.access(vault, os.R_OK | os.X_OK):
        raise ZoteroError(f"The Sub-Sub folder (ZOTERO_VAULT) cannot be read: {vault}. Nothing was changed.")


def literature_keys(vault: Path | None) -> set[str]:
    """The keys of the literature notes in Literature/: the file name and the frontmatter citekey."""
    if not vault:
        return set()
    _check(vault)
    folder = dir_path(Path(vault), "Literature")
    keys: set[str] = set()
    for path in _markdown_files(folder):
        keys.add(unicodedata.normalize("NFC", path.stem))
        key = _frontmatter_key(_read(path))
        if key:
            keys.add(unicodedata.normalize("NFC", key))
    return keys


def taken_in_vault(vault: Path | None) -> set[str]:
    """Keys that a new citekey must not take. The lower-case forms are there too: on macOS
    smith2020.md and Smith2020.md are the same file."""
    keys = literature_keys(vault)
    return keys | {k.lower() for k in keys}


def used_in_vault(vault: Path | None) -> set[str]:
    """Keys, in lower case, that the folder uses: a literature note or a citation in any note."""
    if not vault:
        return set()
    keys = set(literature_keys(vault))  # checks the folder
    for path in _markdown_files(Path(vault)):
        keys |= cited_keys(_read(path))
    return {k.lower() for k in keys}
