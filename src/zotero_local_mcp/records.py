"""Identifiers, a common bibliographic record, and conversions between formats.

A record is a plain dict produced by the external clients (Crossref, PubMed,
OpenAlex, Open Library) with these keys, any of which may be empty:

    source, kind, title, authors [{family, given, name, role}], date, year,
    container, container_abbr, volume, issue, pages, doi, pmid, pmcid, isbn,
    issn, publisher, place, abstract, url, language, cited_by, oa_url,
    openalex_id, is_retracted
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from .citekey import current_key, first_creator_name, slug, year_of

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"|]+)", re.IGNORECASE)
EXTRA_FIELD = re.compile(r"^\s*(DOI|PMID|PMCID|ISBN)\s*:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------- identifiers

def norm_doi(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    v = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", v, flags=re.IGNORECASE)
    m = DOI_RE.search(v)
    return m.group(1).rstrip(".,;)").lower() if m else None


def norm_isbn(value: str | None) -> str | None:
    if not value:
        return None
    v = re.sub(r"[\s-]", "", value).upper()
    if not re.fullmatch(r"\d{9}[\dX]|\d{13}", v):
        return None
    if len(v) == 10:
        core = "978" + v[:9]
        total = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core))
        return core + str((10 - total % 10) % 10)
    return v if len(v) == 13 else None


def detect(identifier: str) -> tuple[str, str] | None:
    """Return (kind, normalized value): kind is doi, pmid or isbn."""
    s = identifier.strip()
    low = s.lower()
    if low.startswith("pmid:") or re.fullmatch(r"\d{1,9}", s):
        digits = re.sub(r"\D", "", s)
        return ("pmid", digits) if digits else None
    if low.startswith("isbn:"):
        isbn = norm_isbn(s[5:])
        return ("isbn", isbn) if isbn else None
    doi = norm_doi(s)
    if doi:
        return ("doi", doi)
    isbn = norm_isbn(s)
    if isbn and re.fullmatch(r"[\d\-\sXx]{10,17}", s):
        return ("isbn", isbn)
    return None


def extra_ids(extra: str | None) -> dict[str, str]:
    return {m.group(1).upper(): m.group(2) for m in EXTRA_FIELD.finditer(extra or "")}


def item_ids(data: dict) -> dict[str, Any]:
    ex = extra_ids(data.get("extra"))
    isbns = {i for part in re.split(r"[,;\s]+", data.get("ISBN") or "") if (i := norm_isbn(part))}
    if ex.get("ISBN") and (i := norm_isbn(ex["ISBN"])):
        isbns.add(i)
    return {
        "doi": norm_doi(data.get("DOI") or ex.get("DOI")),
        "pmid": ex.get("PMID"),
        "isbns": isbns,
    }


def fingerprint(title: str | None, year: str | None) -> str | None:
    t = slug(title or "")
    return f"{t[:80]}|{year}" if len(t) >= 12 and year else None


def title_similarity(a: str | None, b: str | None) -> float:
    return SequenceMatcher(None, slug(a or ""), slug(b or "")).ratio()


# ---------------------------------------------------------------- library index

class LibraryIndex:
    """Lookup of library items by DOI, PMID, ISBN, title+year and citekey."""

    def __init__(self, items: list[dict]) -> None:
        self.by_doi: dict[str, str] = {}
        self.by_pmid: dict[str, str] = {}
        self.by_isbn: dict[str, str] = {}
        self.by_fp: dict[str, str] = {}
        self.by_citekey: dict[str, str] = {}
        self.data: dict[str, dict] = {}
        for it in items:
            d = it["data"]
            key = d["key"]
            self.data[key] = d
            ids = item_ids(d)
            if ids["doi"]:
                self.by_doi.setdefault(ids["doi"], key)
            if ids["pmid"]:
                self.by_pmid.setdefault(ids["pmid"], key)
            for i in ids["isbns"]:
                self.by_isbn.setdefault(i, key)
            fp = fingerprint(d.get("title"), year_of(d))
            if fp:
                self.by_fp.setdefault(fp, key)
            ck = current_key(d)
            if ck:
                self.by_citekey.setdefault(ck, key)

    def lookup(self, doi: str | None = None, pmid: str | None = None, isbn: str | None = None,
               title: str | None = None, year: str | None = None,
               first_author: str | None = None) -> tuple[str, str] | None:
        if doi and (k := self.by_doi.get(norm_doi(doi) or "")):
            return k, "DOI"
        if pmid and (k := self.by_pmid.get(str(pmid))):
            return k, "PMID"
        if isbn and (k := self.by_isbn.get(norm_isbn(isbn) or "")):
            return k, "ISBN"
        fp = fingerprint(title, year)
        if fp and (k := self.by_fp.get(fp)):
            # A title match alone is weak ("Editorial", "Reply to ..."): reject it when the
            # identifiers disagree, and require the same first author when both are known.
            ids = item_ids(self.data[k])
            if doi and ids["doi"] and norm_doi(doi) != ids["doi"]:
                return None
            if pmid and ids["pmid"] and str(pmid) != ids["pmid"]:
                return None
            theirs = slug(first_creator_name(self.data[k]))
            if not first_author or not theirs or slug(first_author) != theirs:
                return None
            return k, "title, year and first author"
        return None

    def match_record(self, rec: dict) -> dict | None:
        first = (rec.get("authors") or [{}])[0]
        author = first.get("family") or (first.get("name") or "").split(" ")[-1]
        hit = self.lookup(rec.get("doi"), rec.get("pmid"), rec.get("isbn"), rec.get("title"),
                          rec.get("year"), author)
        if not hit:
            return None
        key, how = hit
        return {"key": key, "citekey": current_key(self.data[key]), "matched_by": how}

    def add(self, key: str, data: dict) -> None:
        self.__init__([{"data": d} for d in [*self.data.values(), {**data, "key": key}]])


# ---------------------------------------------------------------- record -> Zotero

KIND_TO_ZOTERO = {
    "journal-article": "journalArticle", "article": "journalArticle", "review": "journalArticle",
    "book": "book", "monograph": "book", "edited-book": "book", "reference-book": "book",
    "book-chapter": "bookSection", "book-section": "bookSection", "book-part": "bookSection",
    "proceedings-article": "conferencePaper", "preprint": "preprint", "posted-content": "preprint",
    "report": "report", "dissertation": "thesis", "dataset": "dataset",
}
CONTAINER_FIELD = {
    "journalArticle": "publicationTitle", "bookSection": "bookTitle",
    "conferencePaper": "proceedingsTitle", "preprint": "repository",
}


def zotero_type(rec: dict) -> str:
    return KIND_TO_ZOTERO.get(rec.get("kind") or "", "journalArticle" if rec.get("container") else "document")


def record_to_zotero(rec: dict, template: dict) -> dict:
    """Fill a Zotero item template (from /api/items/new) from a record.

    Only fields that exist in the template are set; identifiers without a field
    go to Extra. Keywords and MeSH headings are never imported as tags.
    """
    item = {k: v for k, v in template.items()}
    ztype = item["itemType"]

    def put(field: str, value: Any) -> bool:
        if value in (None, "", []) or field not in item:
            return False
        item[field] = value
        return True

    creators = []
    for a in rec.get("authors") or []:
        role = a.get("role") or "author"
        if a.get("family"):
            creators.append({"creatorType": role, "lastName": a["family"], "firstName": a.get("given") or ""})
        elif a.get("name"):
            creators.append({"creatorType": role, "name": a["name"]})
    item["creators"] = creators
    put("title", rec.get("title"))
    put(CONTAINER_FIELD.get(ztype, "publicationTitle"), rec.get("container"))
    put("journalAbbreviation", rec.get("container_abbr"))
    for field, key in (("volume", "volume"), ("issue", "issue"), ("pages", "pages"), ("date", "date"),
                       ("ISSN", "issn"), ("publisher", "publisher"), ("place", "place"),
                       ("abstractNote", "abstract"), ("url", "url"), ("language", "language")):
        put(field, rec.get(key))
    extra = []
    if rec.get("doi") and not put("DOI", rec["doi"]):
        extra.append(f"DOI: {rec['doi']}")
    if rec.get("isbn") and not put("ISBN", rec["isbn"]):
        extra.append(f"ISBN: {rec['isbn']}")
    if rec.get("pmid"):
        extra.append(f"PMID: {rec['pmid']}")
    if rec.get("pmcid"):
        extra.append(f"PMCID: {rec['pmcid']}")
    if "extra" in item:
        item["extra"] = "\n".join(extra)
    item["tags"] = []
    item.setdefault("collections", [])
    item.setdefault("relations", {})
    return item


def label_of(rec: dict) -> str:
    first = (rec.get("authors") or [{}])[0]
    who = first.get("family") or first.get("name") or "?"
    return f"{who} {rec.get('year') or 'n.d.'}: {(rec.get('title') or '')[:80]}"


# ---------------------------------------------------------------- Zotero -> CSL JSON

CSL_TYPE = {
    "journalArticle": "article-journal", "book": "book", "bookSection": "chapter",
    "conferencePaper": "paper-conference", "report": "report", "thesis": "thesis",
    "webpage": "webpage", "preprint": "article", "magazineArticle": "article-magazine",
    "newspaperArticle": "article-newspaper", "dataset": "dataset", "presentation": "speech",
    "blogPost": "post-weblog", "encyclopediaArticle": "entry-encyclopedia", "document": "document",
}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def date_parts(date: str | None) -> list[int] | None:
    if not date:
        return None
    m = re.match(r"^\s*(\d{4})(?:-(\d{1,2})(?!\d)(?:-(\d{1,2})(?!\d))?)?(?!\d)", date)
    if m:
        parts = [int(m.group(1))]
        if m.group(2) and 1 <= int(m.group(2)) <= 12:
            parts.append(int(m.group(2)))
            if m.group(3) and 1 <= int(m.group(3)) <= 31:
                parts.append(int(m.group(3)))
        return parts
    year = year_of({"date": date})
    if not year:
        return None
    parts = [int(year)]
    mm = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", date, re.I)
    if mm:
        parts.append(MONTHS[mm.group(1).lower()])
    return parts


def zotero_to_csl(data: dict, citekey: str) -> dict:
    out: dict[str, Any] = {"id": citekey, "type": CSL_TYPE.get(data.get("itemType", ""), "document")}
    names: dict[str, list] = {}
    for c in data.get("creators") or []:
        role = {"author": "author", "editor": "editor", "seriesEditor": "collection-editor",
                "translator": "translator", "bookAuthor": "container-author"}.get(c.get("creatorType"), "author")
        if c.get("lastName"):
            names.setdefault(role, []).append({"family": c["lastName"], "given": c.get("firstName", "")})
        elif c.get("name"):
            names.setdefault(role, []).append({"literal": c["name"]})
    out.update(names)
    container = (data.get("publicationTitle") or data.get("bookTitle") or data.get("proceedingsTitle")
                 or data.get("websiteTitle") or data.get("repository") or "")
    ex = extra_ids(data.get("extra"))
    for csl, value in (
        ("title", data.get("title")), ("container-title", container),
        ("container-title-short", data.get("journalAbbreviation")),
        ("volume", data.get("volume")), ("issue", data.get("issue")), ("page", data.get("pages")),
        ("publisher", data.get("publisher") or data.get("institution") or data.get("university")),
        ("publisher-place", data.get("place")), ("edition", data.get("edition")),
        ("collection-title", data.get("series")), ("number", data.get("reportNumber")),
        ("genre", data.get("thesisType") or data.get("reportType")),
        ("DOI", data.get("DOI") or ex.get("DOI")), ("ISBN", data.get("ISBN")), ("ISSN", data.get("ISSN")),
        ("PMID", ex.get("PMID")), ("URL", data.get("url")), ("language", data.get("language")),
    ):
        if value:
            out[csl] = value
    parts = date_parts(data.get("date"))
    if parts:
        out["issued"] = {"date-parts": [parts]}
    return out
