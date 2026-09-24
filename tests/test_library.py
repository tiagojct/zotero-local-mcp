from __future__ import annotations

import json

import pytest

from zotero_local_mcp.citekey import base_key, slug, with_key
from zotero_local_mcp.client import ZoteroError


def tags_of(fake, key):
    return sorted((t["tag"], int(t.get("type") or 0)) for t in fake.items[key]["tags"])


# ---------------------------------------------------------------- reads

async def test_status_and_overview(lib, fake):
    st = await lib.status()
    assert st["zotero"] == "reachable" and st["server_id"] == fake.server_id
    assert st["vocabulary"]["tags"] == 9
    ov = await lib.overview()
    assert ov["items"] == 4  # the child note is not counted
    assert ov["by_type"] == {"journalArticle": 3, "book": 1}
    assert ov["missing_facet"]["topic"] == 4
    assert ov["missing_facet"]["status"] == 3
    assert ov["manual_tags_outside_vocabulary"]["top"] == {"Asthma": 1, "CDSS": 1}
    assert ov["without_citekey"] == 3
    assert ov["items_with_automatic_tags"] == 1


async def test_find_filters(lib):
    res = await lib.find(missing_facet="topic", detail=True)
    assert res["total"] == 4
    assert res["items"][0]["key"] == "DDDD4444"  # newest first
    first = next(i for i in res["items"] if i["key"] == "AAAA1111")
    assert first["automatic_tags"] == ["Spirometry", "Lung Function Tests"]
    assert (await lib.find(item_type="book"))["items"][0]["citekey"] == "west1974"
    assert (await lib.find(untagged=True))["total"] == 2
    assert {i["key"] for i in (await lib.find(outside_vocabulary=True))["items"]} == {"BBBB2222", "DDDD4444"}
    assert (await lib.find(query="asthma"))["total"] == 1


async def test_get_item_and_tags(lib):
    item = await lib.get_item("BBBB2222")
    assert item["notes"][0]["text"] == "My note"
    assert item["zotero_link"].endswith("BBBB2222")
    tags = await lib.list_tags(include_automatic=True)
    names = {t["tag"]: t for t in tags["tags"]}
    assert names["Spirometry"]["automatic"] is True
    assert names["status/read"]["in_vocabulary"] is True
    out = await lib.list_tags(outside_vocabulary=True)
    assert {t["tag"] for t in out["tags"]} == {"Asthma", "CDSS"}


# ---------------------------------------------------------------- tagging

async def test_tag_items_dry_run_writes_nothing(lib, fake):
    res = await lib.tag_items([{"key": "AAAA1111", "add": ["topic/spirometry", "status/to-read"]}])
    assert res["dry_run"] and res["would_change"] == 1
    ch = res["changes"][0]
    assert ch["added"] == ["_agent", "status/to-read", "topic/spirometry"]
    assert fake.write_requests == 0 and fake.auth_prompts == 0


async def test_tag_items_apply_and_single_facet(lib, fake):
    res = await lib.tag_items([
        {"key": "AAAA1111", "add": ["topic/spirometry", "method/reference-equations"]},
        {"key": "BBBB2222", "add": ["status/to-read"], "remove": ["Asthma"]},
    ], dry_run=False)
    assert res["applied"] == 2 and res["journal_id"]
    assert ("topic/spirometry", 0) in tags_of(fake, "AAAA1111")
    assert ("Spirometry", 1) in tags_of(fake, "AAAA1111")  # automatic tags untouched
    assert ("_agent", 0) in tags_of(fake, "AAAA1111")
    b = tags_of(fake, "BBBB2222")
    assert ("status/to-read", 0) in b and ("status/read", 0) not in b and ("Asthma", 0) not in b
    assert fake.auth_prompts == 1  # remembered key reused for the second batch
    hdr = next(h for h in fake.seen_headers if "zotero-api-key" in h)
    assert hdr["zotero-server-id"] == fake.server_id
    assert not hdr["user-agent"].startswith("Mozilla/")


async def test_unknown_tag_is_refused_with_alias_hint(lib, fake):
    with pytest.raises(ZoteroError) as err:
        await lib.tag_items([{"key": "AAAA1111", "add": ["pft", "status/read", "status/to-read"]}])
    msg = str(err.value)
    assert "'pft' is not in the vocabulary (alias of topic/spirometry)" in msg
    assert "only one 'status/' tag" in msg
    assert fake.write_requests == 0


