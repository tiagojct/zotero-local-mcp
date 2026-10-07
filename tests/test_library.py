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
    # the new collection is journaled; undo moves it to the trash, and undoing that brings it back
    preview = await lib.undo(dry_run=True)
    assert preview["would_change"] == 1 and preview["undoing"]["op"] == "create_collection"
    res = await lib.undo(dry_run=False)
    assert res["applied"] == 1 and fake.collections[created["created"]]["deleted"] is True
    await lib.undo(res["journal_id"], dry_run=False)
    assert fake.collections[created["created"]]["deleted"] is False


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
    saved = json.loads((tmp_path / "state" / "keys.json").read_text(encoding="utf-8"))
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
    , encoding="utf-8")
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


async def test_fulltext_prefers_pdf_and_pages(lib, fake):
    html_att = fake.add_item(itemType="attachment", parentItem="AAAA1111", title="Snapshot",
                             contentType="text/html")
    pdf = fake.add_item(itemType="attachment", parentItem="AAAA1111", title="Full Text PDF",
                        contentType="application/pdf")
    fake.fulltext[html_att] = {"content": "html text", "indexedChars": 9, "totalChars": 9}
    fake.fulltext[pdf] = {"content": "x" * 2500, "indexedPages": 3, "totalPages": 3}
    part = await lib.get_fulltext("AAAA1111", max_chars=1000)
    assert part["attachment"] == pdf and part["total_chars"] == 2500 and part["next_offset"] == 1000
    last = await lib.get_fulltext("AAAA1111", offset=2000, max_chars=1000)
    assert last["text"] == "x" * 500 and last["next_offset"] is None
    with pytest.raises(ZoteroError, match="no attachment"):
        await lib.get_fulltext("CCCC3333")
    fake.add_item(itemType="attachment", parentItem="CCCC3333", title="PDF", contentType="application/pdf")
    with pytest.raises(ZoteroError, match="Reindex"):
        await lib.get_fulltext("CCCC3333")


async def test_find_by_several_tags(lib):
    assert (await lib.find(tags=["Asthma", "status/read"]))["total"] == 1
    assert (await lib.find(tags=["Asthma", "status/to-read"]))["total"] == 0
    assert (await lib.find(tags=["Spirometry"]))["items"][0]["key"] == "AAAA1111"  # automatic tag


# ---------------------------------------------------------------- re-tagging (after the model test)

def _limits_vocab(lib, tmp_path):
    from conftest import VOCAB
    v = tmp_path / "limits.md"
    v.write_text(VOCAB.replace("single_facets: status, type", "single_facets: status\nmax_per_facet: topic=2, type: 1"), encoding="utf-8")
    lib.vocab = type(lib.vocab)(v)
    return lib.vocab.require()


async def test_limits_and_replace(lib, fake, tmp_path):
    vocab = _limits_vocab(lib, tmp_path)
    assert vocab.max_per_facet == {"topic": 2, "type": 1}
    await lib.tag_items([{"key": "AAAA1111", "add": ["topic/spirometry", "topic/asthma"]}], dry_run=False)
    # a third topic is refused for that item, with a reason
    res = await lib.tag_items([{"key": "AAAA1111", "add": ["topic/clinical-decision-support"]}], dry_run=True)
    assert res["would_change"] == 0 and "at most 2" in res["skipped"]["AAAA1111"]
    # with replace, the topic tags become exactly the added ones
    res = await lib.tag_items([{"key": "AAAA1111", "add": ["topic/clinical-decision-support"],
                                "replace": ["topic"]}], dry_run=False)
    tags = {t["tag"] for t in fake.items["AAAA1111"]["tags"]}
    assert "topic/clinical-decision-support" in tags and "topic/spirometry" not in tags and "topic/asthma" not in tags
    with pytest.raises(ZoteroError, match="unknown facet"):
        await lib.tag_items([{"key": "AAAA1111", "add": [], "replace": ["topics"]}])


