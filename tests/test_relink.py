from __future__ import annotations

import html
import sys

import pytest

from zotero_local_mcp import relink
from zotero_local_mcp.client import WriteResult, ZoteroError

PLAN = "old\tnew\tkind\tnote\n" \
       "Sub-Sub/Resources/Zotero/x.md\tsub-sub/literature/x.md\tfile\t\n" \
       "Sub-Sub/Resources/Zotero/Ä b.md\tsub-sub/literature/a-b.md\tfile\t\n"


def note(key, file, version=3, vault="powerslave", parent="AAAA1111"):
    uri = f"obsidian://open?vault={vault}&amp;file={file}"
    return {"key": key, "version": version,
            "data": {"key": key, "version": version, "itemType": "note", "parentItem": parent,
                     "note": f'<div><p>Summary</p><p><a href="{uri}">Obsidian: x</a></p></div>'}}


class FakeClient:
    def __init__(self, notes):
        self.notes = notes
        self.written = []

    async def all_items(self):
        return [{"key": "AAAA1111", "version": 1, "data": {"itemType": "journalArticle"}}, *self.notes]

    async def update_items(self, objects):
        self.written.extend(objects)
        r = WriteResult()
        r.succeeded = [o["key"] for o in objects]
        return r


def test_read_map(tmp_path):
    p = tmp_path / "plan.tsv"
    p.write_text(PLAN, encoding="utf-8")
    m = relink.read_map(p)
    assert m["Sub-Sub/Resources/Zotero/x.md"] == "sub-sub/literature/x.md"
    assert len(m) == 2


async def test_dry_run_writes_nothing(tmp_path):
    z = FakeClient([note("NOTE0001", "Sub-Sub%2FResources%2FZotero%2Fx")])
    plan = {"Sub-Sub/Resources/Zotero/x.md": "sub-sub/literature/x.md"}
    changes = await relink.relink(z, plan, apply=False)
    assert [(c["key"], c["old"], c["new"]) for c in changes] == [
        ("NOTE0001", "Sub-Sub/Resources/Zotero/x.md", "sub-sub/literature/x.md")]
    assert z.written == []


async def test_relink_rewrites_file_param(tmp_path):
    z = FakeClient([note("NOTE0001", "Sub-Sub%2FResources%2FZotero%2Fx"),
                    note("NOTE0002", "Other%2Fy"),
                    note("NOTE0003", "Sub-Sub%2FResources%2FZotero%2Fx", vault="elsewhere")])
    plan = {"Sub-Sub/Resources/Zotero/x.md": "sub-sub/literature/x.md"}
    changes = await relink.relink(z, plan, apply=True)
    assert len(changes) == 1
    assert [o["key"] for o in z.written] == ["NOTE0001"]
    assert z.written[0]["version"] == 3
    body = html.unescape(z.written[0]["note"])
    assert "file=sub-sub%2Fliterature%2Fx\"" in body
    assert "vault=powerslave&file=" in body
    assert "<p>Summary</p>" in body


async def test_relink_keeps_md_suffix_and_nfc(tmp_path):
    import unicodedata
    from urllib.parse import quote
    old = unicodedata.normalize("NFD", "Sub-Sub/Resources/Zotero/Ä b.md")
    z = FakeClient([note("NOTE0001", quote(old, safe=""))])
    changes = await relink.relink(z, {"Sub-Sub/Resources/Zotero/Ä b.md": "sub-sub/literature/a-b.md"}, apply=True)
    assert changes and "file=sub-sub%2Fliterature%2Fa-b.md\"" in html.unescape(z.written[0]["note"])


async def test_cli_prints_one_line(tmp_path, capsys):
    p = tmp_path / "plan.tsv"
    p.write_text(PLAN, encoding="utf-8")
    z = FakeClient([note("NOTE0001", "Sub-Sub%2FResources%2FZotero%2Fx")])
    rc = await relink.run(p, apply=False, client=z)
    assert rc == 0
    out = capsys.readouterr().out
    assert "NOTE0001\tSub-Sub/Resources/Zotero/x.md\tsub-sub/literature/x.md" in out
    assert "Nothing was written" in out


PLAN1 = {"Sub-Sub/Resources/Zotero/x.md": "sub-sub/literature/x.md"}


