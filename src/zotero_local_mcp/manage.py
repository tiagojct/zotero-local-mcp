"""Librarian operations that combine the library with outside metadata services:
imports, metadata audit and repair, duplicates, retractions and open-access PDFs.

All writes go through the same guards as library.py: dry run first, vocabulary
check for tags, version-checked writes, journal entries that undo can revert.
Imported items never receive keywords or MeSH headings as tags.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .citekey import base_key, current_key, first_creator_name, slug, unique_key, with_key, year_of
from .client import ZoteroError
from .external import External, ExternalError
from .library import Library, label, manual, regular
from .records import (
    DOI_RE, LibraryIndex, norm_doi, detect, extra_ids, fingerprint, item_ids, label_of, norm_isbn,
    record_to_zotero, title_similarity, zotero_type,
)

CHECK_LINE = re.compile(r"^(\s*[-*]\s+\[)([xX])(\]\s+)(.*)$")
FILL_FIELDS = (
    "DOI", "abstractNote", "publicationTitle", "bookTitle", "proceedingsTitle", "repository",
    "journalAbbreviation", "volume", "issue", "pages", "date", "ISSN", "ISBN", "publisher",
    "place", "url", "language",
)
SERIOUS = {"retraction", "withdrawal", "removal", "expression_of_concern", "expression-of-concern",
           "partial_retraction"}
CACHE_DAYS = 30


TOKEN = re.compile(r"\b(doi|pmid|isbn):\s*(\S+)", re.I)


def ids_in_line(text: str) -> str | None:
    """Identifier of a queue or alert line: the leading token (queue lines), otherwise the
    last doi:, pmid: or isbn: token (alert lines end with their identifiers)."""
    def fmt(kind: str, value: str) -> str:
        value = value.rstrip(".,;|")
        return value if kind.lower() == "doi" else f"{kind.lower()}:{value}"

    lead = re.match(r"^\s*(doi|pmid|isbn):\s*(\S+)", text, re.I)
    if lead:
        return fmt(lead.group(1), lead.group(2))
    tokens = TOKEN.findall(text)
    for kind in ("doi", "pmid", "isbn"):
        found = [v for k, v in tokens if k.lower() == kind]
        if found:
            return fmt(kind, found[-1])
    m = DOI_RE.search(text)
    return m.group(1).rstrip(".,;)|") if m else None


class Librarian:
    def __init__(self, lib: Library, ext: External) -> None:
        self.lib = lib
        self.z = lib.z
        self.ext = ext

    # ------------------------------------------------------------ records

    async def record_for(self, kind: str, value: str) -> dict | None:
        if kind == "pmid":
            recs = await self.ext.pubmed_fetch([value])
            return recs[0] if recs else None
        if kind == "isbn":
            return await self.ext.openlibrary_isbn(value)
        rec = await self.ext.crossref_work(value)
        if rec is None or rec.get("kind") in ("journal-article", "") or not rec.get("abstract"):
            try:
                pm = await self.ext.pubmed_for_doi(value)
            except ExternalError:
                pm = None
            if pm and norm_doi(pm.get("doi")) != value:
                pm = None  # PubMed returned a different article
            if rec is None:
                return pm
            if pm:
                rec["abstract"] = rec.get("abstract") or pm.get("abstract")
                rec["pmid"], rec["pmcid"] = pm.get("pmid"), pm.get("pmcid")
        return rec

    async def _template(self, ztype: str) -> dict:
        try:
            return await self.z.template(ztype)
        except ZoteroError:
            return await self.z.template("document")

    # ------------------------------------------------------------ import

    async def import_identifiers(self, identifiers: list[str], collection_key: str | None = None,
                                 tags: list[str] | None = None, dry_run: bool = True) -> dict:
        tags = [t.strip() for t in tags or [] if t.strip()]
        if tags:
            errs = self.lib._check_tags(tags)
            if errs:
                raise ZoteroError("Nothing was imported. " + "; ".join(errs))
        if collection_key:
            names = {c["key"]: c["data"]["name"] for c in await self.z.collections()}
            if collection_key not in names:
                raise ZoteroError(f"No collection {collection_key}. Use list_collections.")
        index = LibraryIndex(await self.lib.regular_items())
        taken = set(index.by_citekey)
        skipped: dict[str, str] = {}
        errors: dict[str, str] = {}
        todo: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()
        for ident in identifiers:
            det = detect(ident)
            if det is None:
                errors[ident] = "not a DOI, PMID or ISBN"
                continue
            if det in seen:
                skipped[ident] = "listed twice"
                continue
            seen.add(det)
            kind, value = det
            hit = index.lookup(**{kind: value})
            if hit:
                skipped[ident] = f"already in library: {hit[0]} ({current_key(index.data[hit[0]]) or 'no citekey'})"
                continue
            todo.append((ident, kind, value))

        async def fetch(kind: str, value: str):
            try:
                return await self.record_for(kind, value)
            except ExternalError as exc:
                return exc

        records = await asyncio.gather(*(fetch(k, v) for _, k, v in todo))
        plan: list[dict] = []
        marker = self.lib.s.marker
        for (ident, kind, value), rec in zip(todo, records):
            if isinstance(rec, Exception):
                errors[ident] = str(rec)
                continue
            if rec is None:
                errors[ident] = "not found in Crossref, PubMed or Open Library"
                continue
            match = index.match_record(rec)
            if match and match["key"].startswith("NEW"):
                skipped[ident] = "same work as another identifier in this list"
                continue
            if match:
                skipped[ident] = f"already in library: {match['key']} (matched by {match['matched_by']})"
                continue
            item = record_to_zotero(rec, await self._template(zotero_type(rec)))
            ck = unique_key(base_key(item), taken)
            taken.add(ck)
            item.update(with_key(item, ck))
            item["tags"] = [{"tag": t} for t in ([marker] if marker else []) + tags]
            item["collections"] = [collection_key] if collection_key else []
            index.add(f"NEW{len(plan)}", item)
            plan.append({"identifier": ident, "item": item, "record": rec, "citekey": ck})
        if dry_run:
            return {
                "dry_run": True,
                "would_import": [
                    {"identifier": p["identifier"], "item": label_of(p["record"]),
                     "type": p["item"]["itemType"], "citekey": p["citekey"],
                     "source": p["record"].get("source"), "doi": p["record"].get("doi"),
                     "pmid": p["record"].get("pmid"), "has_abstract": bool(p["record"].get("abstract"))}
                    for p in plan
                ],
                "skipped": skipped,
                "errors": errors,
                "next": "Nothing was written. Show this to the user; call again with dry_run=false after approval.",
            }
        if not plan:
            return {"imported": [], "skipped": skipped, "errors": errors, "journal_id": None}
        res = await self.z.create_items([p["item"] for p in plan])
        imported = []
        for pos, key in sorted(res.created_at.items()):
            p = plan[pos]
            imported.append({"identifier": p["identifier"], "key": key, "citekey": p["citekey"],
                             "item": label_of(p["record"])})
        journal_id = None
        if imported:
            j = await self.lib.journal()
            journal_id = j.record("import", f"import {len(imported)} items",
                                  [{"key": i["key"], "item": i["item"], "before": {"deleted": True},
                                    "after": {"deleted": False}} for i in imported])
        out: dict[str, Any] = {"imported": imported, "skipped": skipped, "errors": errors,
                               "failed": {k: f"HTTP {c}: {m}" for k, (c, m) in res.failed.items()},
                               "journal_id": journal_id}
        if res.error:
            out["error"] = res.error
            out["not_sent"] = [plan[int(n)]["identifier"] for n in res.not_sent]
        return out

    async def import_queue(self, path: str | None = None, collection_key: str | None = None,
                           tags: list[str] | None = None, dry_run: bool = True) -> dict:
        qpath = Path(path).expanduser() if path else self.lib.s.queue_path
        if qpath is None or not qpath.exists():
            raise ZoteroError(f"No queue file at {qpath}.")
        lines = qpath.read_text(encoding="utf-8").splitlines()
        wanted: dict[str, int] = {}
        problems: dict[str, str] = {}
        for n, line in enumerate(lines):
            m = CHECK_LINE.match(line)
            if not m or re.search(r"\((imported|already in library)", line):
                continue
            ident = ids_in_line(m.group(4))
            if ident:
                wanted.setdefault(ident, n)
            else:
                problems[f"line {n + 1}"] = "ticked, but no doi:, pmid: or isbn: found"
        if not wanted:
            return {"queue": str(qpath), "ticked": 0, "note": "No ticked lines to import.", "problems": problems}
        mtime = qpath.stat().st_mtime
        res = await self.import_identifiers(list(wanted), collection_key, tags, dry_run)
        res["queue"] = str(qpath)
        res["problems"] = problems
        if dry_run:
            return res
        if qpath.stat().st_mtime != mtime:
            res["note"] = "The queue file changed during the import; it was not updated."
            return res
        for rec in res.get("imported", []):
            n = wanted[rec["identifier"]]
            lines[n] += f" (imported: {rec['citekey']})"
        for ident, why in res.get("skipped", {}).items():
            if ident in wanted and why.startswith("already in library"):
                lines[wanted[ident]] += " (already in library)"
        qpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return res

    # ------------------------------------------------------------ audit and repair

    @staticmethod
    def problems(d: dict) -> list[str]:
        out = []
        t = d.get("itemType")
        title = d.get("title") or ""
        if not title:
            out.append("no title")
        elif len(title) > 20 and title.upper() == title:
            out.append("title in capitals")
        if not d.get("creators"):
            out.append("no authors")
        if not year_of(d):
            out.append("no year")
        ids = item_ids(d)
        raw_doi = d.get("DOI") or extra_ids(d.get("extra")).get("DOI")
        if raw_doi and not ids["doi"]:
            out.append("malformed DOI")
        if t == "journalArticle":
            if not ids["doi"]:
                out.append("no DOI")
            if not d.get("abstractNote"):
                out.append("no abstract")
            if not d.get("publicationTitle"):
                out.append("no journal")
            if not (d.get("volume") or d.get("pages")):
                out.append("no volume or pages")
            m = re.fullmatch(r"\s*(\d+)\s*[-–]\s*(\d+)\s*", d.get("pages") or "")
            if m and 0 <= int(m.group(2)) - int(m.group(1)) <= 1:
                out.append("1 or 2 pages (letter, editorial or book review?)")
        elif t == "book":
            if not ids["isbns"]:
                out.append("no ISBN")
            if not d.get("publisher"):
                out.append("no publisher")
        elif t == "bookSection" and not d.get("bookTitle"):
            out.append("no book title")
        return out

    async def audit(self, item_type: str | None = None, problem: str | None = None,
                    limit: int = 50, offset: int = 0) -> dict:
        items = await self.lib.regular_items()
        if item_type:
            items = [i for i in items if i["data"].get("itemType") == item_type]
        counts: Counter[str] = Counter()
        rows = []
        for it in items:
            d = it["data"]
            probs = self.problems(d)
            counts.update(probs)
            if probs and (not problem or problem in probs):
                rows.append({"key": d["key"], "item": label(d), "type": d.get("itemType"), "problems": probs,
                             "has_doi": bool(item_ids(d)["doi"])})
        rows.sort(key=lambda r: -len(r["problems"]))
        limit = max(1, min(limit, 200))
        return {"items_checked": len(items),
                "problem_counts": dict(counts.most_common()), "total": len(rows), "offset": offset,
                "items": rows[offset : offset + limit]}

    async def _match(self, d: dict) -> tuple[dict | None, float, str]:
        ids = item_ids(d)
        if ids["doi"]:
            return await self.record_for("doi", ids["doi"]), 1.0, "DOI"
        if ids["pmid"]:
            return await self.record_for("pmid", ids["pmid"]), 1.0, "PMID"
        if ids["isbns"]:
            return await self.record_for("isbn", sorted(ids["isbns"])[0]), 1.0, "ISBN"
        if d.get("itemType") in ("journalArticle", "conferencePaper", "bookSection", "preprint") and d.get("title"):
            q = " ".join(x for x in (d.get("title"), first_creator_name(d), year_of(d)) if x)
            best, score = None, 0.0
            for cand in await self.ext.crossref_search(q, rows=3):
                sim = title_similarity(d.get("title"), cand.get("title"))
                y, cy = year_of(d), cand.get("year")
                year_ok = not y or not cy or abs(int(y) - int(cy)) <= 1
                fam = next((a.get("family") or a.get("name") for a in cand.get("authors") or []), "")
                author_ok = not first_creator_name(d) or slug(first_creator_name(d)) == slug(fam or "")
                s = sim * (1.0 if year_ok else 0.5) * (1.0 if author_ok else 0.7)
                if s > score:
                    best, score = cand, s
            if best and best.get("doi") and best.get("kind") == "journal-article":
                # Pull the PubMed abstract too, as for a DOI import.
                full = await self.record_for("doi", best["doi"])
                best = full or best
            return best, round(score, 3), "title search"
        return None, 0.0, "no identifier or title to search"

    async def repair(self, keys: list[str], overwrite: bool = False, min_confidence: float = 0.9,
                     dry_run: bool = True) -> dict:
        items = {i["key"]: i["data"] for i in await self.z.items_by_keys(keys)}

        async def one(key: str):
            try:
                return key, await self._match(items[key])
            except ExternalError as exc:
                return key, exc

        matches: dict[str, dict] = {}
        records: dict[str, dict] = {}
        skipped: dict[str, str] = {}
        for key, res in await asyncio.gather(*(one(k) for k in keys if k in items)):
            if isinstance(res, Exception):
                skipped[key] = str(res)
                continue
            rec, conf, how = res
            if rec is None:
                skipped[key] = f"no match ({how})"
                continue
            matches[key] = {"matched_by": how, "confidence": conf, "match": label_of(rec),
                            "doi": rec.get("doi")}
            if conf < min_confidence:
                skipped[key] = f"best match below {min_confidence} ({conf}): {label_of(rec)}"
                continue
            records[key] = rec
        for k in keys:
            if k not in items:
                skipped[k] = "not found"

        def editor_for(rec: dict):
            def fn(data: dict) -> dict:
                shape = {f: "" for f in data} | {"itemType": data["itemType"], "creators": []}
                cand = record_to_zotero(rec, shape)
                changes: dict[str, Any] = {}
                for f in FILL_FIELDS:
                    new, cur = cand.get(f), data.get(f)
                    if f not in data or not new:
                        continue
                    if not cur or (overwrite and cur != new):
                        changes[f] = new
                if not data.get("creators") and cand.get("creators"):
                    changes["creators"] = cand["creators"]
                have = extra_ids(data.get("extra"))
                add = [ln for ln in (cand.get("extra") or "").splitlines()
                       if ln.split(":", 1)[0].strip().upper() not in have]
                if add:
                    changes["extra"] = "\n".join(x for x in [data.get("extra") or "", *add] if x)
                return changes
            return fn

        out = await self.lib._run("repair_metadata", f"repair {len(records)} items",
                                  {k: editor_for(r) for k, r in records.items()}, dry_run)
        out["matches"] = matches
        out.setdefault("skipped", {}).update(skipped)
        return out

    # ------------------------------------------------------------ duplicates

    async def duplicates(self) -> dict:
        items = await self.lib.regular_items()
        parent = {i["key"]: i["key"] for i in items}
        reasons: dict[str, set] = defaultdict(set)

        def find(k: str) -> str:
            while parent[k] != k:
                parent[k] = parent[parent[k]]
                k = parent[k]
            return k

        buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
        for it in items:
            d = it["data"]
            ids = item_ids(d)
            if ids["doi"]:
                buckets[("DOI", ids["doi"])].append(d["key"])
            if ids["pmid"]:
                buckets[("PMID", ids["pmid"])].append(d["key"])
            for i in ids["isbns"]:
                if d.get("itemType") == "book":
                    buckets[("ISBN", i)].append(d["key"])
            fp = fingerprint(d.get("title"), year_of(d))
            author = slug(first_creator_name(d))
            if fp and author:
                buckets[("title, year and first author", f"{fp}|{author}")].append(d["key"])
        # A title cut off during import: one title is the start of the other, same first
        # author and year. Zotero's Duplicate Items view does not find these.
        by_author_year: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for it in items:
            d = it["data"]
            t, a, y = slug(d.get("title") or ""), slug(first_creator_name(d)), year_of(d)
            if len(t) >= 20 and a and y:
                by_author_year[(a, y)].append((t, d["key"]))
        doi_of = {it["key"]: item_ids(it["data"])["doi"] for it in items}
        for group in by_author_year.values():
            group.sort()
            for i, (t, k) in enumerate(group):
                for t2, k2 in group[i + 1:]:
                    if doi_of[k] and doi_of[k2] and doi_of[k] != doi_of[k2]:
                        continue  # two DOIs: two works
                    if t2.startswith(t) and t2 != t:
                        buckets[("title cut off (same start, first author and year)", f"{k}|{k2}")] += [k, k2]
        for (why, _), keys in buckets.items():
            if len(keys) < 2:
                continue
            for k in keys[1:]:
                parent[find(k)] = find(keys[0])
            for k in keys:
                reasons[k].add(why)
        groups: dict[str, list[str]] = defaultdict(list)
        for k in parent:
            if reasons.get(k):
                groups[find(k)].append(k)
        by_key = {i["key"]: i for i in items}

        def evidence(k: str) -> dict:
            d = by_key[k]["data"]
            ids = item_ids(d)
            return {"key": k, "item": label(d), "type": d.get("itemType"),
                    "citekey": current_key(d), "added": d.get("dateAdded", "")[:10],
                    "doi": ids["doi"] or "", "abstract": bool(d.get("abstractNote")),
                    "children": (by_key[k].get("meta") or {}).get("numChildren"),
                    "filled_fields": sum(1 for f, v in d.items() if v not in ("", [], {}, None)
                                         and f not in ("key", "version", "tags", "relations", "collections")),
                    "tags": manual(d)}

        out = []
        for keys in groups.values():
            if len(keys) < 2:
                continue
            rows = [evidence(k) for k in sorted(keys, key=lambda k: by_key[k]["data"].get("dateAdded", ""))]
            fullest = max(rows, key=lambda r: (bool(r["doi"]), r["abstract"], r["children"] or 0, r["filled_fields"]))
            g = {"matched_by": sorted(set().union(*(reasons[k] for k in keys))), "items": rows,
                 "fullest_record": fullest["key"]}
            if len({r["type"] for r in rows}) > 1:
                g["types_differ"] = ("Zotero's Duplicate Items view shows only items of the same type, so it "
                                     "does not list this group. To merge: change the item type of the "
                                     "incomplete item to match, then merge in Duplicate Items.")
            keys_used = {r["citekey"] for r in rows if r["citekey"]}
            if len(keys_used) > 1:
                g["citekeys"] = (f"The items have different citekeys ({', '.join(sorted(keys_used))}). "
                                 "Check which one your notes and manuscripts cite before merging.")
            out.append(g)
        return {"groups": len(out), "duplicates": out,
                "how_to_merge": "Merging is done by Tiago in Zotero (Duplicate Items, select the group, Merge). "
                                "Say what the evidence shows (identifiers, fields, files); do not call "
                                "items duplicates on a similar title alone."}

    # ------------------------------------------------------------ retractions

    def _cache_path(self) -> Path:
        return self.lib.s.state_dir / "crossref-updates.json"

    async def retractions(self, keys: list[str] | None = None, refresh: bool = False,
                          limit: int = 200) -> dict:
        items = await self.lib.regular_items()
        if keys:
            items = [i for i in items if i["key"] in set(keys)]
        with_doi = [(i["data"], d) for i in items if (d := item_ids(i["data"])["doi"])]
        path = self._cache_path()
        try:
            cache = json.loads(path.read_text())
        except (OSError, ValueError):
            cache = {}
        now = time.time()
        stale = [(data, doi) for data, doi in with_doi
                 if refresh or doi not in cache or now - cache[doi]["checked"] > CACHE_DAYS * 86400]
        batch = stale[: max(1, limit)]

        async def one(doi: str):
            try:
                return doi, await self.ext.crossref_updates(doi)
            except ExternalError as exc:
                return doi, exc

        errors = {}
        for doi, res in await asyncio.gather(*(one(doi) for _, doi in batch)):
            if isinstance(res, Exception):
                errors[doi] = str(res)
                continue
            cache[doi] = {"checked": now, "notices": res}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache))
        serious, other = [], []
        for data, doi in with_doi:
            notices = (cache.get(doi) or {}).get("notices") or []
            if not notices:
                continue
            row = {"key": data["key"], "item": label(data), "doi": doi, "notices": notices,
                   "tags": manual(data)}
            (serious if any((n.get("type") or "").lower() in SERIOUS for n in notices) else other).append(row)
        return {
            "items_with_doi": len(with_doi),
            "items_without_doi": len(items) - len(with_doi),
            "checked_now": len(batch) - len(errors),
            "still_unchecked": len(stale) - len(batch),
            "retracted_or_concern": serious,
            "corrections_and_other": other,
            "errors": errors,
            "next": ("Call again to check the rest." if len(stale) > len(batch) else
                     "Propose the system tag _retracted (tag_items) for the serious ones."),
        }

    # ------------------------------------------------------------ PDFs

    async def _pdf_parents(self) -> set[str]:
        parents = set()
        for it in await self.z.all_items():
            d = it["data"]
            if d.get("itemType") != "attachment" or not d.get("parentItem"):
                continue
            name = (d.get("filename") or d.get("path") or d.get("title") or "").lower()
            if d.get("contentType") == "application/pdf" or name.endswith(".pdf"):
                parents.add(d["parentItem"])
        return parents

    async def missing_pdfs(self, item_type: str | None = None, with_doi_only: bool = False,
                           limit: int = 50, offset: int = 0) -> dict:
        have = await self._pdf_parents()
        rows = []
        for it in await self.lib.regular_items():
            d = it["data"]
            if d["key"] in have or (item_type and d.get("itemType") != item_type):
                continue
            doi = item_ids(d)["doi"]
            if with_doi_only and not doi:
                continue
            rows.append({"key": d["key"], "item": label(d), "type": d.get("itemType"), "doi": doi})
        by_type = Counter(r["type"] for r in rows)
        limit = max(1, min(limit, 200))
        return {"total": len(rows), "with_doi": sum(1 for r in rows if r["doi"]),
                "by_type": dict(by_type.most_common()), "offset": offset, "items": rows[offset : offset + limit]}

    async def attach_oa_pdfs(self, keys: list[str], dry_run: bool = True) -> dict:
        keys = keys[:25]
        have = await self._pdf_parents()
        items = {i["key"]: i["data"] for i in await self.z.items_by_keys(keys)}
        found, skipped = [], {}

        async def look(key: str):
            d = items.get(key)
            if d is None:
                return key, "not found"
            if key in have:
                return key, "already has a PDF"
            doi = item_ids(d)["doi"]
            if not doi:
                return key, "no DOI"
            try:
                oa = await self.ext.unpaywall(doi)
            except ExternalError as exc:
                return key, str(exc)
            if not oa or not oa.get("pdf_url"):
                return key, "no open-access PDF known to Unpaywall"
            return key, oa

        for key, res in await asyncio.gather(*(look(k) for k in keys)):
            if isinstance(res, str):
                skipped[key] = res
            else:
                found.append({"key": key, "item": label(items[key]), **res})
        if dry_run:
            return {"dry_run": True, "would_attach": found, "skipped": skipped,
                    "next": "Nothing was downloaded. Call again with dry_run=false after approval."}
        attached, journal_changes = [], []
        tmpl = await self.z.template("attachment", "imported_url")
        for f in found:
            d = items[f["key"]]
            try:
                content = await self.ext.download_pdf(f["pdf_url"])
            except ExternalError as exc:
                skipped[f["key"]] = str(exc)
                continue
            fname = f"{current_key(d) or d['key']}.pdf"
            att = dict(tmpl)
            att.update({"itemType": "attachment", "linkMode": "imported_url", "parentItem": f["key"],
                        "title": "Full Text PDF", "url": f["pdf_url"], "contentType": "application/pdf",
                        "filename": fname, "tags": [], "relations": {}})
            att.pop("collections", None)
            res = await self.z.create_items([att])
            if not res.created:
                skipped[f["key"]] = f"could not create the attachment: {res.error or res.failed}"
                continue
            att_key = res.created[0]
            try:
                await self.z.upload_file(att_key, content, fname)
            except ZoteroError as exc:
                await self.z.update_items([{"key": att_key, "version": (await self.z.item(att_key))["version"],
                                            "deleted": True}])
                skipped[f["key"]] = f"upload failed, empty attachment moved to trash: {exc}"
                continue
            attached.append({"key": f["key"], "attachment": att_key, "item": f["item"],
                             "bytes": len(content), "source": f.get("host_type"), "version": f.get("version")})
            journal_changes.append({"key": att_key, "item": f"PDF for {f['item']}",
                                    "before": {"deleted": True}, "after": {"deleted": False}})
        journal_id = None
        if journal_changes:
            journal_id = (await self.lib.journal()).record("attach_pdfs", f"{len(attached)} PDFs", journal_changes)
        return {"attached": attached, "skipped": skipped, "journal_id": journal_id}