async def test_apply_tag_review(lib, fake, tmp_path):
    _limits_vocab(lib, tmp_path)
    fake.items["BBBB2222"]["tags"] = [{"tag": "topic/asthma"}, {"tag": "topic/spirometry"},
                                      {"tag": "status/read"}, {"tag": "_agent"}, {"tag": "Asthma", "type": 1}]
    note = tmp_path / "Zotero tag review 13.md"
    note.write_text(
        "---\nbatch: 13\n---\n\n| Key | Citekey | Item | Current tags | Proposed tags | Reason |\n"
        "|---|---|---|---|---|---|\n"
        "| BBBB2222 | x2026 | Asthma control | topic/asthma, topic/spirometry | `topic/clinical-decision-support`, type/cohort | |\n"
        "| AAAA1111 | y2026 | Spirometry | | skip | not sure |\n"
        "| CCCC3333 | west1974 | Book | | topic/spirometry; status/to-read | |\n", encoding="utf-8")
    prev = await lib.apply_tag_review(str(note))
    assert prev["dry_run"] and prev["rows"] == 2 and prev["would_change"] == 2
    res = await lib.apply_tag_review(str(note), dry_run=False)
    assert res["applied"] == 2 and res["journal_id"]
    b = {(t["tag"], t.get("type", 0)) for t in fake.items["BBBB2222"]["tags"]}
    # topic and type replaced, status kept, marker removed, automatic tag untouched
    assert b == {("topic/clinical-decision-support", 0), ("type/cohort", 0), ("status/read", 0), ("Asthma", 1)}
    c = {t["tag"] for t in fake.items["CCCC3333"]["tags"]}
    assert c == {"topic/spirometry", "status/to-read"}
    assert "Applied" in note.read_text(encoding="utf-8") and res["journal_id"] in note.read_text(encoding="utf-8")
    # undo restores the earlier tags, including the marker
    await lib.undo(res["journal_id"], dry_run=False)
    assert {"topic/spirometry", "_agent"} <= {t["tag"] for t in fake.items["BBBB2222"]["tags"]}


async def test_apply_tag_review_refuses_bad_rows(lib, fake, tmp_path):
    _limits_vocab(lib, tmp_path)
    note = tmp_path / "bad.md"
    note.write_text("| Key | Proposed tags |\n|---|---|\n"
                    "| BBBB2222 | topic/asthma, topic/spirometry, topic/clinical-decision-support |\n"
                    "| AAAA1111 | topic/made-up |\n| nokey | topic/asthma |\n", encoding="utf-8")
    with pytest.raises(ZoteroError) as exc:
        await lib.apply_tag_review(str(note))
    msg = str(exc.value)
    assert "3 topic/ tags (at most 2)" in msg and "made-up" in msg and "not a Zotero item key" in msg
    assert fake.write_requests == 0


async def test_tag_audit(lib, fake, tmp_path):
    _limits_vocab(lib, tmp_path)
    for n in range(12):
        fake.add_item(key=f"PAIR{n:04d}".replace("0", "Q"), title=f"Paper {n}",
                      tags=[{"tag": "topic/spirometry"}, {"tag": "topic/asthma"}, {"tag": "_agent"}])
    fake.items["AAAA1111"]["tags"] = [{"tag": "topic/spirometry"}, {"tag": "topic/asthma"},
                                      {"tag": "topic/clinical-decision-support"}, {"tag": "status/read"},
                                      {"tag": "status/to-read"}]
    res = await lib.tag_audit()
    over = {r["key"]: r["problems"] for r in res["over_limit"]["items"]}
    assert any("3 topic/" in p for p in over["AAAA1111"]) and any("status/" in p for p in over["AAAA1111"])
    assert res["sticky_pairs"][0]["tags"] == ["topic/asthma", "topic/spirometry"]
    assert "AAAA1111" in {r["key"] for r in res["without_review_marker"]["items"]}