class BatchClient(FakeClient):
    """Like LocalZotero.update_items: batches of `size`; the second batch fails."""
    size = 2

    async def update_items(self, objects):
        r = WriteResult()
        r.succeeded = [o["key"] for o in objects[: self.size]]
        if len(objects) > self.size:
            r.error = "Lost the connection"
            r.not_sent = [o["key"] for o in objects[self.size:]]
        return r


async def test_failed_write_reports_state():
    notes = [note(f"NOTE000{i}", "Sub-Sub%2FResources%2FZotero%2Fx") for i in range(1, 5)]
    with pytest.raises(relink.RelinkError) as e:
        await relink.relink(BatchClient(notes), PLAN1, apply=True)
    assert e.value.written == ["NOTE0001", "NOTE0002"]
    assert e.value.not_written == ["NOTE0003", "NOTE0004"]


def test_main_prints_written_and_not_written(monkeypatch, capsys, tmp_path):
    p = tmp_path / "plan.tsv"
    p.write_text(PLAN, encoding="utf-8")

    async def boom(*a, **k):
        raise relink.RelinkError("Lost", ["A"], ["B", "C"])

    monkeypatch.setattr(relink, "run", boom)
    monkeypatch.setattr(sys, "argv", ["zotero-relink", "--map", str(p), "--apply"])
    with pytest.raises(SystemExit) as e:
        relink.main()
    out = capsys.readouterr().out
    assert e.value.code == 1 and "Written (1): A" in out and "Not written (2): B C" in out


async def test_extra_query_parameter():
    n = note("NOTE0001", "Sub-Sub%2FResources%2FZotero%2Fx")
    n["data"]["note"] = n["data"]["note"].replace("vault=powerslave&amp;", "vault=powerslave&amp;line=3&amp;")
    z = FakeClient([n])
    changes = await relink.relink(z, PLAN1, apply=True)
    body = z.written[0]["note"]
    assert len(changes) == 1 and "line=3&amp;file=sub-sub%2Fliterature%2Fx\"" in body


async def test_same_path_in_several_notes():
    z = FakeClient([note(f"NOTE000{i}", "Sub-Sub%2FResources%2FZotero%2Fx", parent=f"P{i}") for i in (1, 2, 3)])
    changes = await relink.relink(z, PLAN1, apply=True)
    assert [c["key"] for c in changes] == ["NOTE0001", "NOTE0002", "NOTE0003"]
    assert [o["key"] for o in z.written] == ["NOTE0001", "NOTE0002", "NOTE0003"]


@pytest.mark.parametrize("text", [
    "from\tto\tkind\tnote\na\tb\tfile\t\n",
    "a\tb\tfile\t\n",
    "old\tnew\tkind\tnote\nonlyone\n",
    "old\tnew\tkind\tnote\na\t\tfile\t\n",
    "",
])
def test_malformed_map(tmp_path, monkeypatch, capsys, text):
    p = tmp_path / "plan.tsv"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ZoteroError):
        relink.read_map(p)
    monkeypatch.setattr(sys, "argv", ["zotero-relink", "--map", str(p), "--apply"])
    with pytest.raises(SystemExit) as e:
        relink.main()
    assert e.value.code == 1 and "Error:" in capsys.readouterr().out


async def test_vault_name_case_insensitive_and_rewritten(tmp_path):
    z = FakeClient([note("NOTE0001", "Sub-Sub%2FResources%2FZotero%2Fx", vault="Powerslave")])
    plan = {"Sub-Sub/Resources/Zotero/x.md": "sub-sub/literature/x.md"}
    changes = await relink.relink(z, plan, apply=True)
    assert len(changes) == 1
    body = html.unescape(z.written[0]["note"])
    assert "vault=powerslave&file=sub-sub%2Fliterature%2Fx\"" in body
    assert "Powerslave" not in body


async def test_vault_option_sets_value_and_unchanged_links_keep_case(tmp_path):
    z = FakeClient([note("NOTE0001", "Sub-Sub%2FResources%2FZotero%2Fx", vault="Powerslave"),
                    note("NOTE0002", "Other%2Fy", vault="Powerslave")])
    plan = {"Sub-Sub/Resources/Zotero/x.md": "sub-sub/literature/x.md"}
    await relink.relink(z, plan, apply=True, vault="POWERSLAVE")
    assert [o["key"] for o in z.written] == ["NOTE0001"]
    assert "vault=POWERSLAVE&amp;file=" in z.written[0]["note"]
