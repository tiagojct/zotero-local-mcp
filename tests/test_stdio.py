"""End to end: start the MCP server over stdio against a fake Zotero HTTP server."""

from __future__ import annotations

import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from conftest import VOCAB


def payload(result):
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


async def test_stdio_roundtrip(fake, tmp_path):
    srv, url = fake.serve()
    vocab = tmp_path / "zotero-tags.md"
    vocab.write_text(VOCAB)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "zotero_local_mcp.server"],
        env={**os.environ, "ZOTERO_API_URL": url, "ZOTERO_VOCAB": str(vocab),
             "ZOTERO_MCP_STATE": str(tmp_path / "state")},
    )
    try:
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            assert {"tag_items", "find_items", "undo", "set_citekeys", "trash_items"} <= set(tools)
            assert tools["find_items"].annotations.read_only_hint is True
            schema = tools["tag_items"].input_schema
            assert schema["properties"]["dry_run"]["default"] is True

            st = payload(await s.call_tool("status", {}))
            assert st["zotero"] == "reachable"

            res = payload(await s.call_tool("tag_items", {
                "changes": [{"key": "AAAA1111", "add": ["topic/spirometry"]}], "dry_run": False}))
            assert res["applied"] == 1
            assert {"tag": "topic/spirometry"} in fake.items["AAAA1111"]["tags"]

            bad = await s.call_tool("tag_items", {"changes": [{"key": "AAAA1111", "add": ["made-up"]}]})
            assert bad.is_error and "not in the vocabulary" in bad.content[0].text

            undo = payload(await s.call_tool("undo", {"dry_run": False}))
            assert undo["applied"] == 1
            assert {"tag": "topic/spirometry"} not in fake.items["AAAA1111"]["tags"]
    finally:
        srv.shutdown()
