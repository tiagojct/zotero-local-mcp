"""Figures and tables from a PDF, cut out as PNG images for notes (pypdfium2, no other library).

The caption is the anchor: "Figure 2." or "Table 1:" at the start of a line. A figure is the
graphics just above its caption (or just below, when the caption comes first), with their labels;
a table is the stretch between its horizontal rules, under the caption. When neither can be found,
the whole page is taken and the result says so.
"""

from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

Box = tuple[float, float, float, float]  # left, bottom, right, top, in PDF points (y goes up)

GRAPHIC = {pdfium_c.FPDF_PAGEOBJ_PATH, pdfium_c.FPDF_PAGEOBJ_IMAGE, pdfium_c.FPDF_PAGEOBJ_SHADING,
           pdfium_c.FPDF_PAGEOBJ_FORM}
LABEL = re.compile(r"^\s*(fig(?:ure)?\.?|table)\s*(\d+[a-z]?)\s*$", re.I)
PAD = 6.0


def parse_label(label: str) -> tuple[str, str]:
    """"Figure 2", "Fig. 2", "fig2" or "Table 1" -> ("figure", "2") or ("table", "1")."""
    m = LABEL.match(label)
    if not m:
        raise ValueError(f'"{label}" is not a figure or table label. Use the label of the caption, for example "Figure 2" or "Table 1".')
    return ("table" if m.group(1).lower() == "table" else "figure"), m.group(2).lower()


