"""Preview or apply tag review notes from the terminal, several at a time.

    zotero-review preview NOTE...        what each note would change (writes nothing)
    zotero-review apply NOTE... [--yes]  apply the notes, one after the other

NOTE is a path in the vault ("Inbox/Zotero tag review 13.md") or a pattern in
quotes ("Inbox/Zotero tag review *.md"). Notes that were already applied (they
have an "Applied" line) are left out unless --again is given. apply shows the
preview first and asks before it writes, unless --yes. Every write is journaled:
undo it in Sub-Sub (librarian mode: history, then undo).

Reads the same settings as the MCP server (ZOTERO_MCP_ENV or
~/.config/zotero-local-mcp/env). Zotero must be running.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import re
import sys
from pathlib import Path

from .client import ZoteroError
from .config import Settings
from .library import Library


def natural(p: Path) -> list:
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", p.name)]


def expand(args: list[str], vault: Path | None) -> tuple[list[Path], list[str]]:
    notes: list[Path] = []
    missing: list[str] = []
    for a in args:
        p = Path(a).expanduser()
        if not p.is_absolute() and vault is not None and not p.exists():
            p = vault / p
        if any(c in a for c in "*?["):
            hits = sorted((Path(h) for h in glob.glob(str(p))), key=natural)
            hits = [h for h in hits if h.suffix == ".md"]
            notes += hits
            if not hits:
                missing.append(a)
        elif p.exists() or p.with_name(p.name + ".md").exists():
            notes.append(p if p.exists() else p.with_name(p.name + ".md"))
        else:
            missing.append(a)
    seen: set[Path] = set()
    return [n for n in notes if not (n in seen or seen.add(n))], missing


def summary_line(name: str, res: dict) -> str:
    parts = [f"{res.get('rows', 0)} rows", f"{res.get('would_change', 0)} would change"]
    if res.get("skipped"):
        parts.append(f"{len(res['skipped'])} skipped")
    if res.get("changed_since_note"):
        parts.append(f"{res['changed_since_note']['count']} changed since the note was written")
    return f"{name}: " + ", ".join(parts)


async def run(args: argparse.Namespace, lib: Library | None = None) -> int:
    own = lib is None
    lib = lib or Library(Settings.from_env())
    notes, missing = expand(args.notes, lib.s.vault)
    for m in missing:
        print(f"No note matches: {m}")
    if not notes:
        return 2
    ready: list[tuple[Path, int]] = []
    problems = 0
    try:
        for p in notes:
            try:
                res = await lib.apply_tag_review(str(p), mark_reviewed=not args.keep_marker,
                                                 dry_run=True, again=args.again)
            except ZoteroError as exc:
                problems += 1
                print(f"{p.name}: NOT READY\n  " + str(exc).replace("\n", "\n  "))
                continue
            if res.get("already_applied") and not args.again:
                print(f"{p.name}: already applied ({', '.join(res['already_applied'])}); left out")
                continue
            print(summary_line(p.name, res))
            for k, why in (res.get("skipped") or {}).items():
                print(f"  skipped {k}: {why}")
            for k, ch in ((res.get("changed_since_note") or {}).get("items") or {}).items():
                print(f"  changed since the note: {k} {ch['item']}\n    note: {', '.join(ch['in_note'])}"
                      f"\n    now:  {', '.join(ch['now'])}")
            if args.verbose:
                for ch in res.get("changes", []):
                    print(f"  {ch['key']} {ch['item']}\n    + {', '.join(ch.get('added', []))}"
                          f"\n    - {', '.join(ch.get('removed', []))}")
            if res.get("would_change"):
                ready.append((p, res["would_change"]))
        if args.cmd == "preview":
            print(f"\n{len(ready)} notes, {sum(n for _, n in ready)} items would change. Nothing was written.")
            return 1 if problems else 0
        if problems:
            print(f"\n{problems} notes are not ready. Fix them, or leave them out. Nothing was written.")
            return 1
        if not ready:
            print("\nNothing to apply.")
            return 0
        total = sum(n for _, n in ready)
        if not args.yes:
            answer = input(f"\nApply {len(ready)} notes ({total} items)? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Nothing was written.")
                return 0
        await lib.z.connect()
        if not lib.z.has_remembered_key:
            print("Zotero will ask for permission to write. Choose Always Allow in the Zotero window.")
        for p, _ in ready:
            res = await lib.apply_tag_review(str(p), mark_reviewed=not args.keep_marker,
                                             dry_run=False, again=args.again)
            line = f"{p.name}: applied {res.get('applied', 0)}"
            if res.get("unchanged"):
                line += f", unchanged {res['unchanged']}"
            if res.get("failed"):
                line += f", FAILED {len(res['failed'])}: " + "; ".join(f"{k} {v}" for k, v in res["failed"].items())
            print(line + (f" (journal {res['journal_id']})" if res.get("journal_id") else ""))
            if res.get("error"):
                print(f"Stopped: {res['error']}\n{res.get('status', '')}")
                return 1
        return 0
    finally:
        if own:
            await lib.z.aclose()


def main() -> None:
    ap = argparse.ArgumentParser(prog="zotero-review", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["preview", "apply"])
    ap.add_argument("notes", nargs="+", help="note paths or patterns (in quotes), relative to the vault")
    ap.add_argument("--yes", action="store_true", help="apply without asking")
    ap.add_argument("--again", action="store_true", help="include notes that were already applied")
    ap.add_argument("--keep-marker", action="store_true", help="keep the review marker (_agent)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the change for every item")
    args = ap.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except ZoteroError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