async def test_version_conflict_is_reapplied(lib, fake):
    # Read happens inside tag_items; simulate an edit between read and write
    orig = lib.z.items_by_keys
    calls = {"n": 0}

    async def racing(keys):
        out = await orig(keys)
        calls["n"] += 1
        if calls["n"] == 1:
            fake.touch("AAAA1111", title="Edited in Zotero")
        return out

    lib.z.items_by_keys = racing
    res = await lib.tag_items([{"key": "AAAA1111", "add": ["topic/spirometry"]}], dry_run=False)
    assert res["applied"] == 1 and not res["failed"]
    assert fake.items["AAAA1111"]["title"] == "Edited in Zotero"  # user's edit kept
    assert ("topic/spirometry", 0) in tags_of(fake, "AAAA1111")


async def test_rename_merge_and_remove_automatic(lib, fake):
    res = await lib.rename_tags({"Asthma": "topic/asthma", "CDSS": "topic/clinical-decision-support",
                                 "Spirometry": "topic/spirometry"}, dry_run=False)
    assert res["applied"] == 3
    assert ("topic/asthma", 0) in tags_of(fake, "BBBB2222")
    assert ("Spirometry", 1) not in tags_of(fake, "AAAA1111")
    assert ("topic/spirometry", 0) in tags_of(fake, "AAAA1111")
    assert ("_agent", 0) not in tags_of(fake, "AAAA1111")  # renames are not marked
    res = await lib.remove_automatic_tags(dry_run=False)
    assert res["applied"] == 1
    assert tags_of(fake, "AAAA1111") == [("topic/spirometry", 0)]
    with pytest.raises(ZoteroError):
        await lib.rename_tags({"Asthma": "asthma-new"})


async def test_undo_restores_and_skips_later_edits(lib, fake):
    before_a = tags_of(fake, "AAAA1111")
    before_b = tags_of(fake, "BBBB2222")
    await lib.tag_items([{"key": "AAAA1111", "add": ["topic/spirometry"]},
                         {"key": "BBBB2222", "add": ["topic/asthma"]}], dry_run=False)
    fake.touch("BBBB2222", tags=[{"tag": "manual-edit"}])  # user changes tags afterwards
    preview = await lib.undo()
    assert preview["dry_run"] and preview["would_change"] == 1
    assert "BBBB2222" in preview["skipped"]
    res = await lib.undo(dry_run=False)
    assert res["applied"] == 1
    assert tags_of(fake, "AAAA1111") == before_a
    assert tags_of(fake, "BBBB2222") == [("manual-edit", 0)] != before_b
    hist = await lib.history()
    assert hist[0]["op"] == "undo" and hist[1]["undone_by"] == [hist[0]["id"]]
    assert hist[1]["items_undone"] == 1 and not hist[1]["fully_undone"]
    # The skipped item can be retried once the user restores its tags
    fake.touch("BBBB2222", tags=[{"tag": "Asthma"}, {"tag": "status/read"}, {"tag": "topic/asthma"}, {"tag": "_agent"}])
    res = await lib.undo(dry_run=False)
    assert res["applied"] == 1 and tags_of(fake, "BBBB2222") == before_b
    with pytest.raises(ZoteroError, match="already undone"):
        await lib.undo(hist[1]["id"])


# ---------------------------------------------------------------- citekeys and fields

def test_citekey_rules():
    assert slug("Fonsêca") == "fonseca"
    assert slug("van der Berg") == "vanderberg"
    assert base_key({"creators": [{"creatorType": "editor", "lastName": "Ávila"}], "date": "May 2019"}) == "avila2019"
    assert base_key({"creators": [{"creatorType": "author", "name": "World Health Organization"}]}) == "worldhealthorganizationnd"
    assert with_key({"extra": "PMID: 1\nCitation Key: old"}, "new") == {"extra": "Citation Key: new\nPMID: 1"}
    assert with_key({"citationKey": "", "extra": ""}, "k") == {"citationKey": "k"}


