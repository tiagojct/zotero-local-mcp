from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

from zotero_local_mcp import reading as reading_mod
from zotero_local_mcp.client import ZoteroError
from zotero_local_mcp.jobs import Jobs
from zotero_local_mcp.reading import EXPORT_MARKER, Reading


def add_pdf_with_annotations(fake) -> str:
    att = fake.add_item(key="PDFA0001", itemType="attachment", parentItem="AAAA1111",
                        title="Full Text PDF", contentType="application/pdf", linkMode="imported_url")
    # Added out of reading order: the tool sorts by annotationSortIndex.
    fake.add_item(key="ANNO0002", itemType="annotation", parentItem=att, annotationType="note",
                  annotationText="", annotationComment="Check the GLI cut-offs",
                  annotationPageLabel="5", annotationSortIndex="00004|000100|00200",
                  annotationPosition=json.dumps({"pageIndex": 4, "rects": []}))
    fake.add_item(key="ANNO0001", itemType="annotation", parentItem=att, annotationType="highlight",
                  annotationText="Reference values differ\nby ethnicity", annotationComment="",
                  annotationColor="#ffd400", annotationPageLabel="3",
                  annotationSortIndex="00002|000010|00100",
                  annotationPosition=json.dumps({"pageIndex": 2, "rects": [[1, 2, 3, 4]]}),
                  tags=[{"tag": "key-finding"}])
    # Another item's annotation must not show.
    other = fake.add_item(key="PDFB0001", itemType="attachment", parentItem="BBBB2222",
                          contentType="application/pdf")
    fake.add_item(key="ANNO0009", itemType="annotation", parentItem=other, annotationType="highlight",
                  annotationText="Other paper", annotationSortIndex="00001|000001|00001")
    return att


# ---------------------------------------------------------------- annotations

async def test_annotations_of_an_item_in_reading_order(lib, fake):
    add_pdf_with_annotations(fake)
    res = await Reading(lib).get_annotations("AAAA1111")
    assert res["count"] == 2
    rows = res["files"][0]["annotations"]
    assert [r["key"] for r in rows] == ["ANNO0001", "ANNO0002"]
    first = rows[0]
    assert first["type"] == "highlight" and first["page_label"] == "3" and first["page"] == 3
    assert first["tags"] == ["key-finding"]
    assert first["link"] == "zotero://open-pdf/library/items/PDFA0001?annotation=ANNO0001"
    assert rows[1]["comment"] == "Check the GLI cut-offs" and "text" not in rows[1]


async def test_annotations_by_attachment_key_and_kind(lib, fake):
    att = add_pdf_with_annotations(fake)
    res = await Reading(lib).get_annotations(att, kinds=["Note"])
    assert res["citekey"] is None
    assert [r["key"] for r in res["files"][0]["annotations"]] == ["ANNO0002"]


async def test_annotations_without_file_or_without_annotations(lib, fake):
    with pytest.raises(ZoteroError, match="no attachment"):
        await Reading(lib).get_annotations("CCCC3333")
    fake.add_item(key="PDFC0001", itemType="attachment", parentItem="CCCC3333", contentType="application/pdf")
    res = await Reading(lib).get_annotations("CCCC3333")
    assert res["count"] == 0 and "Import Annotations" in res["note"]
    with pytest.raises(ZoteroError, match="is a note"):
        await Reading(lib).get_annotations("NOTE0001")


# ---------------------------------------------------------------- selection

async def test_selection_needs_gam(lib, fake):
    with pytest.raises(ZoteroError, match="gam 0.4.0"):
        await Reading(lib).get_selected_items()


async def test_selection_from_gam_with_detail(lib, fake):
    fake.selection = {"gam": "0.4.0", "tab": "reader", "count": 2,
                      "items": [{"key": "AAAA1111", "library": "user", "itemType": "journalArticle",
                                 "title": "Spirometry reference values in adults", "citekey": "", "doi": ""},
                                {"key": "NOTE0001", "library": "user", "itemType": "note",
                                 "title": "My note", "citekey": "", "doi": ""}],
                      "reader": {"key": "PDFA0001", "library": "user"}, "collection": None}
    plain = await Reading(lib).get_selected_items()
    assert plain["reader"]["key"] == "PDFA0001" and "abstract" not in plain["items"][0]
    rich = await Reading(lib).get_selected_items(detail=True)
    assert rich["items"][0]["abstract"] == "GLI equations..."
    assert rich["items"][1] == fake.selection["items"][1]  # a note gets no detail
    assert "get_annotations" in rich["next"]


# ---------------------------------------------------------------- show

