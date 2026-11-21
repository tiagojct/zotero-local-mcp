"""Reading tools: PDF annotations, the Zotero window selection, and Markdown export.

Nothing here changes the library. export_markdown writes files outside the vault only, and
overwrites only files that an earlier export wrote (frontmatter line `zotero-export: true`).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .citekey import current_key, year_of
from .client import ZoteroError, current_library, in_library, link_scope

if TYPE_CHECKING:
    from .library import Library

EXPORT_MARKER = "zotero-export: true"
EXPORT_MAX_ITEMS = 200
KEY = re.compile(r"^[A-Z0-9]{8}$")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _position(data: dict) -> dict:
    try:
        pos = json.loads(data.get("annotationPosition") or "{}")
    except ValueError:
        return {}
    return pos if isinstance(pos, dict) else {}


def annotation_row(a: dict) -> dict:
    """One annotation as the tools show it."""
    d = a["data"]
    pos = _position(d)
    page_index = pos.get("pageIndex")
    att = d.get("parentItem", "")
    row: dict[str, Any] = {
        "key": d["key"],
        "attachment": att,
        "type": d.get("annotationType", ""),
        "text": d.get("annotationText", ""),
        "comment": d.get("annotationComment", ""),
        "color": d.get("annotationColor", ""),
        "page_label": d.get("annotationPageLabel", ""),
        "page": page_index + 1 if isinstance(page_index, int) else None,
        "tags": [t["tag"] for t in d.get("tags") or []],
        "link": f"zotero://open-pdf/{link_scope()}/items/{att}?annotation={d['key']}",
    }
    return {k: v for k, v in row.items() if v not in ("", [], None) or k in ("key", "attachment", "type")}


def vault_root(vault: Path | None) -> Path | None:
    """The Obsidian vault that holds ZOTERO_VAULT: the nearest folder, from ZOTERO_VAULT up, with
    .obsidian/ in it. ZOTERO_VAULT is often the Sub-Sub folder inside the vault, not its root."""
    if not vault:
        return None
    v = vault.expanduser().resolve()
    for folder in (v, *v.parents):
        if (folder / ".obsidian").is_dir():
            return folder
    return v


def _sort_key(a: dict) -> tuple:
    d = a["data"]
    return (d.get("annotationSortIndex") or "", d.get("dateAdded") or "")


class Reading:
    def __init__(self, lib: Library) -> None:
        self.lib = lib
        self.z = lib.z

    # ------------------------------------------------------------ annotations

    async def _attachments(self, key: str) -> tuple[dict, list[dict]]:
        """The item and its file attachments. An attachment key gives the attachment itself."""
        item = await self.z.item(key)
        kind = item["data"].get("itemType")
        if kind == "attachment":
            return item, [item]
        if kind in ("note", "annotation"):
            raise ZoteroError(f"{key} is a {kind}. Give the key of an item or of its PDF.")
        atts = [c for c in await self.z.children(key) if c["data"].get("itemType") == "attachment"]
        return item, atts

    async def _annotations_by_attachment(self, attachment_keys: set[str]) -> dict[str, list[dict]]:
        # Zotero 10's local API does not list annotations as children of a file: read them all
        # and keep those of these files.
        out: dict[str, list[dict]] = {}
        for a in await self.z.annotations():
            parent = a["data"].get("parentItem")
            if parent in attachment_keys and not a["data"].get("deleted"):
                out.setdefault(parent, []).append(a)
        for rows in out.values():
            rows.sort(key=_sort_key)
        return out

    async def get_annotations(self, key: str, kinds: list[str] | None = None) -> dict:
        item, atts = await self._attachments(key)
        if not atts:
            raise ZoteroError("This item has no attachment, so it has no annotations.")
        by_att = await self._annotations_by_attachment({a["key"] for a in atts})
        wanted = {k.lower() for k in kinds or []}
        files = []
        total = 0
        for a in atts:
            rows = [annotation_row(x) for x in by_att.get(a["key"], [])]
            if wanted:
                rows = [r for r in rows if r["type"].lower() in wanted]
            total += len(rows)
            files.append({"attachment": a["key"], "title": a["data"].get("title", ""),
                          "contentType": a["data"].get("contentType", ""), "annotations": rows})
        d = item["data"]
        out = {
            "key": d["key"],
            "title": d.get("title", ""),
            "citekey": current_key(d) if d.get("itemType") != "attachment" else None,
            "count": total,
            "files": files,
        }
        if not total:
            out["note"] = ("No annotations. Highlights made outside Zotero's reader are inside the PDF file: "
                           "in Zotero, use Import Annotations on the PDF first.")
        return out

    # ------------------------------------------------------------ selection

    async def get_selected_items(self, detail: bool = False) -> dict:
        sel = await self.z.gam_selection()
        if detail:
            for row in sel.get("items") or []:
                if row.get("itemType") in ("note", "attachment", "annotation"):
                    continue
                library = None if row.get("library") in (None, "user") else row["library"]
                with in_library(library):
                    try:
                        item = await self.z.item(row["key"])
                    except ZoteroError:
                        continue
                    row.update(self.lib.summarize(item, detail=True))
        sel["next"] = ("Use the keys with get_item, get_fulltext or get_annotations. "
                       "For a paper open in the reader, 'reader' is the file and items[0] its parent.")
        return sel

    async def show_in_zotero(self, key: str) -> dict:
        if not KEY.match(key or ""):
            raise ValueError(f'"{key}" is not a Zotero item key (8 letters and digits).')
        await self.z.item(key)  # refuse a key that is not in this library
        url = f"zotero://select/{link_scope()}/items/{key}"
        if sys.platform != "darwin":
            return {"key": key, "link": url, "shown": False, "note": "Open the link to show the item."}
        proc = await asyncio.create_subprocess_exec("open", url)
        code = await proc.wait()
        return {"key": key, "link": url, "shown": code == 0}

    # ------------------------------------------------------------ export

    def _export_folder(self, folder: str | None) -> Path:
        path = Path(folder).expanduser() if folder else self.lib.s.state_dir / "export"
        path = path.resolve()
        root = vault_root(self.lib.s.vault)
        if root and (path == root or root in path.parents):
            raise ValueError(
                f"{path} is inside the Obsidian vault {root}. Export to a folder outside the vault; "
                "literature notes in the vault are made by gam or Sub-Sub.")
        return path

    async def _select(self, keys: list[str] | None, collection_key: str | None,
                      query: str | None) -> list[dict]:
        if keys:
            items = await self.z.items_by_keys(keys)
            found = {i["key"] for i in items}
            missing = [k for k in keys if k not in found]
            if missing:
                raise ZoteroError(f"Not in the library: {', '.join(missing)}")
            return items
        if collection_key or query:
            return await self.lib.regular_items(collection_key, query)
        raise ValueError("Give keys, collection_key or query.")

    async def _fulltext(self, atts: list[dict]) -> str:
        for a in sorted(atts, key=lambda a: a["data"].get("contentType") != "application/pdf"):
            try:
                ft = await self.z.get_json(f"items/{a['key']}/fulltext")
            except ZoteroError as exc:
                if str(exc).startswith("Not found"):
                    continue
                raise
            if ft.get("content"):
                return ft["content"]
        return ""

    async def export_markdown(self, keys: list[str] | None = None, collection_key: str | None = None,
                              query: str | None = None, folder: str | None = None,
                              fulltext: bool = True, limit: int = 50) -> dict:
        from .library import html_to_text, regular, short_creators

        out_dir = self._export_folder(folder)
        items = [i for i in await self._select(keys, collection_key, query)
                 if regular(i) and not i["data"].get("deleted")]
        limit = max(1, min(limit, EXPORT_MAX_ITEMS))
        more = len(items) - limit
        items = items[:limit]
        children = {i["key"]: await self.z.children(i["key"]) for i in items}
        att_keys = {c["key"] for cs in children.values() for c in cs
                    if c["data"].get("itemType") == "attachment"}
        anns = await self._annotations_by_attachment(att_keys) if att_keys else {}
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        written, skipped, used = [], [], set()
        for item in items:
            d = item["data"]
            name = SAFE_NAME.sub("-", current_key(d) or d["key"]).strip("-") or d["key"]
            if name.lower() in used:
                name = f"{name}-{d['key']}"
            used.add(name.lower())
            path = out_dir / f"{name}.md"
            if path.exists() and not path.is_file():
                skipped.append({"key": d["key"], "path": str(path), "reason": "a folder has this name"})
                continue
            if path.exists():
                head = path.read_text(encoding="utf-8", errors="replace")[:200]
                if EXPORT_MARKER not in head.split("\n---", 1)[0]:
                    skipped.append({"key": d["key"], "path": str(path),
                                    "reason": "a file that this export did not write is there"})
                    continue
            cs = children[d["key"]]
            atts = [c for c in cs if c["data"].get("itemType") == "attachment"]
            notes = [c for c in cs if c["data"].get("itemType") == "note"]
            text = await self._fulltext(atts) if fulltext and atts else ""
            body = render_item(d, notes, atts, anns, text, stamp, short_creators(d, 50), html_to_text)
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(body, encoding="utf-8")
            tmp.replace(path)
            written.append({"key": d["key"], "path": str(path), "chars": len(body),
                            "annotations": sum(len(anns.get(a["key"], [])) for a in atts),
                            "fulltext": bool(text)})
        result: dict[str, Any] = {"folder": str(out_dir), "written": written, "skipped": skipped,
                                  "next": "Read or grep the files; do not pull the full texts into the chat."}
        if more > 0:
            result["not_exported"] = more
            result["note"] = f"{more} more items match. Raise limit or narrow the selection."
        if current_library():
            result["library"] = current_library()
        return result


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_item(d: dict, notes: list[dict], atts: list[dict], anns: dict[str, list[dict]],
                text: str, stamp: str, authors: str, html_to_text) -> str:
    """The Markdown file of one item."""
    key = d["key"]
    venue = d.get("publicationTitle") or d.get("bookTitle") or d.get("publisher") or ""
    lines = [
        "---",
        EXPORT_MARKER,
        f"key: {key}",
        f"citekey: {_yaml(current_key(d) or '')}",
        f"title: {_yaml(d.get('title', ''))}",
        f"authors: {_yaml(authors)}",
        f"year: {_yaml(year_of(d))}",
        f"type: {d.get('itemType', '')}",
        f"venue: {_yaml(venue)}",
        f"doi: {_yaml(d.get('DOI', ''))}",
        f"zotero: zotero://select/{link_scope()}/items/{key}",
        f"exported: {stamp}",
        "---",
        "",
        f"# {d.get('title', '') or key}",
        "",
    ]
    cite = ", ".join(x for x in (authors, year_of(d), venue) if x)
    if cite:
        lines += [cite, ""]
    if d.get("abstractNote"):
        lines += ["## Abstract", "", d["abstractNote"].strip(), ""]
    tags = [t["tag"] for t in d.get("tags") or [] if t.get("type", 0) != 1]
    if tags:
        lines += ["## Tags", "", ", ".join(tags), ""]
    if notes:
        lines += ["## Notes", ""]
        for n in notes:
            lines += [f"### Note {n['key']}", "", html_to_text(n["data"].get("note", "")), ""]
    ann_rows = [(a, x) for a in atts for x in anns.get(a["key"], [])]
    if ann_rows:
        lines += ["## Annotations", ""]
        for a, x in ann_rows:
            r = annotation_row(x)
            where = f"p. {r['page_label']}" if r.get("page_label") else (f"p. {r['page']}" if r.get("page") else "")
            head = f"- {r['type']}" + (f", {where}" if where else "") + f" ([open]({r['link']}))"
            lines.append(head)
            if r.get("text"):
                lines.append("  > " + r["text"].replace("\n", " ").strip())
            if r.get("comment"):
                lines.append("  Comment: " + r["comment"].replace("\n", " ").strip())
        lines.append("")
    if text:
        lines += ["## Full text", "", "Text that Zotero indexed from the file. Page breaks are lost.", "",
                  tidy_text(text), ""]
    return "\n".join(lines)


def tidy_text(text: str) -> str:
    """Indexed text without the indentation and the runs of empty lines that EPUB and HTML leave."""
    text = "\n".join(line.strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip()
