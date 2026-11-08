from __future__ import annotations

import datetime as dt

import pytest

from zotero_local_mcp.config import Settings
from zotero_local_mcp.layout import dir_path, layout_style, note_name
from zotero_local_mcp.review import expand


def lower_vault(tmp_path):
    for d in ("inbox", "literature", "zotero"):
        (tmp_path / d).mkdir()
    return tmp_path


def title_vault(tmp_path):
    for d in ("Inbox", "Literature", "Zotero"):
        (tmp_path / d).mkdir()
    return tmp_path


def test_style_lower(tmp_path):
    assert layout_style(lower_vault(tmp_path)) == "lower"
    assert dir_path(tmp_path, "Inbox") == tmp_path / "inbox"


def test_style_title_and_empty(tmp_path):
    assert layout_style(tmp_path / "missing") == "title"
    assert layout_style(tmp_path) == "title"
    assert layout_style(title_vault(tmp_path)) == "title"
    assert dir_path(tmp_path, "Inbox") == tmp_path / "Inbox"


def test_style_uses_exact_names_not_exists(tmp_path):
    # On a case-insensitive disk (APFS), (tmp/"inbox").exists() is true for Inbox/.
    title_vault(tmp_path)
    assert layout_style(tmp_path) == "title"


VECTORS = [
    ("A baleia branca simboliza o que não pode ser conhecido", "a-baleia-branca-simboliza-o-que-nao-pode-ser-conhecido"),
    ("α-Klotho", "alpha-klotho"),
    ("2026-10-19 AFLRSI | Avaliação", "2026-10-19_aflrsi-avaliacao"),
    ("1998-05-20 - O Enforcado", "1998-05-20_o-enforcado"),
    ("2026-08-20", "2026-08-20"),
    ("7ºCNFC - Interpretar a função respiratória na era da IA", "7cnfc-interpretar-a-funcao-respiratoria-na-era-da-ia"),
    ("ERS Learning Resources Director 2027–2030 — Interview Prep", "ers-learning-resources-director-2027-2030-interview-prep"),
    ("This Is a Rare Arena in Which People Think A.I. Is Helping. It’s Making Things Worse.",
     "this-is-a-rare-arena-in-which-people-think-a-i-is-helping-its-making-things"),
    ("qd-2026-05-06_workshop-sw-2ed", "qd-2026-05-06_workshop-sw-2ed"),
    ("_starbuck", "_starbuck"),
    ("Pasted image 20260831155638", "pasted-image-20260831155638"),
    ("Reuniões", "reunioes"),
    ("Reuniões", "reunioes"),
]


@pytest.mark.parametrize("title,expected", VECTORS)
def test_note_name_vectors(tmp_path, title, expected):
    assert note_name(lower_vault(tmp_path), title) == expected


def test_title_style_unchanged(tmp_path):
    title_vault(tmp_path)
    assert note_name(tmp_path, "Zotero import queue") == "Zotero import queue"
    assert Settings(vault=tmp_path).queue_path == tmp_path / "Inbox" / "Zotero import queue.md"


def test_import_queue_lower(tmp_path):
    lower_vault(tmp_path)
    assert Settings(vault=tmp_path).queue_path == tmp_path / "inbox" / "zotero-import-queue.md"


