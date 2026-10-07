"""Library operations behind the MCP tools.

Rules that every write follows:
- dry_run=True by default: the call returns a preview and writes nothing;
- tags that are added must exist in the vocabulary file (system tags start with "_");
- each item write carries the item version, so newer edits are never overwritten
  (on a version conflict the item is re-read and the change re-applied once);
- every applied write is recorded in the journal and can be undone;
- nothing is ever deleted: items go to the trash, tags are removed from items.

Tag operations cover top-level regular items (not notes, attachments or annotations).
"""

from __future__ import annotations

import datetime as dt

import html
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import markdown as md

from .citekey import base_key, current_key, first_creator_name, unique_key, with_key, year_of
from .client import LocalZotero, ZoteroError
from .config import Settings
from .journal import Journal
from .vocab import VocabularyStore, facet_of

NON_REGULAR = {"note", "attachment", "annotation"}
PREVIEW_LIMIT = 60
PROTECTED_FIELDS = {
    "key", "version", "tags", "collections", "relations", "deleted", "itemType",
    "parentItem", "dateAdded", "dateModified",
}

Editor = Callable[[dict], "dict | None"]


class Skip(Exception):
    """Raised by an editor to leave one item unchanged, with a reason."""


# ---------------------------------------------------------------- helpers

def ttype(t: dict) -> int:
    return int(t.get("type") or 0)


def manual(data: dict) -> list[str]:
    return [t["tag"] for t in data.get("tags") or [] if ttype(t) == 0]


def automatic(data: dict) -> list[str]:
    return [t["tag"] for t in data.get("tags") or [] if ttype(t) == 1]


def norm(field: str, value: Any) -> Any:
    if field == "tags":
        return sorted((t["tag"], ttype(t)) for t in value or [])
    if field == "collections":
        return sorted(value or [])
    if field == "deleted":
        return bool(value)
    if value is None:
        return ""
    return value


def writable(field: str, value: Any) -> Any:
    if value is not None:
        return value
    return {"tags": [], "collections": [], "deleted": False, "creators": []}.get(field, "")


def regular(item: dict) -> bool:
    return item["data"].get("itemType") not in NON_REGULAR


def short_creators(data: dict, n: int = 3) -> str:
    names = [c.get("lastName") or c.get("name") or "" for c in data.get("creators") or []]
    names = [x for x in names if x]
    text = ", ".join(names[:n])
    return text + (" et al." if len(names) > n else "")


