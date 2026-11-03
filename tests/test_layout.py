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