def test_settings_defaults_lower(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_ENV", str(tmp_path / "none"))
    for k in ("ZOTERO_VOCAB", "ZOTERO_ALERTS"):
        monkeypatch.delenv(k, raising=False)
    vault = lower_vault(tmp_path)
    monkeypatch.setenv("ZOTERO_VAULT", str(vault))
    s = Settings.from_env()
    assert s.alerts_config == vault / "zotero" / "literature-alerts.md"
    assert s.vocab_path == vault / "zotero" / "zotero-tags.md"
    monkeypatch.setenv("ZOTERO_ALERTS", str(tmp_path / "mine.md"))
    monkeypatch.setenv("ZOTERO_VOCAB", str(tmp_path / "v.md"))
    s = Settings.from_env()
    assert s.alerts_config == tmp_path / "mine.md"
    assert s.vocab_path == tmp_path / "v.md"


def test_settings_defaults_title(tmp_path, monkeypatch):
    monkeypatch.setenv("ZOTERO_MCP_ENV", str(tmp_path / "none"))
    for k in ("ZOTERO_VOCAB", "ZOTERO_ALERTS"):
        monkeypatch.delenv(k, raising=False)
    vault = title_vault(tmp_path)
    monkeypatch.setenv("ZOTERO_VAULT", str(vault))
    s = Settings.from_env()
    assert s.alerts_config == vault / "Systems" / "Literature alerts.md"
    assert s.vocab_path is None


def test_alert_note_name_lower(tmp_path):
    lower_vault(tmp_path)
    name = note_name(tmp_path, f"Literature alerts {dt.date(2026, 11, 3).isoformat()}")
    assert tmp_path / "inbox" / f"{name}.md" == tmp_path / "inbox" / "literature-alerts-2026-11-03.md"
    assert note_name(tmp_path, "Literature alerts") == "literature-alerts"


def test_tag_review_lower(tmp_path):
    lower_vault(tmp_path)
    assert note_name(tmp_path, "Zotero tag review 32") + ".md" == "zotero-tag-review-32.md"


def test_review_finds_lower_note(tmp_path):
    lower_vault(tmp_path)
    note = tmp_path / "inbox" / "zotero-tag-review-32.md"
    note.write_text("x", encoding="utf-8")
    notes, missing = expand(["32"], tmp_path)
    assert notes == [note] and missing == []


def test_review_finds_title_note(tmp_path):
    title_vault(tmp_path)
    note = tmp_path / "Inbox" / "Zotero tag review 32.md"
    note.write_text("x", encoding="utf-8")
    notes, missing = expand(["32"], tmp_path)
    assert notes == [note] and missing == []


async def test_write_tag_review_lower(lib, tmp_path):
    vault = tmp_path / "vault"
    (vault / "inbox").mkdir(parents=True)
    lib.s = type(lib.s)(**{**lib.s.__dict__, "vault": vault})
    res = await lib.write_tag_review("Zotero tag review 41", [{"key": "AAAA1111", "tags": ["topic/spirometry"]}])
    assert res["path"] == "inbox/zotero-tag-review-41.md"
    assert (vault / "inbox" / "zotero-tag-review-41.md").exists()


async def test_write_tag_review_with_folder_follows_layout(lib, tmp_path):
    vault = tmp_path / "vault"
    (vault / "inbox").mkdir(parents=True)
    lib.s = type(lib.s)(**{**lib.s.__dict__, "vault": vault})
    res = await lib.write_tag_review("Inbox/Zotero tag review 45", [{"key": "AAAA1111", "tags": ["topic/spirometry"]}])
    assert res["path"] == "inbox/zotero-tag-review-45.md"
    assert not (vault / "inbox" / "Zotero tag review 45.md").exists()


async def test_write_tag_review_with_folder_title_style(lib, tmp_path):
    vault = tmp_path / "vault"
    (vault / "Inbox").mkdir(parents=True)
    lib.s = type(lib.s)(**{**lib.s.__dict__, "vault": vault})
    res = await lib.write_tag_review("Inbox/Zotero tag review 45", [{"key": "AAAA1111", "tags": ["topic/spirometry"]}])
    assert res["path"] == "Inbox/Zotero tag review 45.md"


def test_dir_path_follows_exact_folder(tmp_path):
    (tmp_path / "Inbox").mkdir()
    (tmp_path / "research").mkdir()
    assert dir_path(tmp_path, "Inbox") == tmp_path / "Inbox"
    assert dir_path(tmp_path, "Research") == tmp_path / "research"
    assert dir_path(tmp_path, "Syntheses") == tmp_path / "syntheses"  # neither exists: the style


async def test_bakeoff_reference_links(lib, tmp_path):
    from zotero_local_mcp.bakeoff import Bakeoff
    for style, inbox, ref, link in (("title", "Inbox", "Model test reference.md", "[[Zotero tags]]"),
                                    ("lower", "inbox", "model-test-reference.md", "[[zotero-tags]]")):
        vault = tmp_path / style
        (vault / inbox).mkdir(parents=True)
        lib.s = type(lib.s)(**{**lib.s.__dict__, "vault": vault, "state_dir": tmp_path / f"st-{style}"})
        await Bakeoff(lib).make_sample(n=2)
        text = (vault / inbox / ref).read_text(encoding="utf-8")
        assert link in text and "{tags_link}" not in text


def test_empty_folder_named_sub_sub_is_lower(tmp_path):
    root = tmp_path / "sub-sub"
    root.mkdir()
    assert layout_style(root) == "lower"
    assert dir_path(root, "Inbox") == root / "inbox"
    # A folder that does not exist yet.
    assert layout_style(tmp_path / "new" / "sub-sub") == "lower"
    assert layout_style(tmp_path / "other") == "title"


def test_sub_sub_with_titlecase_subfolder_is_title(tmp_path):
    root = tmp_path / "sub-sub"
    root.mkdir()
    (root / "Inbox").mkdir()
    assert layout_style(root) == "title"


def test_settings_path_uses_existing_zotero_folder(tmp_path):
    from zotero_local_mcp.layout import settings_path
    (tmp_path / "inbox").mkdir()
    (tmp_path / "zotero").mkdir()
    assert settings_path(tmp_path, "tags") == tmp_path / "zotero" / "zotero-tags.md"
    root = tmp_path / "sub-sub"
    root.mkdir()
    (root / "Zotero").mkdir()  # TitleCase folder: title style, no default for tags
    assert settings_path(root, "tags") is None


def test_settings_path_follows_mixed_zotero_name(tmp_path):
    from zotero_local_mcp.layout import settings_path
    (tmp_path / "inbox").mkdir()
    (tmp_path / "Zotero").mkdir()
    assert settings_path(tmp_path, "tags") == tmp_path / "Zotero" / "zotero-tags.md"