async def test_apply_tag_review_once_and_changed_since_note(lib, fake, tmp_path):
    _limits_vocab(lib, tmp_path)
    fake.items["BBBB2222"]["tags"] = [{"tag": "topic/asthma"}, {"tag": "status/read"}, {"tag": "_agent"}]
    note = tmp_path / "review.md"
    note.write_text("| Key | Current tags | Proposed tags |\n|---|---|---|\n"
                    "| BBBB2222 | topic/asthma | topic/spirometry |\n", encoding="utf-8")
    fake.touch("BBBB2222", tags=[{"tag": "topic/asthma"}, {"tag": "topic/clinical-decision-support"},
                                 {"tag": "status/read"}, {"tag": "_agent"}])
    prev = await lib.apply_tag_review(str(note))
    ch = prev["changed_since_note"]
    assert ch["count"] == 1 and ch["items"]["BBBB2222"]["now"] == ["topic/asthma", "topic/clinical-decision-support"]
    res = await lib.apply_tag_review(str(note), dry_run=False)
    assert res["applied"] == 1
    # a second apply is refused; the preview says so; again=True applies
    with pytest.raises(ZoteroError, match="already applied"):
        await lib.apply_tag_review(str(note), dry_run=False)
    prev2 = await lib.apply_tag_review(str(note))
    assert prev2["already_applied"] and "again=true" in prev2["next"]
    res2 = await lib.apply_tag_review(str(note), dry_run=False, again=True)
    assert res2["applied"] == 0


async def test_write_tag_review(lib, fake, tmp_path):
    _limits_vocab(lib, tmp_path)
    vault = tmp_path / "vault"
    (vault / "Inbox").mkdir(parents=True)
    lib.s = type(lib.s)(**{**lib.s.__dict__, "vault": vault})
    fake.items["BBBB2222"]["tags"] = [{"tag": "Asthma"}, {"tag": "status/read"}, {"tag": "_agent"}]
    res = await lib.write_tag_review("Zotero tag review 40", [
        {"key": "BBBB2222", "tags": ["topic/asthma", "type/cohort"], "reason": "a | pipe"},
        {"key": "AAAA1111", "tags": "topic/spirometry, topic/asthma", "reason": ""},
    ], intro="Test batch.")
    assert res["path"] == "Inbox/Zotero tag review 40.md" and res["rows"] == 2
    text = (vault / res["path"]).read_text(encoding="utf-8")
    assert "| BBBB2222 |  | Jacinto 2026, Asthma control in primary care | Asthma | topic/asthma, type/cohort | a / pipe |" in text
    assert "Test batch." in text and "_agent" not in text.split("| Key |")[1]
    # the note applies as written
    assert (await lib.apply_tag_review(res["path"]))["rows"] == 2
    # never overwrites; refuses bad rows and paths outside the vault; writes nothing to Zotero
    with pytest.raises(ZoteroError, match="already exists"):
        await lib.write_tag_review("Zotero tag review 40", [{"key": "BBBB2222", "tags": ["topic/asthma"]}])
    with pytest.raises(ZoteroError) as exc:
        await lib.write_tag_review("x", [{"key": "BBBB2222", "tags": ["topic/made-up"]},
                                         {"key": "ZZZZ9999", "tags": ["topic/asthma"]},
                                         {"key": "AAAA1111", "tags": ["type/cohort"]}])
    msg = str(exc.value)
    assert "made-up" in msg and "ZZZZ9999: not in the library" in msg and "AAAA1111: no topic/ tag" in msg
    assert not (vault / "Inbox" / "x.md").exists()
    with pytest.raises(ZoteroError, match="outside the vault"):
        await lib.write_tag_review("../../escape", [{"key": "BBBB2222", "tags": ["topic/asthma"]}])
    assert fake.write_requests == 0


