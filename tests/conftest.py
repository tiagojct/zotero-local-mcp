from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_zotero import FakeZotero  # noqa: E402
from zotero_local_mcp.client import LocalZotero  # noqa: E402
from zotero_local_mcp.config import Settings  # noqa: E402
from zotero_local_mcp.library import Library  # noqa: E402

VOCAB = """\
---
required_facets: topic, status
single_facets: status, type
---
# Vocabulary

## topic
- `topic/spirometry` Lung function testing. aliases: Spirometry, pft
- `topic/asthma` Asthma. aliases: Asthma
- `topic/clinical-decision-support` CDSS. aliases: CDSS, decision support

## method
- `method/reference-equations` Building reference equations.

## type
- `type/cohort`
- `type/systematic-review`
- `type/textbook`

## status
- `status/to-read`
- `status/read`
"""


@pytest.fixture
def fake() -> FakeZotero:
    z = FakeZotero()
    z.add_item(key="AAAA1111", title="Spirometry reference values in adults",
               creators=[{"creatorType": "author", "firstName": "Tiago", "lastName": "Jacinto"}],
               date="2026-03-01", abstractNote="GLI equations...",
               tags=[{"tag": "Spirometry", "type": 1}, {"tag": "Lung Function Tests", "type": 1}],
               dateAdded="2024-01-01T00:00:00Z")
    z.add_item(key="BBBB2222", title="Asthma control in primary care",
               creators=[{"creatorType": "author", "firstName": "T", "lastName": "Jacinto"}],
               date="2026", tags=[{"tag": "Asthma"}, {"tag": "status/read"}],
               dateAdded="2024-02-01T00:00:00Z")
    z.add_item(key="CCCC3333", itemType="book", title="Pulmonary Physiology",
               creators=[{"creatorType": "author", "firstName": "John", "lastName": "West"}],
               date="1974", extra="Citation Key: west1974\nOCLC: 123",
               dateAdded="2023-01-01T00:00:00Z")
    z.add_item(key="DDDD4444", title="Decision support in lung function",
               creators=[{"creatorType": "author", "firstName": "João", "lastName": "Fonsêca"}],
               date="2019-05", tags=[{"tag": "CDSS"}], dateAdded="2025-01-01T00:00:00Z")
    z.add_item(key="NOTE0001", itemType="note", parentItem="BBBB2222", note="<p>My <b>note</b></p>")
    return z


@pytest.fixture
def lib(fake: FakeZotero, tmp_path: Path) -> Library:
    vocab = tmp_path / "zotero-tags.md"
    vocab.write_text(VOCAB, encoding="utf-8")
    settings = Settings(api_url="http://127.0.0.1:23119/api", vocab_path=vocab,
                        marker="_agent", state_dir=tmp_path / "state", auth_timeout=5)
    client = LocalZotero(settings.api_url, settings.state_dir, 5, transport=fake.transport())
    return Library(settings, client)
