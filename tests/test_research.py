from __future__ import annotations

import datetime as dt
import json
import os
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from fake_external import PDF, FakeExternal
from zotero_local_mcp import alerts
from zotero_local_mcp.client import ZoteroError
from zotero_local_mcp.config import Settings
from zotero_local_mcp.external import External
from zotero_local_mcp.library import Library
from zotero_local_mcp.manage import Librarian, ids_in_line
from zotero_local_mcp.records import detect, norm_doi, norm_isbn, zotero_to_csl
from zotero_local_mcp.scholar import ReadOnlyZotero, Scholar


@pytest.fixture
def fx():
    return FakeExternal()


@pytest.fixture
def ext(fx):
    return External(email="test@example.org", transport=fx.transport())


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / "Inbox").mkdir(parents=True)
    (v / "Systems").mkdir()
    return v


@pytest.fixture
def settings(lib, vault):
    s = lib.s
    return Settings(api_url=s.api_url, vocab_path=s.vocab_path, marker=s.marker, state_dir=s.state_dir,
                    auth_timeout=5, email="test@example.org", vault=vault,
                    alerts_config=vault / "Systems" / "Literature alerts.md")


@pytest.fixture
def librarian(lib, ext, settings):
    lib.s = settings
    return Librarian(lib, ext)


@pytest.fixture
def scholar(fake, ext, settings):
    from zotero_local_mcp.scholar import NoteOnlyZotero
    ro = NoteOnlyZotero(settings.api_url, settings.state_dir, 5, transport=fake.transport())
    return Scholar(settings, ext, Library(settings, ro))


def new_items(fake, before):
    return [i for k, i in fake.items.items() if k not in before]


# ---------------------------------------------------------------- identifiers and conversions

def test_identifier_detection():
    assert detect("https://doi.org/10.1183/13993003.00001-2026") == ("doi", "10.1183/13993003.00001-2026")
    assert detect("pmid:39000001") == ("pmid", "39000001")
    assert detect("39000001") == ("pmid", "39000001")
    assert detect("isbn:0-19-957777-X")[0] == "isbn"
    assert detect("978-0-19-957777-4") == ("isbn", "9780199577774")
    assert detect("nonsense") is None
    assert norm_isbn("0199577773") == "9780199577774"
    assert norm_doi("doi: 10.1000/ABC.") == "10.1000/abc"
    assert ids_in_line("- [x] Title. doi:10.1000/x.1 pmid:5") == "10.1000/x.1"
    assert ids_in_line("- [x] Title. pmid:5") == "pmid:5"


def test_csl_conversion():
    data = {"itemType": "journalArticle", "title": "T", "date": "May 2019",
            "creators": [{"creatorType": "author", "lastName": "Fonseca", "firstName": "João"},
                         {"creatorType": "author", "name": "GLI Network"}],
            "publicationTitle": "Resp Med", "volume": "1", "pages": "1-8", "DOI": "10.1/x",
            "extra": "PMID: 12"}
    csl = zotero_to_csl(data, "fonseca2019")
    assert csl["id"] == "fonseca2019" and csl["type"] == "article-journal"
    assert csl["issued"] == {"date-parts": [[2019, 5]]}
    assert csl["author"] == [{"family": "Fonseca", "given": "João"}, {"literal": "GLI Network"}]
    assert csl["PMID"] == "12" and csl["container-title"] == "Resp Med"


# ---------------------------------------------------------------- imports

async def test_import_doi_with_pubmed_abstract(librarian, fake):
    before = set(fake.items)
    prev = await librarian.import_identifiers(["https://doi.org/10.1183/13993003.00001-2026"])
    assert prev["dry_run"] and fake.write_requests == 0
    row = prev["would_import"][0]
    assert row["type"] == "journalArticle" and row["citekey"] == "jacinto2026"
    assert row["pmid"] == "39000001" and row["has_abstract"]
    res = await librarian.import_identifiers(["10.1183/13993003.00001-2026"], dry_run=False)
    assert res["imported"][0]["citekey"] == "jacinto2026" and res["journal_id"]
    item = new_items(fake, before)[0]
    assert item["title"] == "FeNO in children with asthma: a cohort study"
    assert item["DOI"] == "10.1183/13993003.00001-2026"
    assert item["publicationTitle"] == "European Respiratory Journal" and item["date"] == "2026-03"
    assert "BACKGROUND: FeNO matters." in item["abstractNote"]
    assert item["extra"].splitlines() == ["Citation Key: jacinto2026", "PMID: 39000001", "PMCID: PMC999"]
    assert item["tags"] == [{"tag": "_agent"}]  # no MeSH
    # Undo trashes the new item
    await librarian.lib.undo(dry_run=False)
    assert item["deleted"] is True


