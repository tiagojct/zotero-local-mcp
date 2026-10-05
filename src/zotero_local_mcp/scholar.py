"""Research assistant operations: outside search (PubMed, Europe PMC, OpenAlex, and
all three at once), open-access full text by sections, citation graph, manuscript
checks, bibliography export and the import queue.

The researcher never writes to Zotero. Its library client refuses writes, so
text read from outside sources cannot lead to library changes. Additions go
to the import queue note, which the user ticks and the librarian imports.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .citekey import current_key
from .client import LocalZotero, ZoteroError
from .config import Settings
from .external import External, ExternalError, parse_jats
from .library import Library, label, truncate
from .records import DOI_RE, LibraryIndex, detect, fingerprint, label_of, norm_doi, zotero_to_csl
from .screening import EXCLUDE, NO_TERMS, PASS, parse_terms, prescreen

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


class NoteOnlyZotero(LocalZotero):
    """Researcher client: reads, plus creating new child notes on items. Nothing else:
    no edits to existing items or notes, no tags, no trash, no imports."""

    async def _write(self, method, path, body, extra_headers=None, raw=None,
                     content_type="application/json"):
        ok = (method == "POST" and path == "items" and raw is None and isinstance(body, list) and body
              and all(isinstance(o, dict) and o.get("itemType") == "note" and o.get("parentItem")
                      and "key" not in o and "version" not in o for o in body))
        if not ok:
            raise ZoteroError("The researcher can only add new notes to items. "
                              "Other changes go through the librarian (import queue).")
        return await super()._write(method, path, body, extra_headers, raw, content_type)


def short_authors(rec: dict, n: int = 3) -> str:
    names = [a.get("family") or a.get("name") or "" for a in rec.get("authors") or []]
    names = [x for x in names if x]
    return ", ".join(names[:n]) + (" et al." if len(names) > n else "")


class Scholar:
    def __init__(self, settings: Settings, ext: External, lib: Library | None = None) -> None:
        self.s = settings
        self.ext = ext
        self.lib = lib or Library(settings, NoteOnlyZotero(settings.api_url, settings.state_dir,
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

    async def search_europepmc(self, query: str, max_results: int = 25, year_from: int | None = None,
                               year_to: int | None = None, open_access_only: bool = False,
                               abstracts: bool = True) -> dict:
        count, recs = await self.ext.europepmc_search(query, max_results, year_from, year_to, open_access_only)
        index = await self.index()
        rows = []
        for r in recs:
            row = self.row(r, index, abstracts)
            row["pmcid"] = r.get("pmcid") or None
            row["open_access"] = bool(r.get("open_access"))
            rows.append(row)
        return {"source": "Europe PMC", "query": query, "total_hits": count, "returned": len(rows),
                "already_in_library": sum(1 for r in rows if r["in_library"]), "results": rows}

    # ------------------------------------------------------------ several phrasings, three sources

    SOURCES = ("pubmed", "europepmc", "openalex")

    async def search_multi(self, queries: list[str], sources: list[str] | None = None,
                           max_per_query: int = 10, year_from: int | None = None,
                           year_to: int | None = None, limit: int = 40,
                           include_terms: list[str] | None = None, exclude_terms: list[str] | None = None,
                           rank_by: str = "found") -> dict:
        """Run every phrasing on every source, merge the same work (DOI, PMID, PMCID, then title
        and year), and return one compact list, works found most often first.

        include_terms / exclude_terms: keyword pre-screen on title and abstract (see screening.py);
        excluded works stay in the list, last. rank_by: "found" (number of searches, best rank,
        cited_by) or "cited" (number of searches + cited_by / highest cited_by in the set)."""
        queries = [q.strip() for q in queries if q and q.strip()][:6]
        if not queries:
            raise ValueError("Give at least one query.")
        if rank_by not in ("found", "cited"):
            raise ValueError("rank_by must be 'found' or 'cited'.")
        include, exclude = parse_terms(include_terms), parse_terms(exclude_terms)
        screen = bool(include or exclude)
        sources = [s for s in (sources or self.SOURCES) if s in self.SOURCES] or list(self.SOURCES)
        per = max(1, min(max_per_query, 25))

        async def run(source: str, qi: int) -> tuple[str, int, int, list[dict]]:
            q = queries[qi]
            if source == "pubmed":
                count, ids = await self.ext.pubmed_search(q, per, None, "relevance", year_from, year_to)
                return source, qi, count, await self.ext.pubmed_fetch(ids)
            if source == "europepmc":
                count, recs = await self.ext.europepmc_search(q, per, year_from, year_to)
                return source, qi, count, recs
            count, recs = await self.ext.openalex_search(q, per, year_from, year_to)
            return source, qi, count, recs

        jobs = [(s, i) for s in sources for i in range(len(queries))]
        done = await asyncio.gather(*(run(s, i) for s, i in jobs), return_exceptions=True)
        hits: dict[str, dict[str, int]] = {s: {} for s in sources}
        errors: list[str] = []
        merged: list[dict] = []
        keys: dict[str, int] = {}
        retrieved = 0
        for (source, qi), res in zip(jobs, done):
            if isinstance(res, Exception):
                errors.append(f"{source}, query {qi + 1}: {res}")
                continue
            _, _, count, recs = res
            hits[source][f"q{qi + 1}"] = count
            retrieved += len(recs)
            for rank, rec in enumerate(recs):
                ids = [f"doi:{rec['doi']}" if rec.get("doi") else "", f"pmid:{rec['pmid']}" if rec.get("pmid") else "",
                       f"pmcid:{rec['pmcid']}" if rec.get("pmcid") else "",
                       f"t:{fp}" if (fp := fingerprint(rec.get("title"), rec.get("year"))) else ""]
                ids = [i for i in ids if i]
                at = next((keys[i] for i in ids if i in keys), None)
                if at is None:
                    at = len(merged)
                    merged.append({"rec": rec, "found": [], "best_rank": rank})
                m = merged[at]
                for i in ids:
                    keys.setdefault(i, at)
                m["found"].append(f"{source}:q{qi + 1}")
                m["best_rank"] = min(m["best_rank"], rank)
                for field in ("doi", "pmid", "pmcid", "container", "year", "abstract"):  # fill gaps
                    if not m["rec"].get(field) and rec.get(field):
                        m["rec"] = {**m["rec"], field: rec[field]}
                if rec.get("cited_by") is not None:
                    m["cited_by"] = max(m.get("cited_by") or 0, rec["cited_by"])
                if rec.get("open_access") or rec.get("oa_url"):
                    m["open_access"] = True
        index = await self.index()
        if screen:
            for m in merged:
                m["screen"] = prescreen(m["rec"].get("title"), m["rec"].get("abstract"), include, exclude)
        top_cited = max((m.get("cited_by") or 0 for m in merged), default=0)

        def order(m: dict) -> tuple:
            excluded = screen and m["screen"]["prescreen"] == EXCLUDE
            n = len(set(m["found"]))
            if rank_by == "cited":
                prior = n + ((m.get("cited_by") or 0) / top_cited if top_cited else 0)
                return (excluded, -prior, m["best_rank"])
            return (excluded, -n, m["best_rank"], -(m.get("cited_by") or 0))

        merged.sort(key=order)
        rows = []
        for m in merged[: max(1, min(limit, 100))]:
            r = m["rec"]
            row = {k: v for k, v in {
                "title": truncate(r.get("title") or "", 180), "first_author": short_authors(r, 1),
                "year": r.get("year"), "venue": r.get("container"), "doi": r.get("doi"),
                "pmid": r.get("pmid") or None, "pmcid": r.get("pmcid") or None,
                "open_access": m.get("open_access") or None, "cited_by": m.get("cited_by"),
                "in_library": index.match_record(r), "found_by": sorted(set(m["found"])),
            }.items() if v not in (None, "", [])}
            if screen:
                row.update(m["screen"])
            rows.append(row)
        out: dict[str, Any] = {"queries": {f"q{i + 1}": q for i, q in enumerate(queries)}, "total_hits": hits,
                               "unique_works": len(merged), "returned": len(rows),
                               "already_in_library": sum(1 for r in rows if r.get("in_library"))}
        if rank_by != "found":
            out["rank_by"] = rank_by
        if screen:
            decisions = [m["screen"]["prescreen"] for m in merged]
            out["screening"] = {"include_terms": include, "exclude_terms": exclude,
                                "found": retrieved, "unique": len(merged),
                                "prescreen_pass": decisions.count(PASS),
                                "prescreen_exclude": decisions.count(EXCLUDE),
                                "no_terms_matched": decisions.count(NO_TERMS)}
        if errors:
            out["errors"] = errors
        out["results"] = rows
        return out

    # ------------------------------------------------------------ open-access full text

    async def read_oa_fulltext(self, identifier: str, sections: list[str] | None = None,
                               max_chars: int = 12000) -> dict:
        """Europe PMC open-access full text. Without sections: the section list, abstract,
        figure and table captions. With sections (IMRaD names such as methods or results, or
        section numbers): the text of those sections, at most max_chars in total."""
        ident = identifier.strip()
        if re.fullmatch(r"(?i)pmc\d+", ident) or ident.lower().startswith("pmcid:"):
            rec = await self.ext.europepmc_record(pmcid=ident.split(":")[-1].strip())
        else:
            kind, value = await self._resolve(ident)
            if kind not in ("doi", "pmid"):
                raise ValueError("Give a PMCID, a PMID, a DOI or a Zotero item key with a DOI or PMID.")
            rec = await self.ext.europepmc_record(**{kind: value})
        if rec is None:
            return {"identifier": identifier, "status": "not_found",
                    "detail": "Europe PMC has no record for this identifier."}
        head = {"identifier": identifier, "title": rec.get("title"), "pmid": rec.get("pmid") or None,
                "pmcid": rec.get("pmcid") or None, "doi": rec.get("doi"),
                "in_library": (await self.index()).match_record(rec)}
        if not rec.get("open_access") or not rec.get("pmcid"):
            return {**head, "status": "not_open_access",
                    "detail": "Not in Europe PMC's open-access full-text set. Use the abstract, or the "
                              "library's full text (zotero_get_fulltext) if the PDF is in Zotero.",
                    "abstract": truncate(rec.get("abstract") or "", 2000)}
        xml = await self.ext.europepmc_fulltext(rec["pmcid"])
        if not xml:
            return {**head, "status": "no_full_text", "abstract": truncate(rec.get("abstract") or "", 2000)}
        doc = parse_jats(xml)
        inventory = [{k: s[k] for k in ("index", "title", "imrad", "chars")} for s in doc["sections"]]
        out = {**head, "status": "full_text", "source": f"Europe PMC {rec['pmcid']}", "sections": inventory,
               "n_figures": doc["n_figures"], "n_tables": doc["n_tables"], "n_references": doc["n_references"]}
        if not sections:
            return {**out, "abstract": truncate(doc["abstract"] or rec.get("abstract") or "", 3000),
                    "figures": doc["figures"], "tables": doc["tables"],
                    "next": "Ask for sections by IMRaD name (methods, results...) or number."}
        want = {str(x).strip().lower() for x in sections}
        chosen = [s for s in doc["sections"] if s["imrad"] in want or str(s["index"]) in want
                  or s["title"].lower() in want]
        budget = max(1000, min(max_chars, 40000))
        texts, used = [], 0
        for s in chosen:
            part = s["text"][: max(0, budget - used)]
            used += len(part)
            texts.append({"index": s["index"], "title": s["title"], "imrad": s["imrad"], "text": part,
                          **({"truncated": True} if len(part) < len(s["text"]) else {})})
        missing = sorted(want - {s["imrad"] for s in chosen} - {str(s["index"]) for s in chosen}
                         - {s["title"].lower() for s in chosen})
        return {**out, "text": texts, **({"not_found": missing} if missing else {})}

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

    async def find_contact(self, identifier: str) -> dict:
        """Email addresses of the authors of a work, as printed in its PubMed record (the
        corresponding author's address is usually in the affiliation). Nothing is sent."""
        kind, value = await self._resolve(identifier)
        rec = None
        if kind == "pmid":
            recs = await self.ext.pubmed_fetch([value])
            rec = recs[0] if recs else None
        elif kind == "doi":
            rec = await self.ext.pubmed_for_doi(value)
        else:
            raise ValueError("Give a DOI, a PMID or a Zotero item key with one.")
        if rec is None:
            return {"identifier": identifier, "contacts": [], "detail": "No PubMed record. Look for "
                    "'Correspondence' on the article page" + (f": https://doi.org/{value}" if kind == "doi" else ".")}
        seen, contacts = set(), []
        for c in rec.get("contacts") or []:
            if c["email"] not in seen:
                seen.add(c["email"])
                contacts.append(c)
        out = {"identifier": identifier, "title": rec.get("title"), "pmid": rec.get("pmid"), "doi": rec.get("doi"),
               "authors": short_authors(rec, 3), "year": rec.get("year"), "journal": rec.get("container"),
               "contacts": contacts}
        if not contacts:
            out["detail"] = ("The PubMed record has no email address. Look for 'Correspondence' on the article page"
                             + (f": https://doi.org/{rec['doi']}" if rec.get("doi") else "."))
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

    # ------------------------------------------------------------ linked note in Zotero

    async def attach_note(self, key: str, summary: str, note_path: str, dry_run: bool = True) -> dict:
        """Add a short child note to an item: the summary plus an obsidian:// link to the vault note."""
        import html as _html
        from urllib.parse import quote

        import markdown as md

        from .library import regular

        vault = self.s.vault
        if vault is None:
            raise ZoteroError("Set ZOTERO_VAULT first.")
        p = Path(note_path).expanduser()
        p = (p if p.is_absolute() else vault / p).resolve()
        if vault.resolve() not in p.parents or p.suffix != ".md" or not p.exists():
            raise ZoteroError(f"The linked note must be an existing .md file in the vault: {note_path}")
        summary = (summary or "").strip()
        if not summary:
            raise ZoteroError("Give a short summary (a few lines).")
        if len(summary) > 1500:
            raise ZoteroError("Keep the summary under 1500 characters; the full text stays in the vault note.")
        item = await self.lib.z.item(key)
        if not regular(item):
            raise ZoteroError("Notes can only be attached to regular items.")
        rel = p.relative_to(vault.resolve()).with_suffix("").as_posix()
        uri = f"obsidian://open?vault={quote(vault.name)}&file={quote(rel, safe='')}"
        for child in await self.lib.z.children(key):
            if child["data"].get("itemType") == "note" and uri in _html.unescape(child["data"].get("note", "")):
                return {"skipped": "this item already has a note linking to that vault note",
                        "note": child["key"]}
        body = md.markdown(_html.escape(summary, quote=False), extensions=["sane_lists"])
        link = f'<p><a href="{_html.escape(uri)}">Obsidian: {_html.escape(p.stem)}</a></p>'
        if dry_run:
            return {"dry_run": True, "item": label(item["data"]), "summary": summary, "link": uri,
                    "next": "Nothing was written. Call again with dry_run=false after approval."}
        tags = [{"tag": self.s.marker}] if self.s.marker else []
        res = await self.lib.z.create_items([{
            "itemType": "note", "parentItem": key, "note": f"<div>{body}{link}</div>",
            "tags": tags, "collections": [], "relations": {},
        }])
        if not res.created:
            raise ZoteroError(f"Creating the note failed: {res.error or res.failed}")
        note_key = res.created[0]
        jid = (await self.lib.journal()).record(
            "attach_note", f"linked note on {key}",
            [{"key": note_key, "item": f"note on {label(item['data'])}",
              "before": {"deleted": True}, "after": {"deleted": False}}])
        return {"created": note_key, "item": label(item["data"]), "link": uri, "journal_id": jid,
                "undo": "Ask the librarian to undo this journal entry if the note is not wanted."}

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
                "next": "The user ticks the lines they want; the librarian imports them."}