async def test_show_in_zotero(lib, fake, monkeypatch):
    opened = []

    class Proc:
        async def wait(self):
            return 0

    async def fake_exec(*args):
        opened.append(args)
        return Proc()

    monkeypatch.setattr(reading_mod.sys, "platform", "darwin")
    monkeypatch.setattr(reading_mod.asyncio, "create_subprocess_exec", fake_exec)
    res = await Reading(lib).show_in_zotero("AAAA1111")
    assert res["shown"] is True
    assert opened == [("open", "zotero://select/library/items/AAAA1111")]
    with pytest.raises(ValueError, match="not a Zotero item key"):
        await Reading(lib).show_in_zotero("AAAA1111; rm -rf ~")
    with pytest.raises(ZoteroError):
        await Reading(lib).show_in_zotero("ZZZZ9999")
    assert len(opened) == 1


# ---------------------------------------------------------------- export

async def test_export_markdown_writes_one_file_per_item(lib, fake, tmp_path):
    add_pdf_with_annotations(fake)
    fake.fulltext["PDFA0001"] = {"content": "Introduction. Spirometry is...", "indexedPages": 2, "totalPages": 2}
    fake.items["AAAA1111"]["citationKey"] = "jacinto2026"
    out = tmp_path / "kb"
    res = await Reading(lib).export_markdown(keys=["AAAA1111", "BBBB2222"], folder=str(out))
    assert [w["key"] for w in res["written"]] == ["AAAA1111", "BBBB2222"]
    text = (out / "jacinto2026.md").read_text(encoding="utf-8")
    assert text.startswith("---\n" + EXPORT_MARKER + "\n")
    assert 'citekey: "jacinto2026"' in text
    assert "## Abstract\n\nGLI equations..." in text
    assert "## Annotations" in text and "  > Reference values differ by ethnicity" in text
    assert "  Comment: Check the GLI cut-offs" in text
    assert text.index("ANNO0001") < text.index("ANNO0002")
    assert "Other paper" not in text
    assert "## Full text" in text and "Spirometry is..." in text
    # Automatic tags (keywords) are left out; the child note of BBBB2222 is in its file.
    assert "Lung Function Tests" not in text
    other = (out / "BBBB2222.md").read_text(encoding="utf-8")
    assert "## Notes" in other and "My note" in other
    assert res["written"][0]["annotations"] == 2 and res["written"][0]["fulltext"] is True


async def test_export_markdown_never_overwrites_a_foreign_file(lib, fake, tmp_path):
    out = tmp_path / "kb"
    out.mkdir()
    (out / "BBBB2222.md").write_text("---\ntitle: mine\n---\nMy own notes\n", encoding="utf-8")
    res = await Reading(lib).export_markdown(keys=["BBBB2222"], folder=str(out))
    assert res["written"] == [] and res["skipped"][0]["key"] == "BBBB2222"
    assert (out / "BBBB2222.md").read_text(encoding="utf-8").endswith("My own notes\n")
    # A file of an earlier export is replaced.
    (out / "BBBB2222.md").unlink()
    await Reading(lib).export_markdown(keys=["BBBB2222"], folder=str(out))
    fake.touch("BBBB2222", title="Asthma control in primary care, revised")
    res = await Reading(lib).export_markdown(keys=["BBBB2222"], folder=str(out))
    assert res["skipped"] == []
    assert "revised" in (out / "BBBB2222.md").read_text(encoding="utf-8")
    assert not list(out.glob("*.tmp"))


async def test_export_markdown_refuses_the_vault_and_empty_requests(lib, tmp_path):
    vault = tmp_path / "vault"
    (vault / "literature").mkdir(parents=True)
    lib.s = dataclasses.replace(lib.s, vault=vault)
    with pytest.raises(ValueError, match="inside the Obsidian vault"):
        await Reading(lib).export_markdown(keys=["AAAA1111"], folder=str(vault / "literature"))
    with pytest.raises(ValueError, match="Give keys"):
        await Reading(lib).export_markdown(folder=str(tmp_path / "kb"))
    with pytest.raises(ZoteroError, match="ZZZZ9999"):
        await Reading(lib).export_markdown(keys=["ZZZZ9999"], folder=str(tmp_path / "kb"))


async def test_export_markdown_by_query_with_limit_and_default_folder(lib, tmp_path):
    res = await Reading(lib).export_markdown(query="in", limit=1)
    assert Path(res["folder"]) == (tmp_path / "state" / "export").resolve()
    assert len(res["written"]) == 1 and res["not_exported"] >= 1


async def test_export_markdown_same_citekey_twice(lib, fake, tmp_path):
    fake.items["AAAA1111"]["citationKey"] = "same2026"
    fake.items["BBBB2222"]["citationKey"] = "Same2026"
    res = await Reading(lib).export_markdown(keys=["AAAA1111", "BBBB2222"], folder=str(tmp_path / "kb"))
    names = sorted(Path(w["path"]).name for w in res["written"])
    assert names == ["Same2026-BBBB2222.md", "same2026.md"]