async def test_import_skips_duplicates(librarian, fake):
    await librarian.import_identifiers(["10.1183/13993003.00001-2026"], dry_run=False)
    res = await librarian.import_identifiers(["10.1183/13993003.00001-2026", "pmid:39000001", "bad-id"])
    assert res["would_import"] == []
    assert res["skipped"]["10.1183/13993003.00001-2026"].startswith("already in library")
    assert res["skipped"]["pmid:39000001"].startswith("already in library")
    assert res["errors"]["bad-id"] == "not a DOI, PMID or ISBN"


async def test_same_work_twice_in_one_list(librarian):
    res = await librarian.import_identifiers(["pmid:39000001", "10.1183/13993003.00001-2026"])
    assert len(res["would_import"]) == 1
    assert "same work" in next(iter(res["skipped"].values()))


async def test_import_book_chapter_and_missing(librarian, fake):
    before = set(fake.items)
    res = await librarian.import_identifiers(["isbn:9780199577774", "10.1000/book.1", "10.1000/missing"],
                                             dry_run=False)
    assert len(res["imported"]) == 2 and "not found" in res["errors"]["10.1000/missing"]
    items = {i["itemType"]: i for i in new_items(fake, before)}
    book = items["book"]
    assert book["ISBN"] == "9780199577774" and book["publisher"] == "Lippincott"
    assert book["creators"] == [{"creatorType": "author", "lastName": "West", "firstName": "John B."}]
    assert "west2012" in book["extra"]  # west1974 exists; different year
    chap = items["bookSection"]
    assert chap["bookTitle"] == "Lung Function Handbook"
    assert {"creatorType": "editor", "lastName": "Miller", "firstName": "M"} in chap["creators"]
    assert "DOI: 10.1000/book.1" in chap["extra"]  # bookSection template has no DOI field


async def test_import_tags_and_collection_checked(librarian, fake):
    with pytest.raises(ZoteroError, match="not in the vocabulary"):
        await librarian.import_identifiers(["pmid:39000002"], tags=["made-up"])
    ck = fake.add_collection("Inbox papers")
    before = set(fake.items)
    await librarian.import_identifiers(["pmid:39000002"], collection_key=ck, tags=["status/to-read"],
                                       dry_run=False)
    item = new_items(fake, before)[0]
    assert item["collections"] == [ck]
    assert {"tag": "status/to-read"} in item["tags"]
    assert item["journalAbbreviation"] == "Thorax" and item["date"] == "2026-09"


async def test_import_queue_marks_lines(librarian, fake, vault):
    q = vault / "Inbox" / "Zotero import queue.md"
    q.write_text("Header\n\n- [x] doi:10.1183/13993003.00001-2026 | Jacinto 2026\n"
                 "- [ ] pmid:39000002 | not ticked\n- [x] no identifier here\n"
                 "- [x] doi:10.1016/j.rmed.2019.05.001 (already in library)\n")
    prev = await librarian.import_queue()
    assert len(prev["would_import"]) == 1 and "line 5" in prev["problems"]
    res = await librarian.import_queue(dry_run=False)
    assert res["imported"][0]["citekey"] == "jacinto2026"
    lines = q.read_text().splitlines()
    assert lines[2].endswith("(imported: jacinto2026)") and lines[3].startswith("- [ ] pmid")


# ---------------------------------------------------------------- audit, repair, duplicates