async def test_set_citekeys_collisions_and_pinning(lib, fake):
    preview = await lib.set_citekeys()
    assert preview["kept_existing_keys"] == 0 and preview["would_change"] == 3
    res = await lib.set_citekeys(dry_run=False)
    assert res["applied"] == 3
    # A (added 2024-01) gets the bare key, B (2024-02) gets the suffix
    assert fake.items["AAAA1111"]["extra"] == "Citation Key: jacinto2026"
    assert fake.items["BBBB2222"]["extra"] == "Citation Key: jacinto2026a"
    assert fake.items["DDDD4444"]["extra"] == "Citation Key: fonseca2019"
    assert fake.items["CCCC3333"]["extra"].startswith("Citation Key: west1974")
    again = await lib.set_citekeys(keys=["AAAA1111"])
    assert again["kept_existing_keys"] == 1 and again["would_change"] == 0


async def test_update_fields_guards(lib, fake):
    with pytest.raises(ZoteroError):
        await lib.update_fields("AAAA1111", {"tags": []})
    with pytest.raises(ZoteroError) as err:
        await lib.update_fields("AAAA1111", {"titel": "x"})
    assert "Valid:" in str(err.value)
    res = await lib.update_fields("AAAA1111", {"date": "2025-12-01"}, dry_run=False)
    assert res["applied"] == 1 and fake.items["AAAA1111"]["date"] == "2025-12-01"


# ---------------------------------------------------------------- collections, notes, trash

async def test_collections_notes_trash(lib, fake):
    ck = fake.add_collection("Asthma")
    res = await lib.file_items(["BBBB2222"], ck, dry_run=False)
    assert res["applied"] == 1 and fake.items["BBBB2222"]["collections"] == [ck]
    created = await lib.create_collection("Lung function", dry_run=False)
    assert fake.collections[created["created"]]["name"] == "Lung function"
    note = await lib.create_note("AAAA1111", "# Summary\n\n- point one", dry_run=False)
    assert "<h1>Summary</h1>" in fake.items[note["created"]]["note"]
    res = await lib.trash_items(["DDDD4444"], dry_run=False)
    assert res["applied"] == 1 and fake.items["DDDD4444"]["deleted"] is True
    assert (await lib.overview())["items"] == 3
    await lib.undo(dry_run=False)  # restores DDDD4444
    assert fake.items["DDDD4444"]["deleted"] is False
    await lib.undo(dry_run=False)  # trashes the note
    assert fake.items[note["created"]]["deleted"] is True


# ---------------------------------------------------------------- auth

async def test_single_use_keys_and_denial(lib, fake):
    fake.remember = False
    await lib.tag_items([{"key": "AAAA1111", "add": ["topic/spirometry"]}], dry_run=False)
    res = await lib.tag_items([{"key": "BBBB2222", "add": ["topic/asthma"]}], dry_run=False)
    assert fake.auth_prompts == 2 and "single-use" in res["note"]
    fake.deny = True
    res = await lib.tag_items([{"key": "DDDD4444", "add": ["topic/asthma"]}], dry_run=False)
    assert res["applied"] == 0 and "denied" in res["error"] and res["journal_id"] is None
    assert ("topic/asthma", 0) not in tags_of(fake, "DDDD4444")


async def test_remembered_key_is_saved_and_revocation_recovers(lib, fake, tmp_path):
    await lib.tag_items([{"key": "AAAA1111", "add": ["topic/spirometry"]}], dry_run=False)
    saved = json.loads((tmp_path / "state" / "keys.json").read_text())
    assert saved[fake.server_id] in fake.keys
    fake.keys.clear()  # user clicks "Clear Write Authorizations"
    res = await lib.tag_items([{"key": "BBBB2222", "add": ["topic/asthma"]}], dry_run=False)
    assert res["applied"] == 1 and fake.auth_prompts == 2


async def test_batches_of_fifty(lib, fake):
    keys = [fake.add_item(title=f"Paper {n}", date="2020") for n in range(120)]
    res = await lib.tag_items([{"key": k, "add": ["status/to-read"]} for k in keys], dry_run=False)
    assert res["applied"] == 120
    assert fake.write_requests == 3


async def test_missing_vocabulary_blocks_tag_writes(lib, fake, tmp_path):
    (tmp_path / "zotero-tags.md").unlink()
    with pytest.raises(LookupError):
        await lib.tag_items([{"key": "AAAA1111", "add": ["topic/spirometry"]}])
    assert (await lib.status())["vocabulary"]["loaded"] is False


