"""Weekly literature alerts. Run with: zotero-alerts

Reads saved searches from a vault note (ZOTERO_ALERTS, default
Systems/Literature alerts.md), finds works from the last N days in PubMed and
OpenAlex, drops works already in the library or already reported, and writes
Inbox/Literature alerts YYYY-MM-DD.md with tick boxes. No AI model is used.
The librarian's import_queue tool imports the ticked lines.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sys
from pathlib import Path

from .client import ZoteroError
from .config import Settings
from .external import External, ExternalError
from .library import Library
from .records import LibraryIndex
from .scholar import ReadOnlyZotero, short_authors

BULLET = re.compile(r"^\s*[-*+]\s+`([^`]+)`\s*(.*)$")
KEEP_SEEN_DAYS = 365


def parse_config(path: Path) -> tuple[dict, list[tuple[str, str, str]]]:
    text = path.read_text(encoding="utf-8")
    opts = {"days": 7, "max_per_query": 20}
    body = text
    fm = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if fm:
        for line in fm.group(1).splitlines():
            m = re.match(r"^\s*(days|max_per_query)\s*:\s*(\d+)", line)
            if m:
                opts[m.group(1)] = int(m.group(2))
        body = text[fm.end():]
    queries, source = [], None
    for line in body.splitlines():
        h = re.match(r"^#+\s*(pubmed|openalex)\b", line.strip(), re.I)
        if h:
            source = h.group(1).lower()
            continue
        if line.startswith("#"):
            source = None
            continue
        m = BULLET.match(line)
        if m and source:
            queries.append((source, m.group(1).strip(), m.group(2).strip() or m.group(1).strip()))
    return opts, queries


def idents_of(rec: dict) -> list[str]:
    ids = [rec.get("doi"), f"pmid:{rec['pmid']}" if rec.get("pmid") else None, rec.get("openalex_id")]
    return [i for i in ids if i]


def line_for(rec: dict) -> str:
    bits = [f"{(rec.get('title') or '').rstrip('.')}."]
    who = short_authors(rec)
    if who:
        bits.append(f"{who}.")
    venue = " ".join(x for x in (rec.get("container"), rec.get("year")) if x)
    if venue:
        bits.append(f"{venue}.")
    ids = []
    if rec.get("doi"):
        ids.append(f"doi:{rec['doi']}")
    if rec.get("pmid"):
        ids.append(f"pmid:{rec['pmid']}")
    if not ids and rec.get("openalex_id"):
        ids.append(f"https://openalex.org/{rec['openalex_id']}")
    return "- [ ] " + " ".join(bits + ids)


async def run(settings: Settings, today: dt.date | None = None, ext: External | None = None,
              lib: Library | None = None) -> dict:
    today = today or dt.date.today()
    if settings.alerts_config is None or not settings.alerts_config.exists():
        raise SystemExit(f"No alerts note at {settings.alerts_config}. Set ZOTERO_VAULT or ZOTERO_ALERTS.")
    if settings.vault is None:
        raise SystemExit("Set ZOTERO_VAULT so the alert note can be written to the Inbox.")
    opts, queries = parse_config(settings.alerts_config)
    ext = ext or External(settings.email, settings.ncbi_api_key, settings.openalex_api_key)
    lib = lib or Library(settings, ReadOnlyZotero(settings.api_url, settings.state_dir, settings.auth_timeout))
    warnings = []
    try:
        index = LibraryIndex(await lib.regular_items())
    except ZoteroError as exc:
        index = None
        warnings.append(f"Zotero was not reachable, so works already in the library are not filtered out ({exc}).")
    seen_path = settings.state_dir / "alerts-seen.json"
    try:
        seen: dict[str, str] = json.loads(seen_path.read_text())
    except (OSError, ValueError):
        seen = {}
    sections, total = [], 0
    new_seen: dict[str, str] = {}
    since = today - dt.timedelta(days=opts["days"])
    for source, query, label in queries:
        try:
            if source == "pubmed":
                hits, ids = await ext.pubmed_search(query, opts["max_per_query"], reldays=opts["days"],
                                                    sort="pub_date")
                recs = await ext.pubmed_fetch(ids)
            else:
                hits, recs = await ext.openalex_search(query, opts["max_per_query"], since=since,
                                                       sort="publication_date")
            if hits > opts["max_per_query"]:
                warnings.append(f"{label} ({source}): {hits} hits, only the newest {opts['max_per_query']} "
                                "were checked. Narrow the search or raise max_per_query.")
        except ExternalError as exc:
            warnings.append(f"{label} ({source}): {exc}")
            continue
        lines = []
        for rec in recs:
            ids = idents_of(rec)
            if not ids or any(i in seen or i in new_seen for i in ids) or (index and index.match_record(rec)):
                continue
            for i in ids:
                new_seen[i] = today.isoformat()
            lines.append(line_for(rec))
        if lines:
            sections.append(f"## {label} ({source})\n\n" + "\n".join(lines))
            total += len(lines)
    note = None
    if sections or warnings:
        inbox = settings.vault / "Inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        name = f"Literature alerts {today.isoformat()}"
        note = inbox / f"{name}.md"
        n = 2
        while note.exists():
            note = inbox / f"{name} ({n}).md"
            n += 1
        rel = note.relative_to(settings.vault)
        head = (f"---\ncreated: {today.isoformat()}\n---\n"
                f"New works from the saved searches in [[Literature alerts]], published in the last "
                f"{opts['days']} days and not in the library. Tick what you want, then ask the librarian: "
                f"\"Import the ticked items from {rel}\".\n")
        parts = [head, *sections]
        if warnings:
            parts.append("## Warnings\n\n" + "\n".join(f"- {w}" for w in warnings))
        if not sections:
            parts.insert(1, "No new works this week.")
        note.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    # Mark works as seen only after the note is written, so a failed run loses nothing.
    cutoff = (today - dt.timedelta(days=KEEP_SEEN_DAYS)).isoformat()
    seen = {k: v for k, v in {**seen, **new_seen}.items() if v >= cutoff}
    seen_path.parent.mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps(seen))
    return {"new_works": total, "note": str(note) if note else None, "warnings": warnings}


def main() -> None:
    result = asyncio.run(run(Settings.from_env()))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0)


if __name__ == "__main__":
    main()
