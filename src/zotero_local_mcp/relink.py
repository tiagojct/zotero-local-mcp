"""Change the obsidian:// links in the Zotero child notes after the vault was renamed.

    zotero-relink --map PLAN.tsv            show the notes that would change (writes nothing)
    zotero-relink --map PLAN.tsv --apply    change them

PLAN.tsv has a header row "old<TAB>new<TAB>kind<TAB>note". The paths are relative to the vault
root (the folder with .obsidian/), as in the file= part of the link that attach_note writes
(obsidian://open?vault=<vault>&file=<path>). Only the file= value changes, and vault= is set to the --vault value (the name is compared
without case: the notes have vault=Powerslave). Each changed note is
one line: item key, old file, new file. Zotero asks for permission to write. Zotero must be running.
Reads the same settings as the MCP server (ZOTERO_MCP_ENV or ~/.config/zotero-local-mcp/env).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from .client import LocalZotero, ZoteroError
from .config import Settings

URI = re.compile(r"obsidian://open\?([^\"'<>\s#]*)")
SEP = re.compile(r"(&amp;|&)")
HEADER = ["old", "new", "kind", "note"]


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def read_map(path: Path) -> dict[str, str]:
    """old -> new from the TSV. Rows without a change are left out. The kind column is ignored.
    A wrong header or a row with too few columns is an error."""
    plan: dict[str, str] = {}
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f, delimiter="\t"))
    if not rows or [c.strip() for c in rows[0][:4]] != HEADER:
        raise ZoteroError(f"{path}: the first row must be the header: old, new, kind, note (tab-separated).")
    for n, row in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in row):
            continue
        if len(row) < 2 or not row[0].strip() or not row[1].strip():
            raise ZoteroError(f"{path}, line {n}: old and new are needed (tab-separated).")
        old, new = nfc(row[0].strip()), nfc(row[1].strip())
        if old != new:
            plan[old] = new
    return plan


def rewrite(html_text: str, plan: dict[str, str], vault: str) -> tuple[str, list[tuple[str, str]]]:
    """The note with the file= values changed, and the (old, new) file pairs it changed."""
    done: list[tuple[str, str]] = []

    def one(m: re.Match) -> str:
        parts = SEP.split(m.group(1))  # key=value, separator, key=value, ...
        fields = {p.split("=", 1)[0]: i for i, p in enumerate(parts) if i % 2 == 0 and "=" in p}
        if "vault" not in fields or "file" not in fields:
            return m.group(0)
        if unquote(parts[fields["vault"]].split("=", 1)[1]).casefold() != vault.casefold():
            return m.group(0)
        file = nfc(unquote(parts[fields["file"]].split("=", 1)[1]))
        has_md = file.endswith(".md")
        old_md = file if has_md else file + ".md"
        new_md = plan.get(old_md)
        if new_md is None:
            return m.group(0)
        new_file = new_md if has_md else re.sub(r"\.md$", "", new_md)
        parts[fields["file"]] = "file=" + quote(new_file, safe="")
        parts[fields["vault"]] = "vault=" + quote(vault, safe="")
        done.append((old_md, new_md))
        return "obsidian://open?" + "".join(parts)

    return URI.sub(one, html_text), done


class RelinkError(ZoteroError):
    def __init__(self, message: str, written: list[str], not_written: list[str]) -> None:
        super().__init__(message)
        self.written, self.not_written = written, not_written


async def relink(z: Any, plan: dict[str, str], apply: bool = False, vault: str = "powerslave") -> list[dict]:
    """Find the child notes that link to an old path. With apply, write them. Returns one dict per
    changed note: key, old, new."""
    changes: list[dict] = []
    updates: list[dict] = []
    for item in await z.all_items():
        data = item.get("data", {})
        if data.get("itemType") != "note" or not data.get("parentItem"):
            continue
        text, done = rewrite(data.get("note", ""), plan, vault)
        if not done:
            continue
        for old, new in done:
            changes.append({"key": item["key"], "old": old, "new": new})
        updates.append({"key": item["key"], "version": data.get("version", item.get("version")), "note": text})
    if apply and updates:
        # update_items sends at most 50 objects per request and stops at the first failed request.
        res = await z.update_items(updates)
        if res.error or res.failed:
            written = [*res.succeeded, *res.unchanged]
            not_written = [u["key"] for u in updates if u["key"] not in written]
            raise RelinkError(f"Zotero did not save every note: {res.error or res.failed}", written, not_written)
    return changes


async def run(map_path: Path, apply: bool = False, client: Any = None, vault: str = "powerslave") -> int:
    plan = read_map(map_path)
    if not plan:
        print(f"No changes in {map_path}.")
        return 2
    own = client is None
    if own:
        s = Settings.from_env()
        client = LocalZotero(s.api_url, s.state_dir, s.auth_timeout)
    try:
        if own:
            await client.connect()
            if apply and not client.has_remembered_key:
                print("Zotero will ask for permission to write. Choose Always Allow in the Zotero window.")
        changes = await relink(client, plan, apply, vault)
    finally:
        if own:
            await client.aclose()
    for c in changes:
        print(f"{c['key']}\t{c['old']}\t{c['new']}")
    if apply:
        print(f"{len(changes)} links changed.")
    else:
        print(f"{len(changes)} links would change. Nothing was written. Use --apply to write.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="zotero-relink", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--map", required=True, type=Path, help="TSV with the columns old, new, kind, note")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: show only)")
    ap.add_argument("--vault", default="powerslave", help="Obsidian vault name in the links (default powerslave)")
    args = ap.parse_args()
    try:
        sys.exit(asyncio.run(run(args.map, args.apply, vault=args.vault)))
    except RelinkError as exc:
        print(f"Error: {exc}")
        print(f"Written ({len(exc.written)}): {' '.join(exc.written) or 'none'}")
        print(f"Not written ({len(exc.not_written)}): {' '.join(exc.not_written) or 'none'}")
        sys.exit(1)
    except (ZoteroError, OSError) as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