async def test_audit(librarian):
    res = await librarian.audit()
    assert res["items_checked"] == 4
    assert res["problem_counts"]["no DOI"] == 3
    book = next(r for r in res["items"] if r["key"] == "CCCC3333")
    assert set(book["problems"]) == {"no ISBN", "no publisher"}
    assert (await librarian.audit(item_type="book"))["items_checked"] == 1
    assert all("no abstract" in r["problems"] for r in (await librarian.audit(problem="no abstract"))["items"])


async def test_repair_by_title_search(librarian, fake):
    prev = await librarian.repair(["DDDD4444", "AAAA1111"])
    assert prev["matches"]["DDDD4444"]["confidence"] >= 0.9
    assert "below 0.9" in prev["skipped"]["AAAA1111"]
    ch = prev["changes"][0]
    assert ch["DOI"]["after"] == "10.1016/j.rmed.2019.05.001"
    assert "date" not in ch  # filled fields only; the existing date stays
    res = await librarian.repair(["DDDD4444"], dry_run=False)
    d = fake.items["DDDD4444"]
    assert res["applied"] == 1 and d["DOI"] == "10.1016/j.rmed.2019.05.001"
    assert d["publicationTitle"] == "Respiratory Medicine" and d["abstractNote"] == "We built a CDSS."
    assert d["date"] == "2019-05" and d["pages"] == "1-8"


async def test_duplicates(librarian, fake):
    fake.items["AAAA1111"]["DOI"] = "10.1234/dup"
    fake.add_item(key="EEEE5555", title="Another record", DOI="https://doi.org/10.1234/DUP",
                  dateAdded="2025-06-01T00:00:00Z")
    fake.add_item(key="FFFF6666", title="Pulmonary Physiology", date="1974", itemType="book",
                  creators=[{"creatorType": "author", "lastName": "West"}])
    fake.add_item(key="GGGG7777", title="Pulmonary Physiology", date="1974", itemType="book",
                  creators=[{"creatorType": "author", "lastName": "Other"}])
    res = await librarian.duplicates()
    groups = {tuple(sorted(i["key"] for i in g["items"])): g["matched_by"] for g in res["duplicates"]}
    assert groups[("AAAA1111", "EEEE5555")] == ["DOI"]
    assert groups[("CCCC3333", "FFFF6666")] == ["title, year and first author"]
    assert not any("GGGG7777" in k for k in groups)


async def test_retractions_and_cache(librarian, fake, fx):
    fake.items["AAAA1111"]["DOI"] = "10.1000/retracted"
    fake.items["BBBB2222"]["DOI"] = "10.1000/corrected"
    res = await librarian.retractions()
    assert [r["key"] for r in res["retracted_or_concern"]] == ["AAAA1111"]
    assert res["retracted_or_concern"][0]["notices"][0]["source"] == "retraction-watch"
    assert [r["key"] for r in res["corrections_and_other"]] == ["BBBB2222"]
    n = len(fx.requests)
    again = await librarian.retractions()
    assert len(fx.requests) == n and again["checked_now"] == 0


# ---------------------------------------------------------------- PDFs

async def test_missing_and_attach_pdfs(librarian, fake):
    fake.items["AAAA1111"].update(DOI="10.1183/13993003.00001-2026", extra="Citation Key: jacinto2026")
    fake.items["BBBB2222"]["DOI"] = "10.1000/landing"
    miss = await librarian.missing_pdfs(with_doi_only=True)
    assert miss["total"] == 2
    prev = await librarian.attach_oa_pdfs(["AAAA1111", "BBBB2222", "CCCC3333"])
    assert [f["key"] for f in prev["would_attach"]] == ["AAAA1111", "BBBB2222"]
    assert prev["skipped"]["CCCC3333"] == "no DOI"
    res = await librarian.attach_oa_pdfs(["AAAA1111", "BBBB2222"], dry_run=False)
    assert len(res["attached"]) == 1 and "not return a PDF" in res["skipped"]["BBBB2222"]
    att = fake.items[res["attached"][0]["attachment"]]
    assert att["parentItem"] == "AAAA1111" and att["filename"] == "jacinto2026.pdf"
    assert fake.files[att["key"]] == PDF
    again = await librarian.attach_oa_pdfs(["AAAA1111"])
    assert again["skipped"]["AAAA1111"] == "already has a PDF"
    assert (await librarian.missing_pdfs(with_doi_only=True))["total"] == 1


