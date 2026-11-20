"""Keys in the Sub-Sub folder: taken by literature notes, protected when cited."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from zotero_local_mcp.vaultkeys import literature_keys, taken_in_vault, used_in_vault


def write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sub_sub(tmp_path: Path) -> Path:
    vault = tmp_path / "sub-sub"
    for d in ("inbox", "literature", "zotero"):
        (vault / d).mkdir(parents=True)
    return vault


def test_literature_keys_file_names_and_frontmatter(tmp_path):
    vault = sub_sub(tmp_path)
    write(vault, "literature/smith2020.md", "---\ntitle: x\ncitekey: smith2020\n---\n")
    write(vault, "literature/renamed.md", '---\ncitekey: "2021"\n---\n')
    write(vault, "literature/deep/Lee2019.md")
    write(vault, "literature/.hidden/no2020.md")
    write(vault, "inbox/kim2020.md")
    assert literature_keys(vault) == {"smith2020", "renamed", "2021", "Lee2019"}
    assert "lee2019" in taken_in_vault(vault)
    assert literature_keys(None) == set() and used_in_vault(None) == set()


def test_used_in_vault_has_notes_and_citations_in_lower_case(tmp_path):
    vault = sub_sub(tmp_path)
    write(vault, "literature/Smith2020.md")
    write(vault, "syntheses/topic.md", "As [@doe2019; @Roe2018] and [[kim2017|Kim]] show.")
    assert used_in_vault(vault) == {"smith2020", "doe2019", "roe2018", "kim2017"}


async def test_new_keys_skip_literature_notes_in_any_case(lib, fake, tmp_path):
    vault = sub_sub(tmp_path)
    write(vault, "literature/jacinto2026.md")
    write(vault, "literature/Fonseca2019.md")
    lib.s = replace(lib.s, vault=vault)
    await lib.set_citekeys(dry_run=False)
    assert fake.items["AAAA1111"]["extra"] == "Citation Key: jacinto2026a"
    assert fake.items["BBBB2222"]["extra"] == "Citation Key: jacinto2026b"
    assert fake.items["DDDD4444"]["extra"] == "Citation Key: fonseca2019a"


async def test_force_keeps_a_key_that_the_folder_uses(lib, fake, tmp_path):
    vault = sub_sub(tmp_path)
    write(vault, "syntheses/topic.md", "See [@jacinto2026].")
    lib.s = replace(lib.s, vault=vault)
    fake.items["AAAA1111"]["extra"] = "Citation Key: jacinto2026"
    fake.items["DDDD4444"]["extra"] = "Citation Key: oldkey"
    res = await lib.set_citekeys(keys=["AAAA1111", "DDDD4444"], force=True, dry_run=False)
    assert res["kept_cited"] == ["jacinto2026"]
    assert res["applied"] == 1
    assert fake.items["AAAA1111"]["extra"] == "Citation Key: jacinto2026"
    assert fake.items["DDDD4444"]["extra"] == "Citation Key: fonseca2019"


async def test_without_a_folder_nothing_is_protected(lib, fake):
    fake.items["DDDD4444"]["extra"] = "Citation Key: oldkey"
    res = await lib.set_citekeys(keys=["DDDD4444"], force=True, dry_run=False)
    assert res["kept_cited"] == [] and res["applied"] == 1


def test_frontmatter_with_bom_and_trailing_spaces(tmp_path):
    vault = sub_sub(tmp_path)
    write(vault, "literature/a.md", "﻿--- \ncitekey: smith2020\n---  \nBody")
    assert literature_keys(vault) == {"a", "smith2020"}


async def test_a_missing_folder_stops_set_citekeys(lib, fake, tmp_path):
    import pytest
    from zotero_local_mcp.client import ZoteroError
    lib.s = replace(lib.s, vault=tmp_path / "missing")
    fake.items["DDDD4444"]["extra"] = "Citation Key: oldkey"
    with pytest.raises(ZoteroError, match="cannot be read"):
        await lib.set_citekeys(keys=["DDDD4444"], force=True, dry_run=False)
    assert fake.items["DDDD4444"]["extra"] == "Citation Key: oldkey"
