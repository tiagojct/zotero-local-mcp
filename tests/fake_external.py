"""Canned responses for Crossref, PubMed, OpenAlex, Europe PMC, Unpaywall, Open Library and a PDF host."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

PDF = b"%PDF-1.7\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

CROSSREF = {
    "10.1183/13993003.00001-2026": {
        "DOI": "10.1183/13993003.00001-2026", "type": "journal-article",
        "title": ["FeNO in <i>children</i> with asthma"], "subtitle": ["a cohort study"],
        "author": [{"given": "Tiago", "family": "Jacinto"}, {"given": "Ana", "family": "Silva"}],
        "issued": {"date-parts": [[2026, 3]]}, "container-title": ["European Respiratory Journal"],
        "short-container-title": ["Eur Respir J"], "volume": "67", "issue": "3", "page": "2500001",
        "ISSN": ["0903-1936", "1399-3003"], "publisher": "ERS", "URL": "https://doi.org/10.1183/13993003.00001-2026",
        "is-referenced-by-count": 4,
    },
    "10.1000/book.1": {
        "DOI": "10.1000/book.1", "type": "book-chapter", "title": ["Spirometry basics"],
        "author": [{"given": "John", "family": "West"}], "editor": [{"given": "M", "family": "Miller"}],
        "issued": {"date-parts": [[2012]]}, "container-title": ["Lung Function Handbook"],
        "publisher": "Springer", "ISBN": ["978-3-16-148410-0"],
    },
    "10.1000/retracted": {
        "DOI": "10.1000/retracted", "type": "journal-article", "title": ["A retracted paper"],
        "author": [{"given": "X", "family": "Fraud"}], "issued": {"date-parts": [[2020]]},
        "container-title": ["Journal"],
    },
}
CROSSREF_SEARCH = [  # answer to any query.bibliographic
    {"DOI": "10.1016/j.rmed.2019.05.001", "type": "journal-article",
     "title": ["Decision support in lung function"], "author": [{"given": "João", "family": "Fonseca"}],
     "issued": {"date-parts": [[2019, 5]]}, "container-title": ["Respiratory Medicine"],
     "volume": "153", "page": "1-8", "abstract": "<jats:p>We built a CDSS.</jats:p>"},
]
UPDATES = {
    "10.1000/retracted": [{"DOI": "10.1000/retraction-notice", "update-to": [
        {"DOI": "10.1000/retracted", "type": "retraction", "source": "retraction-watch",
         "label": "Retraction", "updated": {"date-parts": [[2022, 1, 5]]}}]}],
    "10.1000/corrected": [{"DOI": "10.1000/erratum", "update-to": [
        {"DOI": "10.1000/corrected", "type": "correction", "source": "publisher",
         "updated": {"date-parts": [[2021, 6]]}}]}],
}
PUBMED = {
    "39000001": {"title": "FeNO in children with asthma: a cohort study.", "doi": "10.1183/13993003.00001-2026",
                 "abstract": [("BACKGROUND", "FeNO matters."), ("RESULTS", "It predicts attacks.")],
                 "authors": [("Jacinto", "Tiago")], "journal": "European Respiratory Journal",
                 "abbr": "Eur Respir J", "year": "2026", "month": "Mar", "volume": "67", "issue": "3",
                 "pages": "2500001", "pmc": "PMC999",
                 "affiliation": "MEDCIDS, Faculty of Medicine, University of Porto, Porto, Portugal. Electronic address: author@example.org."},
    "39000002": {"title": "Spirometry reference equations in older adults.", "doi": "10.1000/new.2",
                 "abstract": [(None, "New GLI data.")], "authors": [("Stanojevic", "Sanja")],
                 "journal": "Thorax", "abbr": "Thorax", "year": "2026", "month": "09", "volume": "81",
                 "issue": "1", "pages": "1-9", "pmc": ""},
}
PUBMED_SEARCH = {"feno": ["39000001", "39000002"], "10.1183/13993003.00001-2026[doi]": ["39000001"]}
OPENALEX = {
    "W1": {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1183/13993003.00001-2026",
           "display_name": "FeNO in children with asthma: a cohort study", "publication_year": 2026,
           "publication_date": "2026-03-01", "type": "article", "cited_by_count": 4,
           "authorships": [{"author": {"display_name": "Tiago Jacinto"}}],
           "primary_location": {"source": {"display_name": "European Respiratory Journal"}},
           "biblio": {"volume": "67", "issue": "3", "first_page": "2500001"},
           "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/39000001"},
           "referenced_works": ["https://openalex.org/W2", "https://openalex.org/W3"],
           "abstract_inverted_index": {"FeNO": [0], "matters": [1]},
           "open_access": {"is_oa": True, "oa_url": "https://oa.example.org/paper.pdf"}, "is_retracted": False},
    "W2": {"id": "https://openalex.org/W2", "doi": "https://doi.org/10.1016/j.rmed.2019.05.001",
           "display_name": "Decision support in lung function", "publication_year": 2019, "type": "article",
           "cited_by_count": 50, "authorships": [{"author": {"display_name": "João Fonseca"}}],
           "referenced_works": []},
    "W3": {"id": "https://openalex.org/W3", "doi": "https://doi.org/10.1000/other",
           "display_name": "Other reference", "publication_year": 2015, "type": "article",
           "cited_by_count": 200, "authorships": [], "referenced_works": []},
    "W4": {"id": "https://openalex.org/W4", "doi": "https://doi.org/10.1000/citing",
           "display_name": "A paper citing ours", "publication_year": 2026, "type": "article",
           "cited_by_count": 1, "authorships": [], "referenced_works": ["https://openalex.org/W1"]},
}
UNPAYWALL = {
    "10.1183/13993003.00001-2026": {"is_oa": True, "best_oa_location": {
        "url_for_pdf": "https://oa.example.org/paper.pdf", "url": "https://oa.example.org/",
        "host_type": "repository", "version": "acceptedVersion", "license": "cc-by"}},
    "10.1000/landing": {"is_oa": True, "best_oa_location": {
        "url_for_pdf": "https://publisher.example.org/landing", "host_type": "publisher",
        "version": "publishedVersion", "license": None}},
}
OPENLIBRARY = {
    "9780199577774": {"title": "Pulmonary Physiology", "subtitle": "The Essentials",
                      "authors": [{"name": "John B. West"}], "publishers": [{"name": "Lippincott"}],
                      "publish_places": [{"name": "Philadelphia"}], "publish_date": "2012",
                      "url": "https://openlibrary.org/books/OL1M"},
}


# Europe PMC: the FeNO paper (same work as PubMed 39000001, open access as PMC999) and a
# preprint that only Europe PMC has.
EUROPEPMC = [
    {"id": "39000001", "source": "MED", "pmid": "39000001", "pmcid": "PMC999", "doi": "10.1183/13993003.00001-2026",
     "title": "FeNO in children with asthma: a cohort study.", "pubYear": "2026", "firstPublicationDate": "2026-03-01",
     "authorList": {"author": [{"lastName": "Jacinto", "firstName": "Tiago"}]},
     "journalInfo": {"journal": {"title": "The European respiratory journal"}},
     "abstractText": "<h4>Background</h4>FeNO matters.", "isOpenAccess": "Y", "inEPMC": "Y", "citedByCount": 5,
     "pubTypeList": {"pubType": ["Journal Article"]}},
    {"id": "PPR1", "source": "PPR", "doi": "10.1101/2026.01.01.000001", "title": "FeNO in preschool wheeze",
     "pubYear": "2026", "authorList": {"author": [{"lastName": "Silva", "firstName": "Ana"}]},
     "isOpenAccess": "N", "inEPMC": "N", "citedByCount": 0},
]
FULLTEXT = {"PMC999": """<?xml version="1.0"?>
<article><front><article-meta><title-group><article-title>FeNO in children with asthma</article-title></title-group>
<abstract><p>FeNO matters.</p></abstract></article-meta></front><body>
<sec sec-type="intro"><title>Background</title><p>Asthma in children.</p></sec>
<sec sec-type="methods"><title>Methods</title><p>We measured FeNO in 120 children.</p>
<fig id="f1"><label>Figure 1</label><caption><p>Study flow.</p></caption></fig></sec>
<sec><title>Results</title><p>FeNO above 35 ppb predicted attacks.</p></sec>
<sec sec-type="conclusions"><title>Discussion</title><p>FeNO helps.</p></sec>
</body><back><ref-list><ref id="r1"/><ref id="r2"/></ref-list></back></article>"""}


def pubmed_xml(ids: list[str]) -> str:
    arts = []
    for pmid in ids:
        r = PUBMED.get(pmid)
        if not r:
            continue
        abstract = "".join(
            f'<AbstractText Label="{lab}">{txt}</AbstractText>' if lab else f"<AbstractText>{txt}</AbstractText>"
            for lab, txt in r["abstract"])
        authors = "".join(f"<Author><LastName>{f}</LastName><ForeName>{g}</ForeName>"
                          + (f"<AffiliationInfo><Affiliation>{r['affiliation']}</Affiliation></AffiliationInfo>"
                             if r.get("affiliation") else "") + "</Author>" for f, g in r["authors"])
        pmc = f'<ArticleId IdType="pmc">{r["pmc"]}</ArticleId>' if r["pmc"] else ""
        arts.append(f"""<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>
