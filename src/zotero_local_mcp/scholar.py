"""Research assistant operations: outside search, citation graph, manuscript
checks, bibliography export and the import queue.

The researcher never writes to Zotero. Its library client refuses writes, so
text read from outside sources cannot lead to library changes. Additions go
to the import queue note, which Tiago ticks and the librarian imports.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .citekey import current_key
from .client import LocalZotero, ZoteroError
from .config import Settings
from .external import External, ExternalError
from .library import Library, label, truncate
from .records import DOI_RE, LibraryIndex, detect, label_of, norm_doi, zotero_to_csl

CITE_RE = re.compile(r"(?<![\w.@])-?@([A-Za-z0-9_][A-Za-z0-9_:.#$%&\-+?<>~/]*)")
CROSSREF_PREFIXES = ("fig-", "tbl-", "sec-", "eq-", "lst-", "thm-", "lem-", "cor-", "prp-", "def-", "exm-", "exr-")
TEXT_SUFFIXES = {".md", ".qmd", ".markdown", ".txt", ".rmd"}
QUEUE_HEADER = (
    "Import queue for the Zotero librarian. Tick the lines to import, then ask the librarian: "
    "\"Import the ticked items from the queue.\"\n\n"
)


class ReadOnlyZotero(LocalZotero):
    async def authorize(self) -> str:  # pragma: no cover - guard
        raise ZoteroError("The researcher cannot write to Zotero. Use the import queue.")

    async def _write(self, *args, **kwargs):
        raise ZoteroError("The researcher cannot write to Zotero. Use the import queue.")


def short_authors(rec: dict, n: int = 3) -> str:
    names = [a.get("family") or a.get("name") or "" for a in rec.get("authors") or []]
    names = [x for x in names if x]
    return ", ".join(names[:n]) + (" et al." if len(names) > n else "")


class Scholar:
    def __init__(self, settings: Settings, ext: External, lib: Library | None = None) -> None:
        self.s = settings
        self.ext = ext
        self.lib = lib or Library(settings, ReadOnlyZotero(settings.api_url, settings.state_dir,
                                                            settings.auth_timeout))

    async def index(self) -> LibraryIndex:
        return LibraryIndex(await self.lib.regular_items())

    def row(self, rec: dict, index: LibraryIndex, abstract: bool = False) -> dict:
        out: dict[str, Any] = {
            "title": rec.get("title"),
            "authors": short_authors(rec),
            "year": rec.get("year"),
            "venue": rec.get("container"),
            "type": rec.get("kind") or ", ".join(rec.get("publication_types") or []),
            "doi": rec.get("doi"),
            "pmid": rec.get("pmid") or None,
            "cited_by": rec.get("cited_by"),
            "open_access": rec.get("oa_url"),
            "in_library": index.match_record(rec),
        }
        if rec.get("is_retracted"):
            out["retracted"] = True
        if abstract:
            out["abstract"] = truncate(rec.get("abstract") or "", 700)
        return out

    # ------------------------------------------------------------ search

    async def search_pubmed(self, query: str, max_results: int = 20, year_from: int | None = None,
                            year_to: int | None = None, sort: str = "relevance",
                            abstracts: bool = True) -> dict:
        count, ids = await self.ext.pubmed_search(query, min(max_results, 100), None,
                                                  "pub_date" if sort == "date" else "relevance",
                                                  year_from, year_to)
        recs = await self.ext.pubmed_fetch(ids)
        index = await self.index()
        rows = [self.row(r, index, abstracts) for r in recs]
        return {"source": "PubMed", "query": query, "total_hits": count, "returned": len(rows),
                "already_in_library": sum(1 for r in rows if r["in_library"]), "results": rows}

    async def search_openalex(self, query: str, max_results: int = 25, year_from: int | None = None,
                              year_to: int | None = None, sort: str = "relevance",
                              abstracts: bool = True) -> dict:
        count, recs = await self.ext.openalex_search(query, max_results, year_from, year_to,
                                                     sort if sort != "relevance" else None)
        index = await self.index()
        rows = [self.row(r, index, abstracts) for r in recs]
        return {"source": "OpenAlex", "query": query, "total_hits": count, "returned": len(rows),
                "already_in_library": sum(1 for r in rows if r["in_library"]), "results": rows}

    async def _resolve(self, identifier: str) -> tuple[str, str | None]:
        """Zotero item key, DOI, PMID or OpenAlex id -> (kind, value) usable outside."""
        ident = identifier.strip()
        if re.fullmatch(r"[0-9A-Z]{8}", ident) and not ident.isdigit():
            item = await self.lib.z.item(ident)
            from .records import item_ids
            ids = item_ids(item["data"])
            if ids["doi"]:
                return "doi", ids["doi"]
            if ids["pmid"]:
                return "pmid", ids["pmid"]
            raise ZoteroError(f"Item {ident} has no DOI or PMID to look up.")
        if re.fullmatch(r"W\d+", ident):
            return "openalex", ident
        det = detect(ident)
        if det is None:
            raise ZoteroError(f"Not a Zotero key, DOI, PMID or OpenAlex id: {identifier}")
        return det

    async def get_work(self, identifier: str) -> dict:
        kind, value = await self._resolve(identifier)
        rec = None
        if kind == "doi":
            rec = await self.ext.crossref_work(value)
            oa = await self.ext.openalex_work(value)
            if rec and oa:
                rec["cited_by"] = oa.get("cited_by")
                rec["oa_url"] = oa.get("oa_url")
                rec["openalex_id"] = oa.get("openalex_id")
                rec["is_retracted"] = oa.get("is_retracted")
                rec["abstract"] = rec.get("abstract") or oa.get("abstract")
            rec = rec or oa
        elif kind == "pmid":
            recs = await self.ext.pubmed_fetch([value])
            rec = recs[0] if recs else None
        elif kind == "openalex":
            rec = await self.ext.openalex_work(value)
        elif kind == "isbn":
            rec = await self.ext.openlibrary_isbn(value)
        if rec is None:
            raise ZoteroError(f"Nothing found for {identifier}.")
        index = await self.index()
        out = self.row(rec, index)
        out["abstract"] = rec.get("abstract") or ""
        out["authors_full"] = [a.get("name") or f"{a.get('family')}, {a.get('given')}"
                               for a in rec.get("authors") or []]
        out["openalex_id"] = rec.get("openalex_id")
        return out

    async def citation_graph(self, identifier: str, direction: str = "both",
                             max_results: int = 50) -> dict:
        kind, value = await self._resolve(identifier)
        ident = value if kind != "pmid" else f"pmid:{value}"
        work = await self.ext.openalex_work(ident)
        if work is None:
            raise ZoteroError(f"OpenAlex does not know {identifier}.")
        index = await self.index()
        out: dict[str, Any] = {"work": label_of(work), "openalex_id": work["openalex_id"],
                               "doi": work.get("doi")}
        if direction in ("references", "both"):
            refs = await self.ext.openalex_by_ids(work.get("referenced_works", [])[:300])
            rows = sorted((self.row(r, index) for r in refs), key=lambda r: -(r["cited_by"] or 0))
            out["references"] = {
                "total": len(work.get("referenced_works", [])),
                "in_library": sum(1 for r in rows if r["in_library"]),
                "missing_from_library": [r for r in rows if not r["in_library"]][:max_results],
                "in_library_items": [{"citekey": r["in_library"]["citekey"], "title": r["title"]}
                                     for r in rows if r["in_library"]],
            }
        if direction in ("cited_by", "both"):
            total, citing = await self.ext.openalex_cited_by(work["openalex_id"], max_results)
            rows = [self.row(r, index) for r in citing]
            out["cited_by"] = {"total": total, "shown": len(rows),
                               "in_library": sum(1 for r in rows if r["in_library"]),
                               "most_cited": rows}
        return out

    async def library_lookup(self, identifiers: list[str]) -> dict:
        index = await self.index()
        out = {}
        for ident in identifiers:
            det = detect(ident)
            if det is None:
                out[ident] = "not a DOI, PMID or ISBN"
                continue
            hit = index.lookup(**{det[0]: det[1]})
            out[ident] = ({"key": hit[0], "citekey": current_key(index.data[hit[0]]),
                           "item": label(index.data[hit[0]])} if hit else None)
        return out

    # ------------------------------------------------------------ manuscripts

    @staticmethod
    def _read_text(path: str) -> tuple[Path, str]:
        p = Path(path).expanduser()
        if p.suffix.lower() not in TEXT_SUFFIXES:
            raise ZoteroError(f"Only text manuscripts ({', '.join(sorted(TEXT_SUFFIXES))}); got {p.suffix}.")
        if not p.exists():
            raise ZoteroError(f"No file at {p}.")
        return p, p.read_text(encoding="utf-8")

    @staticmethod
    def citekeys_in(text: str) -> list[str]:
        keys = []
        for m in CITE_RE.finditer(text):
            key = m.group(1).rstrip(".:;,?!)")
            if key.startswith(CROSSREF_PREFIXES) or not key:
                continue
            if key not in keys:
                keys.append(key)
        return keys

    async def check_manuscript(self, path: str) -> dict:
        p, text = self._read_text(path)
        keys = self.citekeys_in(text)
        index = await self.index()
        found, missing = [], []
        for k in keys:
            if k in index.by_citekey:
                found.append({"citekey": k, "item": label(index.data[index.by_citekey[k]])})
            else:
                stem = re.sub(r"\d.*$", "", k)
                near = [c for c in index.by_citekey if stem and c.startswith(stem)][:5]
                missing.append({"citekey": k, "similar_in_library": near})
        dois = []
        for m in DOI_RE.finditer(text):
            d = norm_doi(m.group(1))
            hit = index.lookup(doi=d)
            dois.append({"doi": d, "in_library": current_key(index.data[hit[0]]) if hit else None})
        bib = None
        fm = re.match(r"^---\n(.*?)\n---", text, re.S)
        if fm:
            bm = re.search(r"^bibliography:\s*(.+)$", fm.group(1), re.M)
            if bm:
                bpath = (p.parent / bm.group(1).strip().strip("'\"")).resolve()
                bib = {"path": str(bpath), "exists": bpath.exists()}
                if bpath.exists():
                    btext = bpath.read_text(encoding="utf-8", errors="replace")
                    if bpath.suffix == ".json":
                        try:
                            ids = {e.get("id") for e in json.loads(btext)}
                        except ValueError:
                            ids = set()
                    else:
                        ids = set(re.findall(r"@\w+\s*\{\s*([^,\s]+)\s*,", btext))
                    bib["missing_from_bibliography"] = [k for k in keys if k not in ids]
        return {"file": str(p), "citations": len(keys), "found": found, "missing": missing,
                "dois_in_text": dois, "bibliography": bib}

    async def export_bibliography(self, output_path: str, citekeys: list[str] | None = None,
                                  manuscript: str | None = None, collection: str | None = None,
                                  tags: list[str] | None = None, overwrite: bool = False) -> dict:
        out = Path(output_path).expanduser()
        if out.suffix.lower() != ".json":
            raise ZoteroError("Write CSL JSON: the output file must end in .json.")
        home = Path.home().resolve()
        if home not in out.resolve().parents:
            raise ZoteroError("The output file must be inside your home folder.")
        if not out.parent.exists():
            raise ZoteroError(f"Folder {out.parent} does not exist.")
        if out.exists() and not overwrite:
            raise ZoteroError(f"{out} exists. Call again with overwrite=true to replace it.")
        index = await self.index()
        wanted: list[str] = list(citekeys or [])
        if manuscript:
            _, text = self._read_text(manuscript)
            wanted += [k for k in self.citekeys_in(text) if k not in wanted]
        chosen: dict[str, dict] = {}
        missing, no_key = [], []
        for k in wanted:
            if k in index.by_citekey:
                chosen[k] = index.data[index.by_citekey[k]]
            else:
                missing.append(k)
        if collection or tags:
            res = await self.lib.find(collection=collection, tags=tags, limit=200)
            total = res["total"]
            offset = 0
            while offset < total:
                page = res if offset == 0 else await self.lib.find(collection=collection, tags=tags,
                                                                   limit=200, offset=offset)
                for it in page["items"]:
                    if it["citekey"]:
                        chosen[it["citekey"]] = index.data[it["key"]]
                    else:
                        no_key.append({"key": it["key"], "title": it["title"]})
                offset += 200
        entries = [zotero_to_csl(d, k) for k, d in sorted(chosen.items())]
        out.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"written": str(out), "entries": len(entries), "missing_citekeys": missing,
                "items_without_citekey": no_key,
                "note": "Items without a citekey need the librarian (set_citekeys)." if no_key else None}

    # ------------------------------------------------------------ import queue

    async def queue_imports(self, entries: list[dict]) -> dict:
        qpath = self.s.queue_path
        if qpath is None:
            raise ZoteroError("Set ZOTERO_VAULT so the import queue note can be written.")
        qpath.parent.mkdir(parents=True, exist_ok=True)
        existing = qpath.read_text(encoding="utf-8") if qpath.exists() else QUEUE_HEADER
        index = await self.index()
        added, skipped = [], {}
        today = dt.date.today().isoformat()
        from .manage import ids_in_line
        queued = set()
        for ln in existing.splitlines():
            ident = ids_in_line(ln) if ln.lstrip().startswith(("- [", "* [")) else None
            if ident and (d := detect(ident)):
                queued.add(d)
        lines = []

        def clean(text: str) -> str:
            # One line only, and no brackets: a label must never create or tick a line.
            return re.sub(r"\s+", " ", text or "").replace("[", "(").replace("]", ")").strip()

        for e in entries:
            ident = (e.get("identifier") or "").strip()
            det = detect(ident)
            if det is None:
                skipped[ident] = "not a DOI, PMID or ISBN"
                continue
            kind, value = det
            token = f"{kind}:{value}"
            if det in queued:
                skipped[ident] = "already in the queue"
                continue
            queued.add(det)
            hit = index.lookup(**{kind: value})
            if hit:
                skipped[ident] = f"already in library ({current_key(index.data[hit[0]]) or hit[0]})"
                continue
            parts = [token, clean(e.get("label")), clean(e.get("reason")), today]
            lines.append("- [ ] " + " | ".join(x for x in parts if x))
            added.append(token)
        if lines:
            text = existing if existing.endswith("\n") else existing + "\n"
            qpath.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")
        return {"queue": str(qpath), "added": added, "skipped": skipped,
                "next": "Tiago ticks the lines he wants; the librarian imports them."}