# ---------------------------------------------------------------- researcher

async def test_researcher_cannot_write(scholar):
    res = await scholar.lib.tag_items([{"key": "AAAA1111", "add": ["topic/asthma"]}], dry_run=False)
    assert res["applied"] == 0 and "only add new notes" in res["error"]
    with pytest.raises(ZoteroError, match="only add new notes"):
        await scholar.lib.create_collection("x", dry_run=False)
    res = await scholar.lib.trash_items(["AAAA1111"], dry_run=False)
    assert res["applied"] == 0 and "only add new notes" in res["error"]


async def test_search_and_graph_flag_library_items(scholar, fake):
    fake.items["AAAA1111"].update(DOI="10.1183/13993003.00001-2026", extra="Citation Key: jacinto2026")
    fake.items["DDDD4444"]["DOI"] = "10.1016/j.rmed.2019.05.001"
    res = await scholar.search_pubmed("FeNO")
    assert res["returned"] == 2 and res["already_in_library"] == 1
    assert res["results"][0]["in_library"]["citekey"] == "jacinto2026"
    assert "RESULTS: It predicts attacks." in res["results"][0]["abstract"]
    oa = await scholar.search_openalex("feno")
    assert oa["results"][0]["in_library"]["key"] == "AAAA1111"
    g = await scholar.citation_graph("AAAA1111")
    assert g["references"]["total"] == 2 and g["references"]["in_library"] == 1
    assert g["references"]["missing_from_library"][0]["doi"] == "10.1000/other"
    assert g["cited_by"]["total"] == 1 and g["cited_by"]["most_cited"][0]["doi"] == "10.1000/citing"
    work = await scholar.get_work("10.1183/13993003.00001-2026")
    assert work["cited_by"] == 4 and work["open_access"] and work["in_library"]


