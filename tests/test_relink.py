from __future__ import annotations

import html

from zotero_local_mcp import relink
from zotero_local_mcp.client import WriteResult

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
