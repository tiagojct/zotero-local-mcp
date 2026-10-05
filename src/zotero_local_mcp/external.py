"""Clients for public scholarly services: Crossref, PubMed (NCBI E-utilities),
OpenAlex, Europe PMC, Unpaywall and Open Library. All return records (see records.py).

Rules: one shared HTTP client, at most four requests at a time, retry on 429
and 5xx with back-off, and an identifying User-Agent with a contact address
when one is configured (the services' "polite" pools).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import re
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import httpx

from . import APP_NAME, __version__
from .records import norm_doi, norm_isbn

CROSSREF = "https://api.crossref.org"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENALEX = "https://api.openalex.org"
UNPAYWALL = "https://api.unpaywall.org/v2"
OPENLIBRARY = "https://openlibrary.org"
EUROPEPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
MAX_PDF = 60 * 1024 * 1024


class ExternalError(RuntimeError):
    pass


def _first(value: Any) -> str:
    if isinstance(value, list):
        return value[0] if value else ""
    return value or ""


def strip_markup(text: str | None) -> str:
    text = re.sub(r"<jats:title>.*?</jats:title>", "", text or "", flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class External:
    def __init__(self, email: str | None = None, ncbi_api_key: str | None = None,
                 openalex_api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.email = email
        self.ncbi_api_key = ncbi_api_key
        self.openalex_api_key = openalex_api_key
        ua = f"{APP_NAME}/{__version__}" + (f" (mailto:{email})" if email else "")
        self.http = httpx.AsyncClient(timeout=40.0, transport=transport, follow_redirects=True,
                                      headers={"User-Agent": ua})
        self._sem = asyncio.Semaphore(4)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        async with self._sem:
            last: Exception | None = None
            for attempt in range(4):
                try:
                    r = await self.http.get(url, params=params)
                except httpx.HTTPError as exc:
                    last = exc
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code == 429 or r.status_code >= 500:
                    wait = r.headers.get("Retry-After")
                    await asyncio.sleep(float(wait) if wait and wait.isdigit() else 2.0 * (attempt + 1))
                    last = ExternalError(f"HTTP {r.status_code} from {url}")
                    continue
                return r
            raise ExternalError(f"Service unavailable: {url} ({last!r})")

    # ------------------------------------------------------------ Crossref

    def _cr_params(self, extra: dict | None = None) -> dict:
        return {**({"mailto": self.email} if self.email else {}), **(extra or {})}

    async def crossref_work(self, doi: str) -> dict | None:
        r = await self._get(f"{CROSSREF}/works/{quote(doi, safe='/:;()')}", self._cr_params())
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ExternalError(f"Crossref: HTTP {r.status_code}")
        return from_crossref(r.json()["message"])

    async def crossref_search(self, bibliographic: str, rows: int = 3) -> list[dict]:
        r = await self._get(f"{CROSSREF}/works",
                            self._cr_params({"query.bibliographic": bibliographic, "rows": rows}))
        if r.status_code != 200:
            raise ExternalError(f"Crossref search: HTTP {r.status_code}")
        return [from_crossref(m) for m in r.json()["message"].get("items", [])]

    async def crossref_updates(self, doi: str) -> list[dict]:
        """Notices (retraction, correction, expression of concern...) that update this DOI."""
        r = await self._get(f"{CROSSREF}/works",
                            self._cr_params({"filter": f"updates:{doi}", "rows": 20}))
        if r.status_code != 200:
            raise ExternalError(f"Crossref updates: HTTP {r.status_code}")
        out = []
        target = norm_doi(doi)
        for item in r.json()["message"].get("items", []):
            for u in item.get("update-to") or []:
                if norm_doi(u.get("DOI")) == target:
                    upd = (u.get("updated") or {}).get("date-parts") or [[None]]
                    out.append({
                        "type": u.get("type"),
                        "label": u.get("label"),
                        "source": u.get("source"),
                        "notice_doi": item.get("DOI"),
                        "date": "-".join(str(p) for p in upd[0] if p),
                    })
        return out

    # ------------------------------------------------------------ PubMed

    def _ncbi(self, extra: dict) -> dict:
        p = {"tool": APP_NAME, **extra}
        if self.email:
            p["email"] = self.email
        if self.ncbi_api_key:
            p["api_key"] = self.ncbi_api_key
        return p

    async def pubmed_search(self, term: str, retmax: int = 20, reldays: int | None = None,
                            sort: str = "relevance", year_from: int | None = None,
                            year_to: int | None = None) -> tuple[int, list[str]]:
        params: dict[str, Any] = {"db": "pubmed", "term": term, "retmode": "json",
                                  "retmax": retmax, "sort": sort}
        if reldays:
            params.update(reldate=reldays, datetype="edat")
        elif year_from or year_to:
            params.update(datetype="pdat", mindate=str(year_from or 1800), maxdate=str(year_to or 3000))
        r = await self._get(f"{EUTILS}/esearch.fcgi", self._ncbi(params))
        if r.status_code != 200:
            raise ExternalError(f"PubMed search: HTTP {r.status_code}")
        res = r.json().get("esearchresult", {})
        return int(res.get("count", 0)), list(res.get("idlist", []))

    async def pubmed_fetch(self, pmids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(pmids), 100):
            chunk = pmids[i : i + 100]
            r = await self._get(f"{EUTILS}/efetch.fcgi",
                                self._ncbi({"db": "pubmed", "id": ",".join(chunk), "retmode": "xml"}))
            if r.status_code != 200:
                raise ExternalError(f"PubMed fetch: HTTP {r.status_code}")
            out.extend(parse_pubmed_xml(r.text))
        order = {p: n for n, p in enumerate(pmids)}
        return sorted(out, key=lambda rec: order.get(rec.get("pmid") or "", 10**9))

    async def pubmed_for_doi(self, doi: str) -> dict | None:
        _, ids = await self.pubmed_search(f"{doi}[doi]", retmax=1)
        if not ids:
            return None
        recs = await self.pubmed_fetch(ids[:1])
        return recs[0] if recs else None

    # ------------------------------------------------------------ OpenAlex

    def _oa(self, extra: dict | None = None) -> dict:
        p = dict(extra or {})
        if self.email:
            p["mailto"] = self.email
        if self.openalex_api_key:
            p["api_key"] = self.openalex_api_key
        return p

    async def openalex_work(self, ident: str) -> dict | None:
        """ident: OpenAlex id (W...), DOI or pmid:NNN."""
        if re.fullmatch(r"W\d+", ident):
            path = ident
        elif ident.lower().startswith("pmid:"):
            path = f"pmid:{ident[5:]}"
        else:
            doi = norm_doi(ident)
            if not doi:
                raise ExternalError(f"Not an OpenAlex id, DOI or pmid: {ident}")
            path = f"doi:{doi}"
        r = await self._get(f"{OPENALEX}/works/{path}", self._oa())
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ExternalError(f"OpenAlex: HTTP {r.status_code}")
        return from_openalex(r.json())

    async def _openalex_list(self, params: dict) -> tuple[int, list[dict]]:
        r = await self._get(f"{OPENALEX}/works", self._oa(params))
        if r.status_code != 200:
            raise ExternalError(f"OpenAlex: HTTP {r.status_code} {r.text[:200]}")
        body = r.json()
        return int((body.get("meta") or {}).get("count") or 0), [from_openalex(w) for w in body.get("results", [])]

    async def openalex_search(self, query: str, per_page: int = 25, year_from: int | None = None,
                              year_to: int | None = None, sort: str | None = None,
                              since: dt.date | None = None) -> tuple[int, list[dict]]:
        filters = []
        if year_from:
            filters.append(f"from_publication_date:{year_from}-01-01")
        if year_to:
            filters.append(f"to_publication_date:{year_to}-12-31")
        if since:
            filters.append(f"from_publication_date:{since.isoformat()}")
        params: dict[str, Any] = {"search": query, "per_page": min(per_page, 100)}
        if filters:
            params["filter"] = ",".join(filters)
        if sort in ("cited_by_count", "publication_date"):
            params["sort"] = f"{sort}:desc"
        return await self._openalex_list(params)

    async def openalex_cited_by(self, work_id: str, per_page: int = 50) -> tuple[int, list[dict]]:
        return await self._openalex_list({"filter": f"cites:{work_id}", "per_page": min(per_page, 100),
                                          "sort": "cited_by_count:desc"})

    async def openalex_by_ids(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        short = [i.rsplit("/", 1)[-1] for i in ids]
        for n in range(0, len(short), 50):
            chunk = short[n : n + 50]
            _, recs = await self._openalex_list({"filter": "openalex_id:" + "|".join(chunk), "per_page": 50})
            out.extend(recs)
        return out

    # ------------------------------------------------------------ Europe PMC

    def _epmc(self, extra: dict) -> dict:
        return {**extra, "format": "json", **({"email": self.email} if self.email else {})}

    async def europepmc_search(self, query: str, page_size: int = 25, year_from: int | None = None,
                               year_to: int | None = None, open_access: bool = False) -> tuple[int, list[dict]]:
        """Europe PMC search (its query syntax; PubMed, PMC, preprints, patents and more)."""
        q = f"({query})" if (year_from or year_to or open_access) else query
        if year_from or year_to:
            q += f" AND PUB_YEAR:[{year_from or 1800} TO {year_to or 3000}]"
        if open_access:
            q += " AND OPEN_ACCESS:y"
        r = await self._get(f"{EUROPEPMC}/search",
                            self._epmc({"query": q, "resultType": "core", "pageSize": min(page_size, 100)}))
        if r.status_code != 200:
            raise ExternalError(f"Europe PMC search: HTTP {r.status_code}")
        body = r.json()
        return int(body.get("hitCount") or 0), [from_europepmc(x) for x in (body.get("resultList") or {}).get("result", [])]

    async def europepmc_record(self, pmid: str | None = None, pmcid: str | None = None,
                               doi: str | None = None) -> dict | None:
        """One work by PMCID, PMID or DOI, or None. The record says whether full text is open."""
        # PMCID must not be quoted: Europe PMC finds nothing for PMCID:"PMC123".
        query = (f"PMCID:{pmcid.upper()}" if pmcid else f"EXT_ID:{pmid} AND SRC:MED" if pmid
                 else f'DOI:"{doi}"' if doi else "")
        if not query:
            return None
        _, recs = await self.europepmc_search(query, 1)
        if not recs:
            return None
        rec = recs[0]
        # Europe PMC returns its best hit; keep it only when the identifier is the one asked for.
        if (pmcid and rec.get("pmcid", "").upper() != pmcid.upper()) or (pmid and rec.get("pmid") != pmid) \
                or (doi and rec.get("doi") != norm_doi(doi)):
            return None
        return rec

    async def europepmc_fulltext(self, pmcid: str) -> str | None:
        """Open-access full text as JATS XML, or None when Europe PMC has none."""
        r = await self._get(f"{EUROPEPMC}/{quote(pmcid.upper())}/fullTextXML")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ExternalError(f"Europe PMC full text: HTTP {r.status_code}")
        return r.text

    # ------------------------------------------------------------ Unpaywall, Open Library, files

    async def unpaywall(self, doi: str) -> dict | None:
        if not self.email:
            raise ExternalError("Unpaywall needs a contact email. Set ZOTERO_CONTACT_EMAIL.")
        r = await self._get(f"{UNPAYWALL}/{quote(doi, safe='/:;()')}", {"email": self.email})
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise ExternalError(f"Unpaywall: HTTP {r.status_code}")
        body = r.json()
        locs = [loc for loc in [body.get("best_oa_location"), *(body.get("oa_locations") or [])] if loc]
        pdf = next((loc for loc in locs if loc.get("url_for_pdf")), None)
        return {
            "is_oa": body.get("is_oa"),
            "pdf_url": pdf.get("url_for_pdf") if pdf else None,
            "landing_url": (locs[0].get("url") if locs else None),
            "host_type": pdf.get("host_type") if pdf else None,
            "version": pdf.get("version") if pdf else None,
            "license": pdf.get("license") if pdf else None,
        }

    async def openlibrary_isbn(self, isbn: str) -> dict | None:
        key = f"ISBN:{isbn}"
        r = await self._get(f"{OPENLIBRARY}/api/books", {"bibkeys": key, "format": "json", "jscmd": "data"})
        if r.status_code != 200:
            raise ExternalError(f"Open Library: HTTP {r.status_code}")
        data = r.json().get(key)
        return from_openlibrary(data, isbn) if data else None

    async def download_pdf(self, url: str) -> bytes:
        async with self._sem:
            try:
                async with self.http.stream("GET", url, headers={"Accept": "application/pdf"}) as r:
                    if r.status_code != 200:
                        raise ExternalError(f"download failed: HTTP {r.status_code}")
                    chunks, size = [], 0
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_PDF:
                            raise ExternalError("file larger than 60 MB")
                        chunks.append(chunk)
            except httpx.HTTPError as exc:
                raise ExternalError(f"download failed: {exc!r}") from exc
        data = b"".join(chunks)
        if not data.lstrip()[:5].startswith(b"%PDF"):
            raise ExternalError("the link did not return a PDF (probably a publisher page)")
        return data


# ---------------------------------------------------------------- parsers

CROSSREF_KIND = {"posted-content": "preprint"}


def from_crossref(m: dict) -> dict:
    authors = [
        {"family": a.get("family", ""), "given": a.get("given", ""), "name": a.get("name", ""), "role": role}
        for role, key in (("author", "author"), ("editor", "editor"))
        for a in m.get(key) or []
    ]
    parts = ((m.get("issued") or m.get("published-print") or m.get("published-online") or {})
             .get("date-parts") or [[None]])[0]
    parts = [p for p in parts if p]
    date = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts))
    title = _first(m.get("title"))
    subtitle = _first(m.get("subtitle"))
    if subtitle and subtitle.lower() not in title.lower():
        title = f"{title}: {subtitle}"
    kind = m.get("type") or ""
    if kind == "posted-content" and m.get("subtype") not in (None, "preprint"):
        kind = "document"
    return {
        "source": "crossref",
        "kind": CROSSREF_KIND.get(kind, kind),
        "title": strip_markup(title),
        "authors": authors,
        "date": date,
        "year": str(parts[0]) if parts else "",
        "container": _first(m.get("container-title")),
        "container_abbr": _first(m.get("short-container-title")),
        "volume": m.get("volume", ""),
        "issue": m.get("issue", ""),
        "pages": m.get("page", ""),
        "doi": norm_doi(m.get("DOI")),
        "isbn": norm_isbn(_first(m.get("ISBN"))),
        "issn": ", ".join(m.get("ISSN") or []),
        "publisher": m.get("publisher", ""),
        "abstract": strip_markup(m.get("abstract")),
        "url": m.get("URL", ""),
        "language": m.get("language", ""),
        "cited_by": m.get("is-referenced-by-count"),
    }


def _text(el: ET.Element | None) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


MONTH = {m: f"{i:02d}" for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def parse_pubmed_xml(text: str) -> list[dict]:
    root = ET.fromstring(text)
    out = []
    for art in root.iter("PubmedArticle"):
        mc = art.find("MedlineCitation")
        a = mc.find("Article") if mc is not None else None
        if a is None:
            continue
        pmid = mc.findtext("PMID") or ""
        abstract = []
        for at in a.findall("Abstract/AbstractText"):
            label = at.get("Label")
            body = _text(at)
            abstract.append(f"{label}: {body}" if label else body)
        authors = []
        contacts = []
        for au in a.findall("AuthorList/Author"):
            who = " ".join(x for x in (au.findtext("ForeName"), au.findtext("LastName")) if x) or au.findtext("CollectiveName") or ""
            for aff in au.findall("AffiliationInfo/Affiliation"):
                for email in EMAIL_RE.findall(_text(aff)):
                    contacts.append({"author": who, "email": email.rstrip(".").lower(), "source": "PubMed affiliation"})
            if au.findtext("CollectiveName"):
                authors.append({"name": au.findtext("CollectiveName"), "role": "author"})
            elif au.findtext("LastName"):
                authors.append({"family": au.findtext("LastName"),
                                "given": au.findtext("ForeName") or au.findtext("Initials") or "",
                                "role": "author"})
        ji = a.find("Journal/JournalIssue")
        pd = ji.find("PubDate") if ji is not None else None
        year = (pd.findtext("Year") if pd is not None else "") or ""
        if not year and pd is not None:
            m = re.search(r"\d{4}", pd.findtext("MedlineDate") or "")
            year = m.group(0) if m else ""
        date = year
        if pd is not None and year:
            mon = pd.findtext("Month") or ""
            mon = MONTH.get(mon[:3], mon if mon.isdigit() else "")
            if mon:
                date += f"-{int(mon):02d}"
                if pd.findtext("Day"):
                    date += f"-{int(pd.findtext('Day')):02d}"
        doi = ""
        for e in a.findall("ELocationID"):
            if e.get("EIdType") == "doi":
                doi = e.text or ""
        pmcid = ""
        for e in art.findall("PubmedData/ArticleIdList/ArticleId"):
            if e.get("IdType") == "doi" and not doi:
                doi = e.text or ""
            if e.get("IdType") == "pmc":
                pmcid = e.text or ""
        out.append({
            "source": "pubmed",
            "kind": "journal-article",
            "title": _text(a.find("ArticleTitle")).rstrip("."),
            "authors": authors,
            "date": date,
            "year": year,
            "container": a.findtext("Journal/Title") or "",
            "container_abbr": a.findtext("Journal/ISOAbbreviation") or "",
            "volume": ji.findtext("Volume") if ji is not None else "",
            "issue": ji.findtext("Issue") if ji is not None else "",
            "pages": a.findtext("Pagination/MedlinePgn") or "",
            "doi": norm_doi(doi),
            "pmid": pmid,
            "pmcid": pmcid,
            "issn": a.findtext("Journal/ISSN") or "",
            "abstract": "\n\n".join(abstract),
            "language": a.findtext("Language") or "",
            "publication_types": [pt.text for pt in a.findall("PublicationTypeList/PublicationType")],
            "contacts": contacts,
        })
    return out


EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _inverted_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    pos = [(i, w) for w, idx in inv.items() for i in idx]
    return " ".join(w for _, w in sorted(pos))


def from_openalex(w: dict) -> dict:
    loc = w.get("primary_location") or {}
    src = loc.get("source") or {}
    bib = w.get("biblio") or {}
    pages = "-".join(p for p in (bib.get("first_page"), bib.get("last_page")) if p)
    pmid = ((w.get("ids") or {}).get("pmid") or "").rsplit("/", 1)[-1]
    authors = []
    for au in w.get("authorships") or []:
        name = (au.get("author") or {}).get("display_name") or au.get("raw_author_name") or ""
        if name:
            authors.append({"name": name, "role": "author"})
    return {
        "source": "openalex",
        "kind": w.get("type") or "",
        "title": w.get("display_name") or w.get("title") or "",
        "authors": authors,
        "date": w.get("publication_date") or "",
        "year": str(w.get("publication_year") or ""),
        "container": src.get("display_name") or "",
        "volume": bib.get("volume") or "",
        "issue": bib.get("issue") or "",
        "pages": pages,
        "doi": norm_doi(w.get("doi")),
        "pmid": pmid,
        "abstract": _inverted_abstract(w.get("abstract_inverted_index")),
        "cited_by": w.get("cited_by_count"),
        "oa_url": (w.get("open_access") or {}).get("oa_url"),
        "openalex_id": (w.get("id") or "").rsplit("/", 1)[-1],
        "referenced_works": w.get("referenced_works") or [],
        "is_retracted": w.get("is_retracted"),
    }


def from_europepmc(r: dict) -> dict:
    authors = []
    for a in ((r.get("authorList") or {}).get("author") or []):
        if a.get("lastName"):
            authors.append({"family": a["lastName"], "given": a.get("firstName") or a.get("initials") or "",
                            "role": "author"})
        elif a.get("collectiveName") or a.get("fullName"):
            authors.append({"name": a.get("collectiveName") or a.get("fullName"), "role": "author"})
    journal = (r.get("journalInfo") or {}).get("journal") or {}
    types = (r.get("pubTypeList") or {}).get("pubType") or []
    source = r.get("source") or ""
    return {
        "source": "europepmc",
        "kind": "preprint" if source == "PPR" else "journal-article",
        "title": strip_markup(r.get("title")).rstrip("."),
        "authors": authors,
        "date": r.get("firstPublicationDate") or "",
        "year": str(r.get("pubYear") or ""),
        "container": journal.get("title") or r.get("bookOrReportDetails", {}).get("publisher", "") or "",
        "doi": norm_doi(r.get("doi")),
        "pmid": r.get("pmid") or "",
        "pmcid": (r.get("pmcid") or "").upper(),
        "abstract": strip_markup(r.get("abstractText")),
        "cited_by": r.get("citedByCount"),
        "open_access": r.get("isOpenAccess") == "Y" and r.get("inEPMC") == "Y",
        "publication_types": types if isinstance(types, list) else [types],
        "europepmc_id": f"{source}/{r.get('id')}" if source and r.get("id") else "",
    }


# Section classification and the JATS reading below follow Feynman's Europe PMC
# full-text tool (MIT, Copyright (c) 2026 Companion, Inc., commit 39f3ece);
# see THIRD_PARTY_NOTICES.

def classify_section(title: str, sec_type: str = "") -> str:
    """IMRaD class of a section. The title wins over sec-type: some publishers mark a
    Discussion section sec-type="conclusions"."""
    by_title = _classify_title(title)
    if by_title != "other":
        return by_title
    by_type = {"conclusion": "conclusion", "conclusions": "conclusion", "discussion": "discussion",
               "intro": "introduction", "introduction": "introduction", "methods": "methods",
               "materials|methods": "methods", "results": "results"}
    return by_type.get(sec_type.strip().lower(), "other")


def _classify_title(title: str) -> str:
    t = re.sub(r"^[0-9ivx]+[.):\s]+", "", title.strip().lower())
    if re.match(r"(introduction|background)\b", t):
        return "introduction"
    if re.match(r"(materials?\s+and\s+methods?|methods?|methodology|experimental procedures?|online methods)\b", t):
        return "methods"
    if re.match(r"results?\s+and\s+discussion\b", t):
        return "results_and_discussion"
    if re.match(r"(results?|findings)\b", t):
        return "results"
    if re.match(r"discussion\b", t):
        return "discussion"
    if re.match(r"(conclusions?|summary)\b", t):
        return "conclusion"
    return "other"


def _jats_text(el: ET.Element | None, skip: tuple[str, ...] = ()) -> str:
    if el is None:
        return ""
    parts: list[str] = []

    def walk(e: ET.Element) -> None:
        if e.tag in skip:
            if e.tail:
                parts.append(e.tail)
            return
        if e.text:
            parts.append(e.text)
        for child in e:
            walk(child)
        if e.tail and e is not el:
            parts.append(e.tail)

    walk(el)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def parse_jats(xml: str) -> dict:
    """Title, abstract, top-level sections (title, IMRaD class, full text), captions and counts."""
    root = ET.fromstring(xml)
    meta = root.find(".//front/article-meta")
    body = root.find(".//body")
    sections = []
    for i, sec in enumerate(body.findall("sec") if body is not None else []):
        title = _jats_text(sec.find("title"))
        text = _jats_text(sec, skip=("fig", "table-wrap", "ref-list", "title"))
        sections.append({"index": i, "title": title, "imrad": classify_section(title, sec.get("sec-type") or ""),
                         "chars": len(text), "text": text})
    if body is not None and not sections:  # a body of paragraphs without sections
        text = _jats_text(body, skip=("fig", "table-wrap", "ref-list"))
        if text:
            sections.append({"index": 0, "title": "", "imrad": "other", "chars": len(text), "text": text})

    def captions(tag: str) -> list[dict]:
        return [{"label": _jats_text(n.find("label")), "caption": _jats_text(n.find("caption"))[:600]}
                for n in root.iter(tag)][:8]

    ref_list = root.find(".//back/ref-list")
    return {
        "title": _jats_text(meta.find("title-group/article-title")) if meta is not None else "",
        "abstract": _jats_text(meta.find("abstract")) if meta is not None else "",
        "sections": sections,
        "figures": captions("fig"),
        "tables": captions("table-wrap"),
        "n_figures": sum(1 for _ in root.iter("fig")),
        "n_tables": sum(1 for _ in root.iter("table-wrap")),
        "n_references": len(ref_list.findall("ref")) if ref_list is not None else 0,
    }


def from_openlibrary(d: dict, isbn: str) -> dict:
    authors = []
    for a in d.get("authors") or []:
        name = a.get("name") or ""
        if "," in name:
            family, given = [x.strip() for x in name.split(",", 1)]
        else:
            bits = name.rsplit(" ", 1)
            family, given = (bits[-1], bits[0]) if len(bits) == 2 else (name, "")
        authors.append({"family": family, "given": given, "role": "author"})
    title = d.get("title") or ""
    if d.get("subtitle"):
        title = f"{title}: {d['subtitle']}"
    m = re.search(r"\d{4}", d.get("publish_date") or "")
    return {
        "source": "openlibrary",
        "kind": "book",
        "title": title,
        "authors": authors,
        "date": m.group(0) if m else "",
        "year": m.group(0) if m else "",
        "publisher": ", ".join(p.get("name", "") for p in d.get("publishers") or []),
        "place": ", ".join(p.get("name", "") for p in d.get("publish_places") or []),
        "isbn": isbn,
        "url": d.get("url") or "",
        "num_pages": d.get("number_of_pages"),
    }