<Journal><ISSN>0903-1936</ISSN><JournalIssue><Volume>{r['volume']}</Volume><Issue>{r['issue']}</Issue>
<PubDate><Year>{r['year']}</Year><Month>{r['month']}</Month></PubDate></JournalIssue>
<Title>{r['journal']}</Title><ISOAbbreviation>{r['abbr']}</ISOAbbreviation></Journal>
<ArticleTitle>{r['title']}</ArticleTitle><Pagination><MedlinePgn>{r['pages']}</MedlinePgn></Pagination>
<ELocationID EIdType="doi">{r['doi']}</ELocationID><Abstract>{abstract}</Abstract>
<AuthorList>{authors}</AuthorList><Language>eng</Language>
<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
</Article><MeshHeadingList><MeshHeading><DescriptorName>Asthma</DescriptorName></MeshHeading></MeshHeadingList>
</MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType="pubmed">{pmid}</ArticleId>{pmc}</ArticleIdList></PubmedData>
</PubmedArticle>""")
    return f"<PubmedArticleSet>{''.join(arts)}</PubmedArticleSet>"


class FakeExternal:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        parts = urlsplit(url)
        host, path = parts.netloc, unquote(parts.path)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}

        def js(obj, status=200):
            return httpx.Response(status, json=obj)

        if host == "api.crossref.org":
            if path.startswith("/works/"):
                doi = path[len("/works/"):].lower()
                return js({"message": CROSSREF[doi]}) if doi in CROSSREF else js({"message": "not found"}, 404)
            if "query.bibliographic" in q:
                return js({"message": {"items": CROSSREF_SEARCH}})
            if q.get("filter", "").startswith("updates:"):
                doi = q["filter"][len("updates:"):]
                return js({"message": {"items": UPDATES.get(doi, [])}})
        if host == "eutils.ncbi.nlm.nih.gov":
            if path.endswith("esearch.fcgi"):
                term = q.get("term", "")
                ids = PUBMED_SEARCH.get(term.lower() if "[doi]" not in term else term, [])
                return js({"esearchresult": {"count": str(len(ids)), "idlist": ids}})
            if path.endswith("efetch.fcgi"):
                return httpx.Response(200, text=pubmed_xml(q.get("id", "").split(",")))
        if host == "api.openalex.org":
            if path.startswith("/works/"):
                ident = path[len("/works/"):]
                for w in OPENALEX.values():
                    if ident == w["id"].rsplit("/", 1)[-1] or ident == f"doi:{(w.get('doi') or '').replace('https://doi.org/', '')}" \
                            or ident == f"pmid:{(w.get('ids') or {}).get('pmid', 'x').rsplit('/', 1)[-1]}":
                        return js(w)
                return js({"error": "not found"}, 404)
            flt = q.get("filter", "")
            if flt.startswith("cites:"):
                target = flt[len("cites:"):]
                res = [w for w in OPENALEX.values() if f"https://openalex.org/{target}" in w.get("referenced_works", [])]
                return js({"meta": {"count": len(res)}, "results": res})
            if flt.startswith("openalex_id:"):
                ids = flt[len("openalex_id:"):].split("|")
                res = [OPENALEX[i] for i in ids if i in OPENALEX]
                return js({"meta": {"count": len(res)}, "results": res})
            if "search" in q:
                res = [OPENALEX["W1"], OPENALEX["W3"]]
                return js({"meta": {"count": 2}, "results": res})
        if host == "www.ebi.ac.uk" and path.endswith("/search"):
            query = q.get("query", "")
            if query.startswith("PMCID:"):
                hits = [r for r in EUROPEPMC if r.get("pmcid") == query.split(":", 1)[1]]
            elif query.startswith("EXT_ID:"):
                hits = [r for r in EUROPEPMC if r.get("pmid") == query.split(":", 1)[1].split(" ")[0]]
            elif query.startswith('DOI:"'):
                hits = [r for r in EUROPEPMC if r.get("doi") == query[5:-1].lower()]
            else:
                hits = EUROPEPMC
            return js({"hitCount": len(hits), "resultList": {"result": hits[: int(q.get("pageSize", 25))]}})
        if host == "www.ebi.ac.uk" and path.endswith("/fullTextXML"):
            pmcid = path.split("/")[-2]
            return httpx.Response(200, text=FULLTEXT[pmcid]) if pmcid in FULLTEXT else httpx.Response(500)
        if host == "api.unpaywall.org":
            doi = path[len("/v2/"):].lower()
            return js(UNPAYWALL[doi]) if doi in UNPAYWALL else js({"error": True}, 404)
        if host == "openlibrary.org" and path == "/api/books":
            key = q.get("bibkeys", "")
            isbn = key.split(":", 1)[-1]
            return js({key: OPENLIBRARY[isbn]} if isbn in OPENLIBRARY else {})
        if host == "www.googleapis.com" and path.startswith("/books/v1/volumes"):
            return js({"items": [{"volumeInfo": {
                "title": "Visão", "subtitle": "n.º 1500", "publisher": "Trust in News", "publishedDate": "2022-01-13",
                "printType": "MAGAZINE", "language": "pt", "industryIdentifiers": [{"type": "ISSN", "identifier": "0872-3540"}],
                "infoLink": "https://books.google.com/books?id=x"}}]})
        if host == "www.wikidata.org":
            if q.get("action") == "wbsearchentities":
                return js({"search": [{"id": "Q10380", "label": "Visão", "description": "Portuguese weekly news magazine",
                                       "concepturi": "http://www.wikidata.org/entity/Q10380"}]})
            if q.get("action") == "wbgetentities":
                return js({"entities": {"Q10380": {"claims": {"P236": [{"mainsnak": {"datavalue": {"value": "0872-3540"}}}]}}}})
        if host == "archive.org":
            return js({"response": {"docs": [{"identifier": "visao-1500", "title": ["Visão n.º 1500"],
                                              "date": "2022-01-13T00:00:00Z", "publisher": "Trust in News"}]}})
        if host == "openlibrary.org" and path == "/search.json":
            return js({"docs": [{"key": "/works/OL1W", "title": "Pulmonary physiology", "author_name": ["West, John B."],
                                 "first_publish_year": 1974, "isbn": ["9780683089356"]}]})
        if host == "api.search.brave.com":
            if request.headers.get("X-Subscription-Token") != "brave-ok":
                return js({"error": "bad key"}, 401)
            return js({"web": {"results": [{"title": "Visão <strong>1500</strong>", "url": "https://visao.pt/1500",
                                            "description": "Edição de 13 de janeiro"}]}})
        if host == "oa.example.org":
            return httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"})
        if host == "publisher.example.org":
            return httpx.Response(200, text="<html>Buy this article</html>")
        return httpx.Response(404, text="unknown")