def truncate(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


def label(data: dict) -> str:
    who = short_creators(data, 1) or "?"
    return f"{who} {year_of(data) or 'n.d.'}: {truncate(data.get('title') or '', 80)}"


def html_to_text(text: str) -> str:
    text = re.sub(r"<(br|/p|/div|/h\d|/li)\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(text)).strip()


# ---------------------------------------------------------------- library


REVIEW_KEY = re.compile(r"^[A-Z0-9]{8}$")


def parse_review_table(text: str) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """Rows (key, tags) from Markdown tables that have a Key column and a
    "Proposed tags" (or "Tags") column. Cells may use commas, semicolons,
    spaces or backticks. Empty cells and "skip" rows are ignored."""
    rows: list[tuple[str, list[str]]] = []
    problems: list[str] = []
    key_col = tag_col = None
    for n, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s.startswith("|"):
            key_col = tag_col = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        low = [c.lower().strip("* ") for c in cells]
        if "key" in low and any(c in ("proposed tags", "tags", "proposed") for c in low):
            key_col = low.index("key")
            tag_col = next(i for i, c in enumerate(low) if c in ("proposed tags", "tags", "proposed"))
            continue
        if key_col is None or re.fullmatch(r"[\s:|-]+", s):
            continue
        if max(key_col, tag_col) >= len(cells):
            problems.append(f"line {n}: too few cells")
            continue
        key = cells[key_col].strip("` ")
        raw = cells[tag_col]
        if not REVIEW_KEY.match(key):
            problems.append(f"line {n}: '{key}' is not a Zotero item key")
            continue
        if not raw or raw.lower().startswith(("skip", "-", "keep")):
            continue
        rows.append((key, split_tags(raw)))
    return rows, problems


def split_tags(cell: str) -> list[str]:
    return [t.strip("`'\" ") for t in re.split(r"[,;]\s*|\s+", cell or "") if t.strip("`'\" ")]


def review_column(text: str, name: str) -> dict[str, str]:
    """{key: cell} for another column of a review table (e.g. "Current tags")."""
    out: dict[str, str] = {}
    key_col = col = None
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            key_col = col = None
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        low = [c.lower().strip("* ") for c in cells]
        if "key" in low and name in low:
            key_col, col = low.index("key"), low.index(name)
            continue
        if key_col is None or re.fullmatch(r"[\s:|-]+", s) or max(key_col, col) >= len(cells):
            continue
        key = cells[key_col].strip("` ")
        if REVIEW_KEY.match(key):
            out[key] = cells[col]
    return out


APPLIED_LINE = re.compile(r"^Applied (\d{4}-\d{2}-\d{2}): (\d+) items", re.M)


def review_cell(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("|", "/")).strip()


class Library:
    def __init__(self, settings: Settings, client: LocalZotero | None = None) -> None:
        self.s = settings
        self.z = client or LocalZotero(settings.api_url, settings.state_dir, settings.auth_timeout)
        self.vocab = VocabularyStore(settings.vocab_path)
        self._journal: Journal | None = None

    async def journal(self) -> Journal:
        await self.z._ensure()
        sid = self.z.server_id or "unknown"
        if self._journal is None or self._journal.dir.name != sid:
            self._journal = Journal(self.s.state_dir, sid)
        return self._journal

    async def regular_items(self, collection: str | None = None, query: str | None = None,
                            fulltext: bool = False) -> list[dict]:
        items = await self.z.top_items(collection, query, fulltext)
        return [i for i in items if regular(i) and not i["data"].get("deleted")]

    def summarize(self, item: dict, detail: bool = False) -> dict:
        d = item["data"]
        out: dict[str, Any] = {
            "key": d["key"],
            "citekey": current_key(d),
            "type": d.get("itemType"),
            "year": year_of(d),
            "authors": short_creators(d),
            "title": d.get("title", ""),
            "tags": manual(d),
        }
        if detail:
            out["venue"] = (
                d.get("publicationTitle") or d.get("bookTitle") or d.get("publisher") or ""
            )
            out["doi"] = d.get("DOI", "")
            out["abstract"] = truncate(d.get("abstractNote") or "", 1500)
            out["automatic_tags"] = automatic(d)
        return out

    # ------------------------------------------------------------ reads

    async def status(self) -> dict:
        out: dict[str, Any] = {"api_url": self.s.api_url}
        try:
            await self.z.connect()
            out.update(
                zotero="reachable",
                server_id=self.z.server_id,
                api_version=self.z.api_version,
                write_key_remembered=self.z.has_remembered_key,
            )
        except ZoteroError as exc:
            out.update(zotero="unreachable", error=str(exc))
        vocab = self.vocab.get()
        out["vocabulary"] = (
            {"path": str(vocab.path), "tags": len(vocab.entries), "facets": vocab.facets,
             "required_facets": vocab.required_facets, "single_facets": vocab.single_facets,
             "problems": vocab.problems}
            if vocab else {"path": str(self.s.vocab_path), "loaded": False}
        )
        out["review_marker"] = self.s.marker or None
        out["state_dir"] = str(self.s.state_dir)
        out["vault"] = str(self.s.vault) if self.s.vault else None
        out["contact_email_set"] = bool(self.s.email)
        return out

    async def overview(self) -> dict:
        items = await self.regular_items()
        vocab = self.vocab.get()
        types = Counter(i["data"]["itemType"] for i in items)
        facets = vocab.facets if vocab else sorted(
            {f for i in items for t in manual(i["data"]) if (f := facet_of(t))}
        )
        missing = {
            f: sum(1 for i in items if not any(facet_of(t) == f for t in manual(i["data"])))
            for f in facets
        }
        outside: Counter[str] = Counter()
        if vocab:
            for i in items:
                outside.update(t for t in manual(i["data"]) if not vocab.allows(t))
        marker = self.s.marker
        return {
            "items": len(items),
            "by_type": dict(types.most_common()),
            "without_manual_tags": sum(1 for i in items if not manual(i["data"])),
            "missing_facet": missing,
            "required_facets": vocab.required_facets if vocab else [],
            "items_with_automatic_tags": sum(1 for i in items if automatic(i["data"])),
            "manual_tags_outside_vocabulary": {"distinct": len(outside),
                                               "top": dict(outside.most_common(25))},
            "without_citekey": sum(1 for i in items if not current_key(i["data"])),
            "awaiting_review": (sum(1 for i in items if marker in manual(i["data"]))
                                if marker else None),
            "vocabulary_loaded": vocab is not None,
        }

    async def find(self, query: str | None = None, fulltext: bool = False,
                   collection: str | None = None, tags: list[str] | None = None,
                   item_type: str | None = None, missing_facet: str | None = None,
                   untagged: bool = False, outside_vocabulary: bool = False,
                   detail: bool = False, limit: int = 50, offset: int = 0) -> dict:
        items = await self.regular_items(collection, query, fulltext)
        want = set(tags or [])
        vocab = self.vocab.get() if outside_vocabulary else None
        if outside_vocabulary and vocab is None:
            raise ZoteroError("outside_vocabulary needs the vocabulary file.")

        def keep(i: dict) -> bool:
            d = i["data"]
            tags_here = manual(d)
            if want and not want <= set(tags_here) | set(automatic(d)):
                return False
            if item_type and d.get("itemType") != item_type:
                return False
            if untagged and tags_here:
                return False
            if missing_facet and any(facet_of(t) == missing_facet for t in tags_here):
                return False
            if vocab and all(vocab.allows(t) for t in tags_here):
                return False
            return True

        hits = sorted((i for i in items if keep(i)),
                      key=lambda i: i["data"].get("dateAdded", ""), reverse=True)
        limit = max(1, min(limit, 200))
        page = hits[offset : offset + limit]
        return {
            "total": len(hits),
            "offset": offset,
            "returned": len(page),
            "items": [self.summarize(i, detail) for i in page],
        }

    async def get_item(self, key: str) -> dict:
        item = await self.z.item(key)
        d = item["data"]
        out = self.summarize(item, detail=True)
        out["abstract"] = d.get("abstractNote", "")
        out["all_tags"] = [{"tag": t["tag"], "automatic": ttype(t) == 1} for t in d.get("tags") or []]
        out["fields"] = {
            k: v for k, v in d.items()
            if k not in {"key", "version", "tags", "relations", "abstractNote"} and v not in ("", [], {}, None)
        }
        names = {c["key"]: c["data"]["name"] for c in await self.z.collections()}
        out["collections"] = [{"key": c, "name": names.get(c, "?")} for c in d.get("collections") or []]
        out["zotero_link"] = f"zotero://select/library/items/{key}"
        notes, files = [], []
        if regular(item):
            for c in await self.z.children(key):
                cd = c["data"]
                if cd.get("itemType") == "note":
                    notes.append({"key": cd["key"], "text": truncate(html_to_text(cd.get("note", "")), 3000)})
                elif cd.get("itemType") == "attachment":
                    files.append({"key": cd["key"], "title": cd.get("title"),
                                  "contentType": cd.get("contentType"), "linkMode": cd.get("linkMode"),
                                  "filename": cd.get("filename") or cd.get("path")})
        out["notes"], out["attachments"] = notes, files
        return out

    async def get_fulltext(self, key: str, offset: int = 0, max_chars: int = 30000) -> dict:
        """Text that Zotero indexed from the item's PDF (or other attachment)."""
        item = await self.z.item(key)
        if item["data"].get("itemType") == "attachment":
            atts = [item]
        else:
            atts = [c for c in await self.z.children(key)
                    if c["data"].get("itemType") == "attachment"]
        atts.sort(key=lambda a: a["data"].get("contentType") != "application/pdf")
        max_chars = max(1000, min(max_chars, 60000))
        for a in atts:
            try:
                ft = await self.z.get_json(f"items/{a['key']}/fulltext")
            except ZoteroError as exc:
                if str(exc).startswith("Not found"):
                    continue
                raise
            text = ft.get("content") or ""
            end = offset + max_chars
            return {
                "key": key,
                "attachment": a["key"],
                "attachment_title": a["data"].get("title"),
                "total_chars": len(text),
                "offset": offset,
                "next_offset": end if end < len(text) else None,
                "indexed_pages": ft.get("indexedPages"),
                "total_pages": ft.get("totalPages"),
                "text": text[offset:end],
            }
        raise ZoteroError(
            "No indexed full text for this item. In Zotero, open the PDF once so WebDAV "
            "downloads it, then right-click the attachment and choose Reindex Item."
            if atts else "This item has no attachment."
        )

    async def list_tags(self, facet: str | None = None, outside_vocabulary: bool = False,
                        include_automatic: bool = False, min_items: int = 1) -> dict:
        items = await self.regular_items()
        vocab = self.vocab.get()
        counts: Counter[tuple[str, int]] = Counter()
        for i in items:
            for t in i["data"].get("tags") or []:
                counts[(t["tag"], ttype(t))] += 1
        rows = []
        for (tag, kind), n in counts.most_common():
            if kind == 1 and not include_automatic:
                continue
            if facet and facet_of(tag) != facet:
                continue
            in_vocab = vocab.allows(tag) if vocab else None
            if outside_vocabulary and in_vocab:
                continue
            if n < min_items:
                continue
            rows.append({"tag": tag, "items": n, "automatic": kind == 1, "in_vocabulary": in_vocab})
        return {"distinct": len(rows), "tags": rows}

    def get_vocabulary(self) -> dict:
        return self.vocab.require().as_dict()

    async def list_collections(self) -> list[dict]:
        cols = await self.z.collections()
        names = {c["key"]: c["data"]["name"] for c in cols}
        return sorted(
            (
                {"key": c["key"], "name": c["data"]["name"],
                 "parent": names.get(c["data"].get("parentCollection") or "", None),
                 "items": (c.get("meta") or {}).get("numItems")}
                for c in cols
            ),
            key=lambda r: ((r["parent"] or ""), r["name"].lower()),
        )

    async def history(self, limit: int = 10) -> list[dict]:
        j = await self.journal()
        rows = []
        for e in reversed(j.entries()[-limit:]):
            rows.append({k: e.get(k) for k in ("id", "op", "summary", "time", "undoes", "undone_by")}
                        | {"items": len(e.get("changes") or []),
                           "items_undone": len(e.get("undone_keys") or []),
                           "fully_undone": Journal.fully_undone(e)})
        return rows

    # ------------------------------------------------------------ write engine

    async def _plan(self, edits: dict[str, Editor]) -> tuple[list[dict], dict[str, str]]:
        found = {i["key"]: i for i in await self.z.items_by_keys(list(edits))}
        plan, skipped = [], {}
        for key, fn in edits.items():
            item = found.get(key)
            if item is None:
                skipped[key] = "not found"
                continue
            data = item["data"]
            try:
                new = fn(data)
            except Skip as exc:
                skipped[key] = str(exc)
                continue
            if not new:
                continue
            changed = {f: v for f, v in new.items() if norm(f, v) != norm(f, data.get(f))}
            if changed:
                plan.append({"key": key, "data": data,
                             "before": {f: data.get(f) for f in changed}, "after": changed})
        return plan, skipped

    @staticmethod
    def _describe(p: dict) -> dict:
        d: dict[str, Any] = {"key": p["key"], "item": label(p["data"])}
        for f, new in p["after"].items():
            old = p["before"].get(f)
            if f == "tags":
                o = {t["tag"] for t in old or []}
                n = {t["tag"] for t in new or []}
                d["added"] = sorted(n - o)
                d["removed"] = sorted(o - n)
                d["tags_after"] = [t["tag"] for t in new if ttype(t) == 0]
            elif f == "collections":
                d["collections"] = {"before": old or [], "after": new}
            else:
                d[f] = {"before": truncate(str(writable(f, old)), 300), "after": truncate(str(new), 300)}
        return d

    async def _run(self, op: str, summary: str, edits: dict[str, Editor], dry_run: bool,
                   undoes: str | None = None) -> dict:
        plan, skipped = await self._plan(edits)
        if dry_run:
            return {
                "dry_run": True,
                "operation": op,
                "would_change": len(plan),
                "changes": [self._describe(p) for p in plan[:PREVIEW_LIMIT]],
                "not_shown": max(0, len(plan) - PREVIEW_LIMIT),
                "skipped": skipped,
                "next": "Nothing was written. Show this preview to the user and call again "
                        "with dry_run=false only after they approve.",
            }
        if not plan:
            return {"applied": 0, "skipped": skipped, "journal_id": None}

        def objects(ps: list[dict]) -> list[dict]:
            return [{"key": p["key"], "version": p["data"]["version"],
                     **{f: writable(f, v) for f, v in p["after"].items()}} for p in ps]

        res = await self.z.update_items(objects(plan))
        done = {p["key"]: p for p in plan if p["key"] in res.succeeded}
        failed = dict(res.failed)
        error, not_sent = res.error, list(res.not_sent)
        # Someone edited these items meanwhile. Tag and collection changes merge with that edit, so
        # they are applied again on the new version; a field change would overwrite it, so it is skipped.
        merging = {"tags", "collections"}
        conflicts = [k for k, (code, _) in failed.items() if code == 412 and k in edits]
        for k in [k for k in conflicts if not set(next(p for p in plan if p["key"] == k)["after"]) <= merging]:
            conflicts.remove(k)
            failed.pop(k, None)
            skipped[k] = "changed in Zotero while waiting for approval; nothing written. Run the change again to see a new preview."
        if conflicts and not error:
            plan2, skipped2 = await self._plan({k: edits[k] for k in conflicts})
            for k in conflicts:
                failed.pop(k, None)
            skipped.update(skipped2)
            if plan2:
                res2 = await self.z.update_items(objects(plan2))
                done.update({p["key"]: p for p in plan2 if p["key"] in res2.succeeded})
                failed.update(res2.failed)
                error, not_sent = res2.error, list(res2.not_sent)
        journal_id = None
        if done:
            j = await self.journal()
            journal_id = j.record(
                op, summary,
                [{"key": k, "item": label(p["data"]), "before": p["before"], "after": p["after"]}
                 for k, p in done.items()],
                undoes=undoes,
            )
        out = {
            "applied": len(done),
            "unchanged": len(res.unchanged),
            "skipped": skipped,
            "failed": {k: f"HTTP {c}: {m}" for k, (c, m) in failed.items()},
            "journal_id": journal_id,
        }
        if error:
            out["error"] = error
            out["not_sent"] = not_sent
            out["status"] = (f"Stopped after {len(done)} items were saved (journaled, undoable). "
                             f"{len(not_sent)} items were not sent. Fix the error, then run the "
                             "same call again: saved items will show as unchanged.")
        if self.z.last_auth_note:
            out["note"] = self.z.last_auth_note
        return out

    # ------------------------------------------------------------ tags

    def _tag_editor(self, add: list[str], remove: list[str], single: list[str],
                    mark: bool, replace: list[str] | None = None,
                    limits: dict[str, int] | None = None, unmark: bool = False) -> Editor:
        """add/remove tags. replace: facets whose manual tags become exactly the added ones.
        limits: most manual tags per facet; a change that would go over is skipped.
        unmark: remove the review marker (the change is the user's own review)."""
        marker = self.s.marker
        replace_set = set(replace or [])

        def fn(data: dict) -> dict:
            tags = [dict(t) for t in data.get("tags") or []]
            before_manual = set(manual(data))
            new_single = {facet_of(t) for t in add if facet_of(t) in single}
            kept = [
                t for t in tags
                if t["tag"] not in remove
                and not (ttype(t) == 0 and facet_of(t["tag"]) in new_single and t["tag"] not in add)
                and not (ttype(t) == 0 and facet_of(t["tag"]) in replace_set and t["tag"] not in add)
                and not (unmark and marker and t["tag"] == marker)
            ]
            names = {t["tag"] for t in kept if ttype(t) == 0}
            new_adds = [t for t in add if t not in before_manual]
            extra = [marker] if (mark and not unmark and marker and new_adds) else []
            for t in [*add, *extra]:
                if t in names:
                    continue
                auto = next((x for x in kept if x["tag"] == t and ttype(x) == 1), None)
                if auto is not None:
                    auto.pop("type", None)  # same name as an automatic tag: make it manual
                else:
                    kept.append({"tag": t})
                names.add(t)
            for facet, most in (limits or {}).items():
                after = [t["tag"] for t in kept if ttype(t) == 0 and facet_of(t["tag"]) == facet]
                before = [t for t in before_manual if facet_of(t) == facet]
                if len(after) > most and len(after) > len(before):
                    raise Skip(f"would have {len(after)} {facet}/ tags (at most {most}): "
                               f"{', '.join(sorted(after))}. Remove some, or use replace.")
            return {"tags": kept}

        return fn

    def _check_tags(self, tags: list[str]) -> list[str]:
        vocab = self.vocab.require()
        aliases = vocab.alias_map()
        errors = []
        for t in tags:
            # Added tags come from the list; of the system tags ("_"), only the review marker.
            if t in vocab.entries or (self.s.marker and t == self.s.marker):
                continue
            hint = aliases.get(t.lower()) or aliases.get(t.split("/", 1)[-1].lower())
            errors.append(f"'{t}' is not in the vocabulary" + (f" (alias of {hint})" if hint else ""))
        return errors

    async def tag_items(self, changes: list[dict], dry_run: bool = True) -> dict:
        vocab = self.vocab.require()
        errors: list[str] = []
        edits: dict[str, Editor] = {}
        for ch in changes:
            key = ch["key"]
            add = [t.strip() for t in ch.get("add") or [] if t.strip()]
            remove = [t.strip() for t in ch.get("remove") or [] if t.strip()]
            replace = [f.strip().rstrip("/") for f in ch.get("replace") or [] if f.strip()]
            errors += [f"{key}: {e}" for e in self._check_tags(add)]
            errors += [f"{key}: replace names an unknown facet '{f}'" for f in replace if f not in vocab.facets]
            per_facet = Counter(facet_of(t) for t in add if facet_of(t) in vocab.single_facets)
            errors += [f"{key}: only one '{f}/' tag is allowed" for f, n in per_facet.items() if n > 1]
            if key in edits:
                errors.append(f"{key}: listed twice")
            edits[key] = self._tag_editor(add, remove, vocab.single_facets, mark=True,
                                          replace=replace, limits=vocab.max_per_facet)
        if errors:
            raise ZoteroError(
                "Nothing was written. Fix these first (add missing tags to the vocabulary file "
                "only with the user's approval):\n" + "\n".join(errors)
            )
        return await self._run("tag_items", f"tag {len(edits)} items", edits, dry_run)

    def _review_errors(self, key: str, tags: list[str]) -> list[str]:
        vocab = self.vocab.require()
        errors = [f"{key}: {e}" for e in self._check_tags(tags)]
        per = Counter(facet_of(t) for t in tags)
        errors += [f"{key}: only one '{f}/' tag is allowed" for f in vocab.single_facets if per[f] > 1]
        errors += [f"{key}: {per[f]} {f}/ tags (at most {m})" for f, m in vocab.max_per_facet.items()
                   if per[f] > m]
        return errors

    async def apply_tag_review(self, path: str, mark_reviewed: bool = True,
                               dry_run: bool = True, again: bool = False) -> dict:
        """Apply a tag review note exactly as the user edited it.

        The note has a Markdown table with a Key column and a "Proposed tags" column.
        For every row, the listed tags become the item's complete set for each facet
        that is not single-valued (topic, method, type); a listed status/ tag replaces
        the status. Rows with an empty cell, or "skip", are left alone. With
        mark_reviewed, the review marker is removed (the rows are the user's review).

        A note that carries an "Applied" line is not applied again unless again=True.
        If the note has a "Current tags" column, items whose tags changed after the note
        was written are listed (the note's rows overwrite those changes)."""
        vocab = self.vocab.require()
        note = self._note_path(path)
        text = note.read_text(encoding="utf-8")
        applied_before = [f"{d} ({n} items)" for d, n in APPLIED_LINE.findall(text)]
        if applied_before and not again and not dry_run:
            raise ZoteroError(f"{note.name} was already applied on {', '.join(applied_before)}. "
                              "Nothing was written. Pass again=true to apply it once more.")
        rows, problems = parse_review_table(text)
        if not rows:
            raise ZoteroError(f"No rows with a Key and a Proposed tags column in {note}."
                              + (" " + "; ".join(problems) if problems else ""))
        replace = [f for f in vocab.facets if f not in vocab.single_facets]
        current = review_column(text, "current tags")
        changed: dict[str, dict] = {}
        errors: list[str] = list(problems)
        edits: dict[str, Editor] = {}

        def watch(key: str, fn: Editor, expected: list[str]) -> Editor:
            def g(data: dict) -> dict:
                now = sorted(t for t in manual(data) if facet_of(t) in replace)
                if expected != now:
                    changed[key] = {"item": label(data), "in_note": expected, "now": now}
                return fn(data)
            return g

        for key, tags in rows:
            errors += self._review_errors(key, tags)
            if key in edits:
                errors.append(f"{key}: listed twice")
            fn = self._tag_editor(tags, [], vocab.single_facets, mark=False,
                                  replace=replace, unmark=mark_reviewed)
            if key in current:
                fn = watch(key, fn, sorted(t for t in split_tags(current[key]) if facet_of(t) in replace))
            edits[key] = fn
        if errors:
            raise ZoteroError("Nothing was written. Fix these rows in the note first:\n" + "\n".join(errors))
        res = await self._run("apply_tag_review", f"{note.name}: {len(edits)} items", edits, dry_run)
        res["note"] = str(note)
        res["rows"] = len(edits)
        res["checks_changes"] = bool(current)
        if applied_before:
            res["already_applied"] = applied_before
            if dry_run and not again:
                res["next"] = (f"{note.name} was already applied ({', '.join(applied_before)}). "
                               "Apply it again only if the user asks, with again=true.")
        if changed:
            res["changed_since_note"] = {"count": len(changed), "items": dict(list(changed.items())[:20]),
                                         "note": "These items' tags changed after the note was written. "
                                                 "Applying the note overwrites those changes."}
        if not dry_run and res.get("applied"):
            stamp = f"\nApplied {dt.date.today().isoformat()}: {res['applied']} items (journal {res['journal_id']}).\n"
            with note.open("a", encoding="utf-8") as fh:
                fh.write(stamp)
        return res

    async def write_tag_review(self, path: str, rows: list[dict], intro: str = "",
                               proposed_by: str = "Sub-Sub librarian") -> dict:
        """Write a tag review note for the user: one row per item with its current tags, the
        proposed complete set of topic/, method/ and type/ tags, and a reason. Every row is
        checked against the vocabulary first. The note is new; an existing file is never
        overwritten. Relative paths are in the vault; a bare name goes to Inbox/."""
        vocab = self.vocab.require()
        if self.s.vault is None:
            raise ZoteroError("write_tag_review needs the vault (ZOTERO_VAULT).")
        vault = self.s.vault.resolve()
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = vault / ("Inbox" if len(p.parts) == 1 else "") / p
        if p.suffix != ".md":
            p = p.with_name(p.name + ".md")
        p = p.resolve()
        if vault not in p.parents:
            raise ZoteroError(f"{p} is outside the vault ({vault}).")
        if p.exists():
            raise ZoteroError(f"{p.relative_to(vault)} already exists. Choose a new name; notes are never overwritten.")
        if not rows:
            raise ZoteroError("No rows.")
        errors: list[str] = []
        seen: set[str] = set()
        clean: list[tuple[str, list[str], str]] = []
        for r in rows:
            key = str(r.get("key", "")).strip()
            tags = r.get("tags") or []
            tags = split_tags(tags) if isinstance(tags, str) else [str(t).strip() for t in tags if str(t).strip()]
            if not REVIEW_KEY.match(key):
                errors.append(f"'{key}' is not a Zotero item key")
                continue
            if key in seen:
                errors.append(f"{key}: listed twice")
            seen.add(key)
            if not tags:
                errors.append(f"{key}: no proposed tags")
            if not any(facet_of(t) == "topic" for t in tags) and "topic" in vocab.facets:
                errors.append(f"{key}: no topic/ tag")
            errors += self._review_errors(key, tags)
            clean.append((key, tags, str(r.get("reason") or "")))
        found = {i["key"]: i["data"] for i in await self.z.items_by_keys([k for k, _, _ in clean])}
        errors += [f"{k}: not in the library" for k, _, _ in clean if k not in found]
        if errors:
            raise ZoteroError("Nothing was written. Fix these rows first:\n" + "\n".join(errors))
        marker = self.s.marker
        lines = []
        for key, tags, reason in clean:
            d = found[key]
            title = truncate(d.get("title") or "", 90)
            item = f"{first_creator_name(d)} {year_of(d)}, {title}".strip()
            cur = [t for t in manual(d) if t != marker and facet_of(t) not in vocab.single_facets]
            lines.append("| " + " | ".join([key, current_key(d) or "", review_cell(item), review_cell(", ".join(cur)),
                                              ", ".join(tags), review_cell(reason)]) + " |")
        name = p.stem
        text = (
            f"---\ncreated: {dt.date.today().isoformat()}\nproposed-by: {proposed_by}\n"
            f"items: {len(clean)}\n---\n\n# {name}\n\n"
            + (intro.strip() + "\n\n" if intro.strip() else "")
            + "For each row, Proposed tags becomes the item's complete set of topic/, method/ and type/ "
              "tags. Status tags and tags without a facet stay.\n\n"
              "Edit the Proposed tags column. Write skip to leave an item as it is. Then, in Sub-Sub "
              f"librarian mode: apply the tag review {p.relative_to(vault).as_posix()}. Applying also removes "
              "the review marker from these items, because this note is your review.\n\n"
              "| Key | Citekey | Item | Current tags | Proposed tags | Reason |\n|---|---|---|---|---|---|\n"
            + "\n".join(lines) + "\n"
        )
        parsed, problems = parse_review_table(text)
        if problems or [k for k, _ in parsed] != [k for k, _, _ in clean]:
            raise ZoteroError("The note would not read back correctly: " + "; ".join(problems))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return {"path": p.relative_to(vault).as_posix(), "rows": len(clean),
                "next": "Ask the user to review the note. Apply it with apply_tag_review only when they say so."}

    def _note_path(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute() and self.s.vault is not None:
            p = self.s.vault / p
        if p.suffix != ".md":
            p = p.with_suffix(p.suffix + ".md") if p.suffix else p.with_suffix(".md")
        # Review notes live in the vault; a path elsewhere is refused (the note gets a line appended).
        if self.s.vault is not None and not p.resolve().is_relative_to(self.s.vault.resolve()):
            raise ZoteroError(f"{p} is outside the vault ({self.s.vault}).")
        if not p.exists():
            raise ZoteroError(f"No note at {p}.")
        return p

    async def tag_audit(self, limit: int = 50) -> dict:
        """Items whose tags break the vocabulary rules or look batch-applied."""
        vocab = self.vocab.require()
        items = await self.regular_items()
        by_key = {i["key"]: i for i in items}
        tagsets = {i["key"]: set(manual(i["data"])) for i in items}
        over: dict[str, list[str]] = {}
        for k, tags in tagsets.items():
            for f, most in vocab.max_per_facet.items():
                n = sum(1 for t in tags if facet_of(t) == f)
                if n > most:
                    over.setdefault(k, []).append(f"{n} {f}/ tags (at most {most})")
            for f in vocab.single_facets:
                n = sum(1 for t in tags if facet_of(t) == f)
                if n > 1:
                    over.setdefault(k, []).append(f"{n} {f}/ tags (only one allowed)")
        # Pairs of tags that almost always come together: a sign of tags applied in batches.
        count = Counter(t for tags in tagsets.values() for t in tags if vocab.allows(t) and not t.startswith("_"))
        pair_count: Counter = Counter()
        for tags in tagsets.values():
            scored = sorted(t for t in tags if t in count and facet_of(t) not in vocab.single_facets)
            for a_i, a in enumerate(scored):
                for b in scored[a_i + 1:]:
                    pair_count[(a, b)] += 1
        sticky = []
        for (a, b), n in pair_count.items():
            union = count[a] + count[b] - n
            if n >= 10 and union and n / union >= 0.8:
                sticky.append({"tags": [a, b], "together": n, "jaccard": round(n / union, 2)})
        sticky.sort(key=lambda x: -x["together"])
        marker = self.s.marker
        unmarked = [k for k, tags in tagsets.items()
                    if tags and marker not in tags and any(facet_of(t) not in vocab.single_facets
                                                           for t in tags if facet_of(t))]

        def rows(keys: list[str]) -> list[dict]:
            return [{"key": k, "item": label(by_key[k]["data"]),
                     "tags": sorted(t for t in tagsets[k] if not t.startswith("_"))} for k in keys[:limit]]

        return {
            "items": len(items),
            "over_limit": {"count": len(over), "items": [r | {"problems": over[r["key"]]} for r in rows(sorted(over))]},
            "sticky_pairs": sticky[:20],
            "without_review_marker": {"count": len(unmarked), "items": rows(sorted(unmarked))},
            "note": "Sticky pairs appear together on at least 80% of their items: check whether "
                    "they were applied in batches. Items without the review marker were not "
                    "proposed by an agent (reviewed by the user, or tags from before the vocabulary).",
        }

    async def rename_tags(self, mapping: dict[str, str], dry_run: bool = True) -> dict:
        errors = self._check_tags(sorted(set(mapping.values())))
        if errors:
            raise ZoteroError("Nothing was written. Targets must be in the vocabulary:\n" + "\n".join(errors))
        sources = set(mapping)
        edits: dict[str, Editor] = {}
        for i in await self.regular_items():
            present = {t["tag"] for t in i["data"].get("tags") or []} & sources
            if not present:
                continue
            targets = sorted({mapping[s] for s in present})
            edits[i["key"]] = self._tag_editor(targets, sorted(present), [], mark=False)
        summary = "; ".join(f"{s} -> {t}" for s, t in sorted(mapping.items()))
        return await self._run("rename_tags", truncate(summary, 500), edits, dry_run)

    async def remove_tags(self, tags: list[str], dry_run: bool = True) -> dict:
        drop = set(tags)
        edits = {
            i["key"]: self._tag_editor([], sorted(drop), [], mark=False)
            for i in await self.regular_items()
            if {t["tag"] for t in i["data"].get("tags") or []} & drop
        }
        return await self._run("remove_tags", truncate(", ".join(sorted(drop)), 500), edits, dry_run)

    async def remove_automatic_tags(self, keys: list[str] | None = None, dry_run: bool = True) -> dict:
        if keys:
            targets = keys
        else:
            targets = [i["key"] for i in await self.regular_items() if automatic(i["data"])]

        def fn(data: dict) -> dict:
            return {"tags": [t for t in data.get("tags") or [] if ttype(t) == 0]}

        return await self._run("remove_automatic_tags", f"{len(targets)} items",
                               {k: fn for k in targets}, dry_run)

    # ------------------------------------------------------------ citekeys and fields

    async def set_citekeys(self, keys: list[str] | None = None, force: bool = False,
                           dry_run: bool = True) -> dict:
        items = await self.regular_items()
        by_key = {i["key"]: i for i in items}
        holders = Counter(k for i in items if (k := current_key(i["data"])))
        taken = set(holders)
        if keys:
            unknown = [k for k in keys if k not in by_key]
            if unknown:
                raise ZoteroError(f"Unknown or non-regular items: {', '.join(unknown)}")
            targets = [by_key[k] for k in keys]
        else:
            targets = [i for i in items if not current_key(i["data"])]
        targets.sort(key=lambda i: i["data"].get("dateAdded", ""))
        edits: dict[str, Editor] = {}
        pinned = 0
        for i in targets:
            old = current_key(i["data"])
            if old and not force:
                pinned += 1
                continue
            if old:
                holders[old] -= 1
                if holders[old] <= 0:
                    taken.discard(old)  # free the key only if no other item holds it
            new = unique_key(base_key(i["data"]), taken)
            taken.add(new)
            edits[i["key"]] = lambda data, new=new: with_key(data, new)
        out = await self._run("set_citekeys", f"{len(edits)} items", edits, dry_run)
        out["kept_existing_keys"] = pinned
        return out

    async def update_fields(self, key: str, fields: dict[str, Any], dry_run: bool = True) -> dict:
        bad = sorted(set(fields) & PROTECTED_FIELDS)
        if bad:
            raise ZoteroError(
                f"Use the dedicated tools for: {', '.join(bad)} (tags, collections, trash, citekeys)."
            )
        item = await self.z.item(key)
        unknown = sorted(f for f in fields if f not in item["data"])
        if unknown:
            valid = sorted(f for f in item["data"] if f not in PROTECTED_FIELDS)
            raise ZoteroError(
                f"Fields not valid for this {item['data'].get('itemType')}: {', '.join(unknown)}. "
                f"Valid: {', '.join(valid)}"
            )
        return await self._run("update_fields", f"{key}: {', '.join(fields)}",
                               {key: lambda data: dict(fields)}, dry_run)

    # ------------------------------------------------------------ collections, notes, trash

    async def file_items(self, keys: list[str], collection_key: str, remove: bool = False,
                         dry_run: bool = True) -> dict:
        names = {c["key"]: c["data"]["name"] for c in await self.z.collections()}
        if collection_key not in names:
            raise ZoteroError(f"No collection {collection_key}. Use list_collections.")

        def fn(data: dict) -> dict:
            cols = list(data.get("collections") or [])
            if remove:
                cols = [c for c in cols if c != collection_key]
            elif collection_key not in cols:
                cols.append(collection_key)
            return {"collections": cols}

        verb = "remove from" if remove else "add to"
        return await self._run("file_items", f"{verb} {names[collection_key]}: {len(keys)} items",
                               {k: fn for k in keys}, dry_run)

    async def create_collection(self, name: str, parent_key: str | None = None,
                                dry_run: bool = True) -> dict:
        if parent_key:
            names = {c["key"]: c["data"]["name"] for c in await self.z.collections()}
            if parent_key not in names:
                raise ZoteroError(f"No parent collection {parent_key}.")
        if dry_run:
            return {"dry_run": True, "would_create": {"name": name, "parent": parent_key},
                    "next": "Nothing was written. Call again with dry_run=false after approval."}
        new_key = await self.z.create_collection(name, parent_key)
        jid = (await self.journal()).record(
            "create_collection", f"collection {name}",
            [{"key": new_key, "collection": True, "item": f"collection {name}",
              "before": {"deleted": True}, "after": {"deleted": False}}])
        return {"created": new_key, "name": name, "journal_id": jid}

    async def create_note(self, parent_key: str, markdown_text: str, dry_run: bool = True) -> dict:
        parent = await self.z.item(parent_key)
        if not regular(parent):
            raise ZoteroError("Notes can only be attached to regular items.")
        # Raw HTML in the text stays text (as in attach_note): the note shows what the preview shows.
        body = md.markdown(html.escape(markdown_text, quote=False), extensions=["extra", "sane_lists"])
        if dry_run:
            return {"dry_run": True, "parent": label(parent["data"]),
                    "note_preview": markdown_text,
                    "next": "Nothing was written. Call again with dry_run=false after approval."}
        tags = [{"tag": self.s.marker}] if self.s.marker else []
        res = await self.z.create_items([{
            "itemType": "note", "parentItem": parent_key, "note": f"<div>{body}</div>",
            "tags": tags, "collections": [], "relations": {},
        }])
        if not res.created:
            raise ZoteroError(f"Creating the note failed: {res.failed}")
        note_key = res.created[0]
        j = await self.journal()
        jid = j.record("create_note", f"note on {parent_key}",
                       [{"key": note_key, "item": f"note on {label(parent['data'])}",
                         "before": {"deleted": True}, "after": {"deleted": False}}])
        return {"created": note_key, "parent": parent_key, "journal_id": jid}

    async def trash_items(self, keys: list[str], dry_run: bool = True) -> dict:
        def fn(data: dict) -> dict:
            if data.get("deleted"):
                raise Skip("already in the trash")
            return {"deleted": True}

        return await self._run("trash_items", f"trash {len(keys)} items", {k: fn for k in keys}, dry_run)

    # ------------------------------------------------------------ undo

    async def undo(self, journal_id: str | None = None, dry_run: bool = True) -> dict:
        j = await self.journal()
        entry = j.load(journal_id) if journal_id else j.last_undoable()
        if entry is None:
            raise ZoteroError("Nothing to undo.")
        done_keys = set(entry.get("undone_keys") or [])
        pending = [ch for ch in entry["changes"] if ch["key"] not in done_keys]
        if not pending:
            raise ZoteroError(f"{entry['id']} was already undone completely.")
        undoing = {"id": entry["id"], "op": entry["op"], "summary": entry["summary"]}
        # A new collection is undone by moving it to the trash (items in it stay in the library);
        # undoing that undo brings it back.
        cols = [ch for ch in pending if ch.get("collection")]
        if cols:
            if dry_run:
                return {"dry_run": True, "operation": "undo", "would_change": len(cols),
                        "changes": [{"key": ch["key"], "item": ch["item"],
                                     "deleted": {"before": str(ch["after"]["deleted"]), "after": str(ch["before"]["deleted"])}}
                                    for ch in cols],
                        "not_shown": 0, "skipped": {}, "undoing": undoing,
                        "next": "Nothing was written. Show this preview to the user and call again "
                                "with dry_run=false only after they approve."}
            for ch in cols:
                await self.z.set_collection_deleted(ch["key"], bool(ch["before"]["deleted"]))
            jid = j.record("undo", f"undo {entry['id']} ({entry['op']})",
                           [{**ch, "before": ch["after"], "after": ch["before"]} for ch in cols], undoes=entry["id"])
            return {"applied": len(cols), "skipped": {}, "failed": {}, "journal_id": jid, "undoing": undoing}
        edits: dict[str, Editor] = {}
        for ch in pending:
            def fn(data: dict, after=ch["after"], before=ch["before"]) -> dict:
                for f, v in after.items():
                    if norm(f, data.get(f)) != norm(f, v):
                        raise Skip(f"'{f}' was changed after this operation; left as is")
                return dict(before)

            edits[ch["key"]] = fn
        out = await self._run("undo", f"undo {entry['id']} ({entry['op']})", edits, dry_run,
                              undoes=entry["id"])
        out["undoing"] = undoing
        return out