# ---------------------------------------------------------------- jobs

async def test_job_returns_fast_results_directly():
    jobs = Jobs()

    async def quick():
        return {"ok": 1}

    assert await jobs.run("quick", quick, wait=1) == {"ok": 1}


async def test_job_slow_call_gives_a_job_id_then_the_result():
    jobs = Jobs()
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return {"done": True}

    first = await jobs.run("slow", slow, wait=0.01)
    assert first["status"] == "running" and first["tool"] == "slow"
    assert jobs.get(first["job_id"])["status"] == "running"
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    res = jobs.get(first["job_id"])
    assert res["status"] == "done" and res["result"] == {"done": True}


async def test_job_failure_is_reported():
    jobs = Jobs()

    async def bad():
        await asyncio.sleep(0.05)
        raise ZoteroError("Zotero went away")

    first = await jobs.run("bad", bad, wait=0.01)
    await asyncio.sleep(0.1)
    res = jobs.get(first["job_id"])
    assert res["status"] == "failed" and "Zotero went away" in res["error"]
    with pytest.raises(LookupError, match="No job"):
        jobs.get("job-unknown")


async def test_job_fast_failure_raises_to_the_caller():
    jobs = Jobs()

    async def bad():
        raise ValueError("bad input")

    with pytest.raises(ValueError, match="bad input"):
        await jobs.run("bad", bad, wait=1)


async def test_server_wraps_long_tools(monkeypatch):
    from zotero_local_mcp import server
    monkeypatch.setenv("ZOTERO_JOB_WAIT", "0")
    gate = asyncio.Event()

    class Slow:
        async def attach_oa_pdfs(self, keys, dry_run):
            await gate.wait()
            return {"attached": keys}

    monkeypatch.setattr(server, "librarian", lambda: Slow())
    first = await server.attach_oa_pdfs(["AAAA1111"], dry_run=True)
    assert first["status"] == "running"
    gate.set()
    await asyncio.sleep(0.01)
    assert (await server.get_job(first["job_id"]))["result"] == {"attached": ["AAAA1111"]}


def test_tidy_text_removes_indentation_and_empty_runs():
    from zotero_local_mcp.reading import tidy_text
    assert tidy_text("  Title\n  \n\n\n    Para one\n\tPara two  \n\n") == "Title\n\nPara one\nPara two"


async def test_export_markdown_refuses_the_whole_obsidian_vault(lib, tmp_path):
    # ZOTERO_VAULT is the Sub-Sub folder; the Obsidian vault (with .obsidian/) is its parent.
    vault = tmp_path / "powerslave"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "sub-sub").mkdir()
    (vault / "atlas").mkdir()
    lib.s = dataclasses.replace(lib.s, vault=vault / "sub-sub")
    with pytest.raises(ValueError, match="inside the Obsidian vault"):
        await Reading(lib).export_markdown(keys=["AAAA1111"], folder=str(vault / "atlas"))
    assert not list((vault / "atlas").iterdir())
    res = await Reading(lib).export_markdown(keys=["AAAA1111"], folder=str(tmp_path / "kb"))
    assert len(res["written"]) == 1


async def test_export_markdown_skips_a_folder_with_the_file_name(lib, tmp_path):
    out = tmp_path / "kb"
    (out / "BBBB2222.md").mkdir(parents=True)
    res = await Reading(lib).export_markdown(keys=["AAAA1111", "BBBB2222"], folder=str(out))
    assert [w["key"] for w in res["written"]] == ["AAAA1111"]
    assert res["skipped"][0]["reason"] == "a folder has this name"


async def test_job_same_call_after_client_cancel_does_not_run_twice():
    jobs = Jobs()
    gate = asyncio.Event()
    starts = []

    async def write():
        starts.append(1)
        await gate.wait()
        return {"written": True}

    caller = asyncio.ensure_future(jobs.run("import_identifiers", write, wait=10, call="same"))
    await asyncio.sleep(0.01)
    caller.cancel()  # the client stops waiting before the job id is given
    with pytest.raises(asyncio.CancelledError):
        await caller
    again = await jobs.run("import_identifiers", write, wait=0.01, call="same")
    assert again["status"] == "running" and "still running" in again["message"]
    assert starts == [1]
    other = await jobs.run("import_identifiers", write, wait=0.01, call="other")
    assert other["job_id"] != again["job_id"] and starts == [1, 1]
    gate.set()
    await asyncio.sleep(0.01)
    assert jobs.get(again["job_id"])["result"] == {"written": True}


async def test_job_fast_result_leaves_no_job():
    jobs = Jobs()

    async def quick():
        return 1

    assert await jobs.run("t", quick, wait=1, call="c") == 1
    assert jobs._jobs == {}