async def test_manuscript_check_and_export(scholar, fake, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    fake.items["AAAA1111"]["extra"] = "Citation Key: jacinto2026"
    ms = tmp_path / "paper.qmd"
    ms.write_text("---\ntitle: X\nbibliography: refs.json\n---\n"
                  "FeNO helps [@jacinto2026; @jacinto2025] (see @fig-1). Mail me at a@b.com. "
                  "@west1974 disagreed. doi 10.1000/other.\n")
    res = await scholar.check_manuscript(str(ms))
    assert [f["citekey"] for f in res["found"]] == ["jacinto2026", "west1974"]
    assert res["missing"] == [{"citekey": "jacinto2025", "similar_in_library": ["jacinto2026"]}]
    assert res["dois_in_text"] == [{"doi": "10.1000/other", "in_library": None}]
    assert res["bibliography"]["exists"] is False
    out = await scholar.export_bibliography(str(tmp_path / "refs.json"), manuscript=str(ms))
    assert out["entries"] == 2 and out["missing_citekeys"] == ["jacinto2025"]
    ids = [e["id"] for e in json.loads((tmp_path / "refs.json").read_text())]
    assert ids == ["jacinto2026", "west1974"]
    with pytest.raises(ZoteroError, match="overwrite"):
        await scholar.export_bibliography(str(tmp_path / "refs.json"), citekeys=["west1974"])
    res = await scholar.check_manuscript(str(ms))
    assert res["bibliography"]["missing_from_bibliography"] == ["jacinto2025"]
    with pytest.raises(ZoteroError, match="home folder"):
        await scholar.export_bibliography("/etc/refs.json", citekeys=["west1974"])


async def test_queue_imports_then_librarian_imports(scholar, librarian, fake, vault):
    fake.items["DDDD4444"]["DOI"] = "10.1016/j.rmed.2019.05.001"
    res = await scholar.queue_imports([
        {"identifier": "10.1183/13993003.00001-2026", "label": "Jacinto 2026: FeNO", "reason": "key cohort"},
        {"identifier": "10.1016/j.rmed.2019.05.001", "label": "in library"},
        {"identifier": "pmid:39000002", "label": "Stanojevic 2026"},
    ])
    assert res["added"] == ["doi:10.1183/13993003.00001-2026", "pmid:39000002"]
    assert "already in library" in res["skipped"]["10.1016/j.rmed.2019.05.001"]
    again = await scholar.queue_imports([{"identifier": "pmid:39000002"}])
    assert again["skipped"]["pmid:39000002"] == "already in the queue"
    q = vault / "Inbox" / "Zotero import queue.md"
    q.write_text(q.read_text().replace("- [ ] pmid:39000002", "- [x] pmid:39000002"))
    out = await librarian.import_queue(dry_run=False)
    assert [i["identifier"] for i in out["imported"]] == ["pmid:39000002"]
    assert "(imported: stanojevic2026)" in q.read_text()


# ---------------------------------------------------------------- alerts

async def test_alerts(settings, ext, fake, vault, lib):
    fake.items["AAAA1111"]["DOI"] = "10.1183/13993003.00001-2026"
    settings.alerts_config.write_text(
        "---\ndays: 7\n---\nSaved searches.\n\n## PubMed\n\n- `FeNO` FeNO in asthma\n\n"
        "## OpenAlex\n\n- `feno` FeNO (OpenAlex)\n")
    ro = Library(settings, ReadOnlyZotero(settings.api_url, settings.state_dir, 5, transport=fake.transport()))
    day = dt.date(2026, 9, 28)
    res = await alerts.run(settings, today=day, ext=ext, lib=ro)
    note = vault / "Inbox" / "Literature alerts 2026-09-28.md"
    text = note.read_text()
    assert res["new_works"] == 2  # PubMed 39000002, OpenAlex W3; W1/39000001 are in the library
    assert "- [ ] Spirometry reference equations in older adults. Stanojevic. Thorax 2026. doi:10.1000/new.2 pmid:39000002" in text
    assert "doi:10.1000/other" in text and "[[Literature alerts]]" in text
    res2 = await alerts.run(settings, today=day + dt.timedelta(days=7), ext=ext, lib=ro)
    assert res2["new_works"] == 0 and res2["note"] is None


# ---------------------------------------------------------------- MCP wiring

async def test_scholar_server_lists_tools(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "zotero_local_mcp.scholar_server"],
                                   env={**os.environ, "ZOTERO_MCP_STATE": str(tmp_path)})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
    assert names == {"search_pubmed", "search_openalex", "get_work", "citation_graph", "library_lookup",
                     "check_manuscript", "export_bibliography", "queue_imports", "attach_note"}


async def test_librarian_server_lists_new_tools(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "zotero_local_mcp.server"],
                                   env={**os.environ, "ZOTERO_MCP_STATE": str(tmp_path)})
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
    assert {"import_identifiers", "import_queue", "audit_metadata", "repair_metadata", "find_duplicates",
            "check_retractions", "missing_pdfs", "attach_oa_pdfs", "tag_items", "get_fulltext"} <= names
    assert not {"search_pubmed", "citation_graph"} & names


# ---------------------------------------------------------------- second review findings

def test_identifier_edge_cases():
    wiley = "10.1002/(SICI)1097-4636(199706)35:4<463::AID-JBM6>3.0.CO;2-9"
    assert norm_doi(wiley) == wiley.lower()
    assert detect("arXiv:2101.12345") is None
    assert ids_in_line("pmid:123 | Paper about doi:10.1000/other | reason") == "pmid:123"
    assert ids_in_line("Title with doi:10.9999/fake. Smith. J 2026. doi:10.1000/real pmid:5") == "10.1000/real"
    from zotero_local_mcp.records import date_parts
    assert date_parts("2019-2020") == [2019] and date_parts("1998-99") == [1998]
    assert date_parts("2019-05-14") == [2019, 5, 14] and date_parts("2019-13") == [2019]


