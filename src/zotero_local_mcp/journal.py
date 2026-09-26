"""Append-only record of every applied write, so any change can be undone.

One JSON file per operation under <state_dir>/journal/<server_id>/. Each
change stores the item key and the before and after values of the fields
that changed. Undo writes the 'before' values back, but only for items whose
current values still equal 'after' (items edited since are skipped).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, state_dir: Path, server_id: str) -> None:
        self.dir = state_dir / "journal" / server_id
        self.dir.mkdir(parents=True, exist_ok=True)

    def record(self, op: str, summary: str, changes: list[dict[str, Any]],
               undoes: str | None = None) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        n = len(list(self.dir.glob(f"{stamp}-*.json")))
        entry_id = f"{stamp}-{n:02d}-{op}"
        entry = {
            "id": entry_id,
            "op": op,
            "summary": summary,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "undoes": undoes,
            "undone_by": [],
            "undone_keys": [],
            "changes": changes,
        }
        (self.dir / f"{entry_id}.json").write_text(json.dumps(entry, indent=1, ensure_ascii=False), encoding="utf-8")
        if undoes:
            orig = self.load(undoes)
            orig["undone_by"] = [*(orig.get("undone_by") or []), entry_id]
            orig["undone_keys"] = sorted({*(orig.get("undone_keys") or []),
                                          *(c["key"] for c in changes)})
            (self.dir / f"{undoes}.json").write_text(json.dumps(orig, indent=1, ensure_ascii=False), encoding="utf-8")
        return entry_id

    @staticmethod
    def fully_undone(entry: dict) -> bool:
        keys = {c["key"] for c in entry.get("changes") or []}
        return bool(keys) and keys <= set(entry.get("undone_keys") or [])

    def load(self, entry_id: str) -> dict:
        path = self.dir / f"{entry_id}.json"
        if not path.exists():
            raise LookupError(f"No journal entry {entry_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def entries(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return out

    def last_undoable(self) -> dict | None:
        for e in reversed(self.entries()):
            if not e.get("undoes") and not self.fully_undone(e):
                return e
        return None