async def test_review_command(lib, fake, tmp_path, capsys):
    import argparse
    from zotero_local_mcp import review
    _limits_vocab(lib, tmp_path)
    vault = tmp_path / "vault"
    (vault / "Inbox").mkdir(parents=True)
    lib.s = type(lib.s)(**{**lib.s.__dict__, "vault": vault})
    for n, (key, tag) in enumerate([("AAAA1111", "topic/spirometry"), ("BBBB2222", "topic/asthma")], start=9):
        (vault / "Inbox" / f"Zotero tag review {n}.md").write_text(f"| Key | Proposed tags |\n|---|---|\n| {key} | {tag} |\n", encoding="utf-8")
    (vault / "Inbox" / "Zotero tag review 10.md").write_text(
        (vault / "Inbox" / "Zotero tag review 10.md").read_text() + "\nApplied 2026-09-01: 1 items (journal x).\n", encoding="utf-8")

    def ns(cmd, *notes, **kw):
        return argparse.Namespace(cmd=cmd, notes=list(notes), yes=kw.get("yes", False), again=False,
                                  keep_marker=False, verbose=False)

    assert await review.run(ns("preview", "Inbox/Zotero tag review *.md"), lib) == 0
    out = capsys.readouterr().out
    assert out.index("review 9.md") < out.index("review 10.md") and "already applied" in out
    assert "1 notes, 1 items would change" in out and fake.write_requests == 0
    assert "no Current tags column" in out
    assert await review.run(ns("apply", "Inbox/Zotero tag review *.md", yes=True), lib) == 0
    out = capsys.readouterr().out
    assert "Zotero tag review 9.md: applied 1" in out
    assert "topic/spirometry" in {t["tag"] for t in fake.items["AAAA1111"]["tags"]}
    assert "topic/asthma" not in {t["tag"] for t in fake.items["BBBB2222"]["tags"]}
    assert await review.run(ns("preview", "Inbox/nothing*.md"), lib) == 2
    capsys.readouterr()
    # a range of note numbers; missing numbers are reported
    assert await review.run(ns("preview", "9-11"), lib) == 0
    out = capsys.readouterr().out
    assert "No note matches: Zotero tag review 11.md" in out and "review 9.md: already applied" in out


async def test_field_change_is_not_reapplied_over_a_concurrent_edit(lib, fake):
    orig = lib.z.items_by_keys
    calls = {"n": 0}

    async def racing(keys):
        out = await orig(keys)
        calls["n"] += 1
        if calls["n"] == 1:
            fake.touch("AAAA1111", title="Edited in Zotero")
        return out

    lib.z.items_by_keys = racing
    res = await lib.update_fields("AAAA1111", {"title": "Model's title"}, dry_run=False)
    assert res["applied"] == 0
    assert "changed in Zotero" in res["skipped"]["AAAA1111"]
    assert fake.items["AAAA1111"]["title"] == "Edited in Zotero"  # the user's edit stays


async def test_only_listed_tags_and_the_marker_can_be_added(lib, fake):
    with pytest.raises(ZoteroError) as err:
        await lib.tag_items([{"key": "AAAA1111", "add": ["_anything"]}])
    assert "'_anything' is not in the vocabulary" in str(err.value)
    res = await lib.tag_items([{"key": "AAAA1111", "add": ["topic/spirometry"]}], dry_run=False)
    assert res["applied"] == 1 and ("_agent", 0) in tags_of(fake, "AAAA1111")


async def test_undo_takes_journal_ids_only(lib, tmp_path):
    evil = tmp_path / "evil.json"
    evil.write_text('{"id": "x", "op": "tag_items", "changes": []}', encoding="utf-8")
    for bad in [str(tmp_path / "evil"), "../../evil", "20261007-093000-00-tag_items/../../x"]:
        with pytest.raises(Exception):
            await lib.undo(journal_id=bad)


async def test_review_notes_outside_the_vault_are_refused(lib, tmp_path):
    import dataclasses
    (tmp_path / "vault").mkdir()
    lib.s = dataclasses.replace(lib.s, vault=tmp_path / "vault")
    outside = tmp_path / "elsewhere.md"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(ZoteroError) as err:
        lib._note_path(str(outside))
    assert "outside the vault" in str(err.value)


async def test_create_note_keeps_raw_html_as_text(lib, fake):
    preview = await lib.create_note("AAAA1111", "Hello <img src=x onerror=alert(1)> **bold**")
    assert "<img" in preview["note_preview"]  # shown whole, as written
    await lib.create_note("AAAA1111", "Hello <img src=x onerror=alert(1)> **bold**", dry_run=False)
    note = next(i for i in fake.items.values() if i.get("itemType") == "note" and "Hello" in i.get("note", ""))
    assert "<img" not in note["note"] and "&lt;img" in note["note"] and "<strong>bold</strong>" in note["note"]