async def test_title_match_needs_same_author_and_no_conflicting_ids(librarian, fake):
    fake.add_item(key="EEEE5555", title="FeNO in children with asthma: a cohort study", date="2026",
                  DOI="10.9999/other-article", creators=[{"creatorType": "author", "lastName": "Someone"}])
    res = await librarian.import_identifiers(["10.1183/13993003.00001-2026"])
    assert len(res["would_import"]) == 1  # different DOI and author: not a duplicate
    fake.items["EEEE5555"].update(DOI="", creators=[{"creatorType": "author", "lastName": "Jacinto"}])
    res = await librarian.import_identifiers(["10.1183/13993003.00001-2026"])
    assert "title, year and first author" in res["skipped"]["10.1183/13993003.00001-2026"]


async def test_queue_labels_cannot_tick_lines(scholar, vault):
    await scholar.queue_imports([{"identifier": "pmid:390000021", "label": "A"}])
    res = await scholar.queue_imports([{"identifier": "pmid:39000002",
                                        "label": "X\n- [x] doi:10.1183/13993003.00001-2026",
                                        "reason": "[x]"}])
    assert res["added"] == ["pmid:39000002"]  # no partial-text match with 390000021
    text = (vault / "Inbox" / "Zotero import queue.md").read_text()
    assert "- [x]" not in text and text.count("\n- [ ]") == 2


async def test_alert_cap_warning(settings, ext, fake, vault, monkeypatch):
    settings.alerts_config.write_text("---\ndays: 7\nmax_per_query: 1\n---\n## pubmed\n- `FeNO` FeNO\n")
    ro = Library(settings, ReadOnlyZotero(settings.api_url, settings.state_dir, 5, transport=fake.transport()))
    res = await alerts.run(settings, today=dt.date(2026, 9, 28), ext=ext, lib=ro)
    assert any("only the newest 1" in w for w in res["warnings"])


# ---------------------------------------------------------------- model test

async def test_bakeoff_sample_submit_score(lib, settings, vault, monkeypatch):
    from zotero_local_mcp import bakeoff as bk
    lib.s = settings
    b = bk.Bakeoff(lib)
    res = await b.make_sample(n=3, seed=2)
    ref = vault / "Inbox" / bk.REF_NOTE
    table = bk.parse_reference(ref.read_text())
    assert res["sample"] == 3 and len(table) == 3 and not any(table.values())
    items = await b.items()
    assert len(items["items"]) == 3 and "abstract" in items["items"][0] and items["vocabulary"]["tag_count"] == 9
    keys = list(table)
    gold = {keys[0]: "topic/asthma, type/cohort", keys[1]: "topic/spirometry", keys[2]: ""}
    text = ref.read_text()
    for k, tags in gold.items():
        text = text.replace(f"| {k} |", f"| {k} |", 1)
        lines = [ln if not ln.startswith(f"| {k} |") else ln.rstrip().rstrip("|").rstrip() + f" {tags} |"
                 for ln in text.splitlines()]
        text = "\n".join(lines)
    ref.write_text(text)
    with pytest.raises(SystemExit):
        await b.make_sample(n=3)  # reference already filled
    b.submit("opencode-go/model-a", [{"key": keys[0], "tags": ["topic/asthma", "type/cohort"]},
                                     {"key": keys[1], "tags": ["topic/spirometry", "status/read", "made-up"]}])
    out = b.submit("opencode-go/model-b", [{"key": keys[0], "tags": ["topic/copd"]}, {"key": "ZZZZ9999", "tags": []}])
    assert out["missing_items"] == keys[1:] and out["not_in_sample"] == ["ZZZZ9999"]
    (b.dir / "usage-model-a.json").write_text(json.dumps({"steps": 4, "tokens_in": 1000, "tokens_out": 200,
                                                          "cost_usd": 0.01, "seconds": 90}))
    res = b.score()
    a, bb = res["results"]
    assert res["items_scored"] == 2  # the empty reference row is skipped
    assert a["model"] == "opencode-go/model-a" and a["edits"] == 0 and a["exact_items"] == 2
    assert a["invalid_tags"] == 1 and a["status_tags"] == 1
    assert bb["edits"] == 4 and bb["missing_items"] == 1 and bb["recall"] == 0.0
    note = (vault / "Inbox" / bk.RESULTS_NOTE).read_text()
    assert "| opencode-go/model-a | 0 | 0.0 |" in note and "1000/200" in note


