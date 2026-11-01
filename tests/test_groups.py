from __future__ import annotations

import pytest

from zotero_local_mcp.client import ZoteroError, in_library, library_path


@pytest.fixture
def group(fake):
    g = fake.add_group(4242, "Lung function lab")
    g.add_item(key="GGGG1111", title="Spirometry in the lab group",
               creators=[{"creatorType": "author", "firstName": "Ana", "lastName": "Silva"}], date="2025")
    return g


def test_library_path():
    assert library_path(None) == "users/0"
    assert library_path("user") == "users/0"
    assert library_path("group:4242") == "groups/4242"
    assert library_path("4242") == "groups/4242"
    for bad in ("group:abc", "groups/1", "group:", "../users/1"):
        with pytest.raises(ZoteroError):
            library_path(bad)


async def test_list_libraries(lib, group):
    res = await lib.list_libraries()
    assert res["libraries"][0] == {"library": "user", "name": "My Library", "writable": True}
    assert {"library": "group:4242", "name": "Lung function lab", "writable": False} in res["libraries"]


async def test_reads_in_a_group(lib, group):
    with in_library("group:4242"):
        found = await lib.find(query="spirometry")
        item = await lib.get_item("GGGG1111")
    assert [i["key"] for i in found["items"]] == ["GGGG1111"]
    assert found["library"] == "group:4242"
    assert item["zotero_link"] == "zotero://select/groups/4242/items/GGGG1111"
    # outside the block, the user's library again
    assert "GGGG1111" not in {i["key"] for i in (await lib.find(query="spirometry"))["items"]}
    assert "library" not in await lib.find(query="spirometry")


async def test_unknown_group(lib, group):
    with in_library("group:7"), pytest.raises(ZoteroError, match="Not found"):
        await lib.find(query="x")


async def test_no_writes_in_a_group(lib, group, fake):
    before = fake.write_requests
    with in_library("group:4242"):
        res = await lib.tag_items([{"key": "GGGG1111", "add": ["topic/spirometry"]}], dry_run=False)
    assert res["applied"] == 0 and "read-only" in res["error"] and res["not_sent"] == ["GGGG1111"]
    assert fake.write_requests == before