def png(width: int, height: int, stride: int, data: bytes, channels: int) -> bytes:
    """A PNG file from raw RGB or RGBA rows (standard library only)."""
    rows = b"".join(b"\x00" + data[y * stride: y * stride + width * channels] for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6 if channels == 4 else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")


def _union(a: Box | None, b: Box) -> Box:
    return b if a is None else (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _overlaps_x(a: Box, b: Box, slack: float = 20.0) -> bool:
    return a[0] < b[2] + slack and b[0] < a[2] + slack


def _caption(page: pdfium.PdfPage, kind: str, number: str) -> tuple[Box, str, bool] | None:
    """The caption line on this page: its box, its text, and whether it reads like a caption
    ("Figure 2." or "Table 1:") rather than a sentence that starts with "Figure 2 shows"."""
    tp = page.get_textpage()
    try:
        text = tp.get_text_range()
        words = r"fig(?:ure)?\.?" if kind == "figure" else r"table"
        best: tuple[Box, str, bool] | None = None
        for m in re.finditer(rf"(?:^|[\r\n])[ \t]*({words}[ \t]*{re.escape(number)})(?![\w])([^\r\n]*)", text, re.I):
            start, end = m.start(1), m.end(2)
            box: Box | None = None
            for i in range(start, end):
                if not text[i].isspace():
                    box = _union(box, tp.get_charbox(i))
            if box is None:
                continue
            caption = text[start:end].strip()
            punctuated = bool(re.match(r"[.:|–—-]", m.group(2).lstrip()))
            if punctuated:
                return box, caption, True
            best = best or (box, caption, False)
        return best
    finally:
        tp.close()


def find_caption(pdf: pdfium.PdfDocument, kind: str, number: str) -> tuple[int, Box, str] | None:
    fallback: tuple[int, Box, str] | None = None
    for i in range(len(pdf)):
        found = _caption(pdf[i], kind, number)
        if found:
            box, caption, punctuated = found
            if punctuated:
                return i, box, caption
            fallback = fallback or (i, box, caption)
    return fallback


def _grow(start: float, candidates: list[Box], up: bool, first_gap: float, gap: float) -> Box | None:
    """Join the boxes that follow one another from `start`, upwards or downwards, while the gap
    between them stays small."""
    region: Box | None = None
    edge = start
    for b in sorted(candidates, key=lambda b: b[1] if up else -b[3]):
        distance = (b[1] - edge) if up else (edge - b[3])
        if distance > (first_gap if region is None else gap):
            break
        region = _union(region, b)
        edge = region[3] if up else region[1]
    return region


def _caption_tops(page: pdfium.PdfPage) -> list[float]:
    """The tops of the caption lines on a page ("Figure 3." or "Table 2" at the start of a line)."""
    tp = page.get_textpage()
    try:
        text = tp.get_text_range()
        return [tp.get_charbox(m.start(1))[3] for m in re.finditer(r"(?:^|[\r\n])[ \t]*((?:fig(?:ure)?\.?|table)[ \t]*\d)", text, re.I)]
    finally:
        tp.close()


def _join(rules: list[Box]) -> list[Box]:
    """Rules drawn in pieces (one per column) joined into one, when the pieces meet on one line."""
    out: list[Box] = []
    for b in sorted(rules, key=lambda b: (round(b[3]), b[0])):
        last = out[-1] if out else None
        if last and abs(last[3] - b[3]) < 1.5 and b[0] <= last[2] + 3:
            out[-1] = _union(last, b)
        else:
            out.append(b)
    return out


def _empty_gap(boxes: list[Box], low: float, high: float) -> float:
    """The tallest stretch between `low` and `high` (y) that no box covers."""
    edge, widest = high, 0.0
    for b in sorted((b for b in boxes if b[1] < high and b[3] > low), key=lambda b: -b[3]):
        widest = max(widest, edge - b[3])
        edge = min(edge, b[1])
    return max(widest, edge - low)


def region(page: pdfium.PdfPage, kind: str, cap: Box) -> Box | None:
    width, height = page.get_size()
    objects = [(o.type, tuple(o.get_bounds())) for o in page.get_objects()]
    # Not a page background or a frame around the whole page.
    graphics = [b for t, b in objects if t in GRAPHIC and (b[2] - b[0]) * (b[3] - b[1]) < 0.8 * width * height]
    texts = [b for t, b in objects if t == pdfium_c.FPDF_PAGEOBJ_TEXT]
    if kind == "figure":
        above = [b for b in graphics if b[1] >= cap[3] - 4 and _overlaps_x(b, cap)]
        box = _grow(cap[3], above, up=True, first_gap=40, gap=30)
        if box is None:  # the caption comes first
            below = [b for b in graphics if b[3] <= cap[1] + 4 and _overlaps_x(b, cap)]
            box = _grow(cap[1], below, up=False, first_gap=40, gap=30)
        if box is None:
            return None
        # The labels of the axes and panels, but not the caption.
        near = (box[0] - 8, box[1] - 8, box[2] + 8, box[3] + 8)
        for b in texts:
            if b[0] < near[2] and near[0] < b[2] and b[1] < near[3] and near[1] < b[3] and not (b[1] < cap[3] and cap[1] < b[3]):
                box = _union(box, b)
    else:
        # Horizontal rules under the caption: the table runs to the last rule of the group, then on
        # to the next rule as wide as the table (its bottom rule, over rows without rules), but not
        # across an empty gap or the next caption.
        rules = _join([b for b in graphics if (b[3] - b[1]) < 3 and b[3] <= cap[1] + 4])
        rules = [b for b in rules if (b[2] - b[0]) > 30 and _overlaps_x(b, cap)]
        lines = _grow(cap[1], rules, up=False, first_gap=40, gap=160)
        if lines is None or sum(1 for b in rules if lines[1] <= b[1] and b[3] <= lines[3]) < 2:
            return None
        stop = max((c for c in _caption_tops(page) if c < cap[1] - 4), default=0.0)
        content = [b for b in texts + rules if _overlaps_x(b, lines, 0)]
        bottom = lines[1]
        for rule in sorted((b for b in rules if b[3] < bottom and abs(b[0] - lines[0]) < 8 and abs(b[2] - lines[2]) < 8), key=lambda b: -b[3]):
            if rule[1] >= stop and _empty_gap(content, rule[3], bottom) <= 40:
                bottom = rule[1]
            break
        first = lines
        box = (first[0], bottom, first[2], cap[1])
        for b in texts:
            if box[1] - 2 <= b[1] and b[3] <= cap[1] and _overlaps_x(b, box, 0):
                box = _union(box, b)
    left, bottom, right, top = max(0.0, box[0] - PAD), max(0.0, box[1] - PAD), min(width, box[2] + PAD), min(height, box[3] + PAD)
    # The padding stops short of the caption, on the side that faces it.
    if box[1] >= cap[3] - 4:
        bottom = max(bottom, cap[3] + 1)
    elif box[3] <= cap[1] + 4:
        top = min(top, cap[1] - 1)
    return left, bottom, right, top


def extract(pdf_path: Path, label: str, out_path: Path, scale: float = 2.0) -> dict:
    """Find the caption, cut out the figure or table (or take the page), and write a PNG."""
    kind, number = parse_label(label)
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        found = find_caption(pdf, kind, number)
        if not found:
            raise ValueError(f'No caption "{label}" in the PDF. Use the label as the caption prints it.')
        index, cap, caption = found
        page = pdf[index]
        width, height = page.get_size()
        box = region(page, kind, cap)
        method = "crop" if box else "page"
        box = box or (0.0, 0.0, width, height)
        bitmap = page.render(scale=scale, crop=(box[0], box[1], width - box[2], height - box[3]), rev_byteorder=True)
        data = png(bitmap.width, bitmap.height, bitmap.stride, bytes(bitmap.buffer), bitmap.n_channels)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        return {"page": index + 1, "caption": caption[:300], "method": method,
                "box": [round(v) for v in box], "width": bitmap.width, "height": bitmap.height}
    finally:
        pdf.close()
