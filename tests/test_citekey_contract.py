"""The shared citekey cases. zotero-gam (citekey.mjs) runs an exact copy of the same file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotero_local_mcp.citekey import base_key, current_key, unique_key

CASES = json.loads((Path(__file__).parent / "fixtures" / "citekey-cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES["base_key"], ids=lambda c: c["name"])
def test_base_key(case):
    assert base_key(case["data"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["current_key"], ids=lambda c: c["name"])
def test_current_key(case):
    assert current_key(case["data"]) == case["expected"]


@pytest.mark.parametrize("case", CASES["unique_key"], ids=lambda c: c["name"])
def test_unique_key(case):
    assert unique_key(case["base"], set(case["taken"])) == case["expected"]
