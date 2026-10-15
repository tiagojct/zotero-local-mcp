from __future__ import annotations

from pathlib import Path

import pytest

from zotero_local_mcp.client import LocalZotero, ZoteroError
from zotero_local_mcp.config import Settings
from zotero_local_mcp.figures import parse_label
from zotero_local_mcp.library import Library


def make_pdf(path: Path) -> None:
    """Two pages. Page 1 mentions Figure 1 in a sentence; page 2 has a blue box with its caption
    under it, a paragraph, and a table between three rules under its caption."""
    page1 = b"BT /F1 11 Tf 72 700 Td (Page one. As Figure 1 shows, the box is blue.) Tj ET\n"
    page2 = b"""0 0 1 rg 100 450 300 200 re f
BT /F1 11 Tf 100 430 Td (Figure 1. A blue box for the test.) Tj ET
BT /F1 11 Tf 100 380 Td (Body text that is not part of the figure goes here.) Tj ET
BT /F1 11 Tf 100 330 Td (Table 1. Counts by group.) Tj ET
0 g 100 318 300 1 re f
BT /F1 10 Tf 100 305 Td (Group n) Tj ET
0 g 100 298 300 0.5 re f
BT /F1 10 Tf 100 285 Td (A 12) Tj ET
BT /F1 10 Tf 100 272 Td (B 30) Tj ET
0 g 100 265 300 1 re f
BT /F1 11 Tf 100 200 Td (More body text after the table.) Tj ET
"""

    def stream(c: bytes) -> bytes:
        return b"<< /Length %d >>\nstream\n" % len(c) + c + b"\nendstream"

    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>",
        stream(page1),
        stream(page2),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))


@pytest.fixture
def setup(fake, tmp_path: Path):
    pdf = tmp_path / "Smith 2021.pdf"
    make_pdf(pdf)
    fake.add_item(key="PDF00001", itemType="attachment", parentItem="AAAA1111", contentType="application/pdf",
                  linkMode="imported_file", title="Smith 2021.pdf", _file=str(pdf))
    vault = tmp_path / "vault"
    settings = Settings(api_url="http://127.0.0.1:23119/api", vocab_path=tmp_path / "v.md", marker="_agent",
                        state_dir=tmp_path / "state", auth_timeout=5, vault=vault)
    return Library(settings, LocalZotero(settings.api_url, settings.state_dir, 5, transport=fake.transport())), vault


def test_labels():
    assert parse_label("Figure 2") == ("figure", "2")
    assert parse_label("Fig. 3b") == ("figure", "3b")
    assert parse_label("table 1") == ("table", "1")
    with pytest.raises(ValueError):
        parse_label("the second figure")


async def test_figure_is_cut_out_above_its_caption(setup):
    lib, vault = setup
    out = await lib.get_figure("AAAA1111", "Figure 1")
    assert out["page"] == 2, "the caption, not the mention on page 1"
    assert out["method"] == "crop"
    left, bottom, right, top = out["box"]
    assert 90 <= left <= 100 and 395 <= right <= 410, "the width of the box"
    assert 440 <= bottom <= 450 and 650 <= top <= 660, "the box, without the caption (y 430) or the text"
    image = vault / out["image"]
    assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert out["image"].startswith("Literature/figures/") and out["markdown"].startswith("![Figure 1. A blue box for the test.](figures/")
    assert out["pdf_link"] == "zotero://open-pdf/library/items/PDF00001?page=2"
    assert out["width"] == round((right - left) * 2) or abs(out["width"] - (right - left) * 2) <= 2


async def test_table_runs_between_its_rules(setup):
    lib, _ = setup
    out = await lib.get_figure("AAAA1111", "Table 1")
    assert out["method"] == "crop"
    _, bottom, _, top = out["box"]
    assert 255 <= bottom <= 266, "down to the last rule"
    assert 320 <= top <= 330, "up to the caption, which is left out"


async def test_no_caption_no_pdf(setup, fake):
    lib, _ = setup
    with pytest.raises(ValueError, match="No caption"):
        await lib.get_figure("AAAA1111", "Figure 9")
    with pytest.raises(ZoteroError, match="no PDF"):
        await lib.get_figure("BBBB2222", "Figure 1")