def test_usage_from_opencode_log(tmp_path):
    from zotero_local_mcp.bakeoff import usage_from_log
    log = tmp_path / "run.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in [
        {"type": "text", "part": {"text": "hi"}},
        {"type": "step_finish", "part": {"type": "step-finish", "tokens": {"input": 100, "output": 20,
                                         "reasoning": 5, "cache": {"read": 50}}, "cost": 0.002}},
        {"type": "step_finish", "part": {"type": "step-finish", "tokens": {"input": 10, "output": 1}, "cost": 0.001}},
    ]) + "\nnot json\n")
    u = usage_from_log(log)
    assert u == {"steps": 2, "tokens_in": 160, "tokens_out": 26, "cost_usd": 0.003}


async def test_researcher_attaches_linked_note(scholar, lib, fake, vault):
    note = vault / "Resources" / "Zotero" / "jacinto2026.md"
    note.parent.mkdir(parents=True)
    note.write_text("# note")
    with pytest.raises(ZoteroError, match="existing .md file"):
        await scholar.attach_note("AAAA1111", "x", "Resources/Zotero/missing.md")
    with pytest.raises(ZoteroError, match="existing .md file"):
        await scholar.attach_note("AAAA1111", "x", "/etc/passwd")
    prev = await scholar.attach_note("AAAA1111", "Cohort of 500 children.", "Resources/Zotero/jacinto2026.md")
    assert prev["dry_run"] and prev["link"] == "obsidian://open?vault=vault&file=Resources%2FZotero%2Fjacinto2026"
    before = set(fake.items)
    res = await scholar.attach_note("AAAA1111", "Cohort of **500** children. <script>x</script>",
                                    str(note), dry_run=False)
    new = fake.items[res["created"]]
    assert new["parentItem"] == "AAAA1111" and new["tags"] == [{"tag": "_agent"}]
    assert "<strong>500</strong>" in new["note"] and "<script>" not in new["note"]
    assert "obsidian://open?vault=vault&amp;file=Resources%2FZotero%2Fjacinto2026" in new["note"]
    again = await scholar.attach_note("AAAA1111", "Other summary", str(note), dry_run=False)
    assert "already has a note" in again["skipped"]
    assert set(fake.items) - before == {res["created"]}
    # The librarian can undo it from the shared journal
    lib.s = scholar.s
    out = await lib.undo(dry_run=False)
    assert out["applied"] == 1 and fake.items[res["created"]]["deleted"] is True


# ---------------------------------------------------------------- Sub-Sub model test tasks

async def test_bench_prepare(lib, fake, ext, settings, tmp_path, monkeypatch):
    from fake_external import PUBMED
    from zotero_local_mcp import bench
    from zotero_local_mcp.bakeoff import Bakeoff
    lib.s = settings
    pmid = next(iter(PUBMED))
    monkeypatch.setattr(bench, "NEW_DOIS", ["10.1000/missing", "10.1183/13993003.00001-2026"])
    monkeypatch.setattr(bench, "NEW_PMIDS", [pmid])
    # reviewed items (no _agent) and agent-tagged items, all on one topic
    for n in range(8):
        tags = [{"tag": "topic/asthma"}, {"tag": "status/read"}] + ([{"tag": "_agent"}] if n % 2 else [])
        fake.add_item(key=f"ITEM{n:04d}".replace("0", "Q"), title=f"Asthma study {n}", date="2020",
                      abstractNote=f"Abstract {n}", DOI=f"10.1000/asthma.{n}", tags=tags,
                      extra=f"Citation Key: author{n}2020",
                      creators=[{"creatorType": "author", "firstName": "A", "lastName": f"Author{n}"}])
    att = fake.add_item(itemType="attachment", parentItem="ITEMQQQ3", contentType="application/pdf",
                        title="PDF", linkMode="imported_file")
    fake.fulltext[att] = {"content": "x" * 9000, "indexedPages": 5, "totalPages": 5}
    out = tmp_path / "bench"
    res = await bench.prepare(lib, ext, out, seed=1, n=6)
    tasks = json.loads((out / "tasks.json").read_text())
    assert res["tagging_items"] == 6 and len(tasks["tagging"]["keys"]) == 6
    ref = (out / "reference.md").read_text()
    assert ref.count("## ") == 6 and "Abstract" in ref and "topic/asthma" not in ref  # blind: no current tags
    assert "AAAA1111" in tasks["facet_fix"]["missing_topic"]
    assert tasks["lit_note"]["key"] == "ITEMQQQ3"
    assert tasks["synthesis"]["topic"] == "topic/asthma" and len(tasks["synthesis"]["keys"]) == 8
    ids = [x["id"] for x in tasks["import"]["new"]]
    assert ids == ["10.1183/13993003.00001-2026", f"PMID:{pmid}"]
    assert tasks["import"]["in_library"]["id"].startswith("10.1000/asthma.")
    index = json.loads((out / "library-index.json").read_text())
    assert any(r["citekey"] == "author32020" for r in index)
    # bakeoff_items uses the same sample and hides the current tags
    items = (await Bakeoff(lib).items())["items"]
    assert len(items) == 6 and all("tags" not in i and "automatic_tags" not in i for i in items)