def test_vocabulary_parsing(tmp_path):
    from zotero_local_mcp.vocab import Vocabulary
    p = tmp_path / "v.md"
    p.write_text(
        "---\nrequired_facets:\n  - topic\n  - status\nsingle_facets: [status]\n---\n"
        "## topic\n- `topic/asthma` — Asthma. aliases: Asthma, bronchial asthma\n"
        "- `topic/asthma` dup\n- `nofacet`\n* `status/read`\n"
    )
    v = Vocabulary.load(p)
    assert v.required_facets == ["topic", "status"] and v.single_facets == ["status"]
    assert v.entries["topic/asthma"].aliases == ["Asthma", "bronchial asthma"]
    assert v.alias_map()["bronchial asthma"] == "topic/asthma"
    assert any("duplicate" in x for x in v.problems) and any("no facet" in x for x in v.problems)
    assert v.allows("_agent") and v.allows("status/read") and not v.allows("topic/x")


# ---------------------------------------------------------------- review findings

async def test_partial_batches_are_journaled_and_reported(lib, fake):
    fake.remember = False
    keys = [fake.add_item(title=f"P{n}", date="2020") for n in range(60)]
    orig = fake.handle
    state = {"auth": 0}

    def deny_second(method, url, headers, body):
        if url.endswith("/local/authorize"):
            state["auth"] += 1
            fake.deny = state["auth"] >= 2
        return orig(method, url, headers, body)

    fake.handle = deny_second
    res = await lib.tag_items([{"key": k, "add": ["status/to-read"]} for k in keys], dry_run=False)
    assert res["applied"] == 50 and len(res["not_sent"]) == 10
    assert "denied" in res["error"] and res["journal_id"]
    assert (await lib.history())[0]["items"] == 50


async def test_connection_loss_mid_write_is_reported(lib, fake):
    import httpx
    keys = [fake.add_item(title=f"P{n}", date="2020") for n in range(60)]
    orig = fake.handle
    state = {"posts": 0}

    def drop(method, url, headers, body):
        if method == "POST" and url.endswith("/items"):
            state["posts"] += 1
            if state["posts"] == 2:
                raise httpx.ReadTimeout("timed out")
        return orig(method, url, headers, body)

    fake.handle = drop
    res = await lib.tag_items([{"key": k, "add": ["status/to-read"]} for k in keys], dry_run=False)
    assert res["applied"] == 50 and "Lost the connection" in res["error"]


async def test_adding_tag_that_exists_as_automatic_makes_it_manual(lib, fake):
    fake.items["AAAA1111"]["tags"].append({"tag": "topic/spirometry", "type": 1})
    await lib.tag_items([{"key": "AAAA1111", "add": ["topic/spirometry"]}], dry_run=False)
    names = [t for t in tags_of(fake, "AAAA1111") if t[0] == "topic/spirometry"]
    assert names == [("topic/spirometry", 0)]


async def test_force_citekey_does_not_free_a_shared_key(lib, fake):
    fake.items["AAAA1111"]["extra"] = "Citation Key: jacinto2026"
    fake.items["BBBB2222"]["extra"] = "Citation Key: jacinto2026"
    s = fake.add_item(title="S", date="2026", creators=[{"creatorType": "author", "lastName": "Jacinto"}],
                      dateAdded="2026-01-01T00:00:00Z")
    res = await lib.set_citekeys(keys=["AAAA1111", s], force=True, dry_run=False)
    assert res["applied"] == 2
    assert fake.items[s]["extra"] != "Citation Key: jacinto2026"
    assert fake.items["BBBB2222"]["extra"] == "Citation Key: jacinto2026"


async def test_key_is_not_reused_across_databases(lib, fake):
    await lib.tag_items([{"key": "AAAA1111", "add": ["topic/spirometry"]}], dry_run=False)
    old_key = lib.z._key
    fake.server_id = "srvOTHER002"
    fake.keys.clear()
    await lib.overview()  # read triggers 412 -> reconnect
    assert lib.z._key != old_key
    res = await lib.tag_items([{"key": "BBBB2222", "add": ["topic/asthma"]}], dry_run=False)
    assert res["applied"] == 1 and fake.auth_prompts == 2