async def test_templates_come_from_the_web_api_and_are_saved(librarian, fake, settings):
    """Zotero 10's local API has no /items/new (found in the model test)."""
    from zotero_local_mcp.client import LocalZotero
    await librarian.import_identifiers(["10.1183/13993003.00001-2026"])
    assert fake.web_template_requests == 1
    saved = settings.state_dir / "templates" / "journalArticle.json"
    assert json.loads(saved.read_text())["itemType"] == "journalArticle"
    again = LocalZotero(settings.api_url, settings.state_dir, 5, transport=fake.transport())
    assert (await again.template("journalArticle"))["itemType"] == "journalArticle"
    assert fake.web_template_requests == 1  # from the saved copy
    fake.local_templates = True
    third = LocalZotero(settings.api_url, settings.state_dir, 5, transport=fake.transport())
    assert (await third.template("book"))["itemType"] == "book"
    assert fake.web_template_requests == 1  # the local API wins when it has templates
    await again.aclose()
    await third.aclose()


async def test_duplicates_cut_off_title_and_types(librarian, fake):
    full = fake.add_item(key="HHHH8888", title="An official ATS clinical practice guideline: interpretation of exhaled nitric oxide",
                         date="2011-09-01", DOI="10.1164/rccm.9120-11st", abstractNote="FeNO...",
                         creators=[{"creatorType": "author", "lastName": "Dweik"}], extra="Citation Key: dweik2011a",
                         dateAdded="2026-05-16T19:20:57Z")
    stub = fake.add_item(key="JJJJ9999", itemType="document", title="An official ATS clinical practice guideline inter",
                         date="2011", creators=[{"creatorType": "author", "lastName": "Dweik"}],
                         extra="Citation Key: dweik2011", dateAdded="2026-05-16T19:21:00Z")
    # same author and year, different DOIs: two works, not a cut-off title
    fake.add_item(key="KKKK2222", title="An official ATS clinical practice guideline: interpretation of exhaled nitric oxide levels",
                  date="2011", DOI="10.1000/other", creators=[{"creatorType": "author", "lastName": "Dweik"}])
    res = await librarian.duplicates()
    g = next(g for g in res["duplicates"] if stub in {i["key"] for i in g["items"]})
    assert {i["key"] for i in g["items"]} >= {full, stub}
    assert any("cut off" in m for m in g["matched_by"])
    assert g["fullest_record"] == full and "types_differ" in g and "dweik2011" in g["citekeys"]


async def test_audit_flags_one_page_articles(librarian, fake):
    fake.add_item(key="LLLL3333", title="Introduction to statistics and data analysis", pages="549-549",
                  publicationTitle="J R Stat Soc A", DOI="10.1093/jrsssa/qnad123", date="2024")
    res = await librarian.audit(problem="1 or 2 pages (letter, editorial or book review?)")
    assert [r["key"] for r in res["items"]] == ["LLLL3333"]
