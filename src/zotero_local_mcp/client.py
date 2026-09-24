"""Thin async client for the Zotero 10 local API (http://127.0.0.1:23119/api/).

Reads need no key. Writes need a local API key that Zotero grants through a
dialog (POST /api/local/authorize). Every write carries the Zotero-Server-ID
header, and every item update carries the item version, so an edit made in
the Zotero window between our read and our write is never overwritten.

This client never sends DELETE requests. Removing things is done by editing
items (tags, collections) or by moving items to the trash (deleted = true).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx

from . import APP_NAME, __version__

BATCH = 50  # Zotero accepts at most 50 objects per multi-object write


class ZoteroError(RuntimeError):
    """Any failure that the agent should see as a plain message."""


class WriteResult:
    def __init__(self) -> None:
        self.succeeded: list[str] = []
        self.unchanged: list[str] = []
        self.failed: dict[str, tuple[int, str]] = {}
        self.created: list[str] = []
        self.error: str | None = None  # set when a batch failed and the rest was not sent
        self.not_sent: list[str] = []


class LocalZotero:
    def __init__(
        self,
        api_url: str = "http://127.0.0.1:23119/api",
        state_dir: Path | None = None,
        auth_timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base = api_url.rstrip("/")
        self.prefix = f"{self.base}/users/0"
        self.state_dir = state_dir or Path("~/.local/share/zotero-local-mcp").expanduser()
        self.auth_timeout = auth_timeout
        self.server_id: str | None = None
        self.api_version: str | None = None
        self._key: str | None = None
        self._key_remembered = False
        self._key_sid: str | None = None  # database the in-memory key belongs to
        self.last_auth_note: str | None = None
        self._http = httpx.AsyncClient(
            timeout=60.0,
            transport=transport,
            headers={
                # Must not start with "Mozilla/": Zotero drops browser-like requests.
                "User-Agent": f"{APP_NAME}/{__version__}",
                "Zotero-API-Version": "3",
            },
        )

    # ------------------------------------------------------------------ setup

    async def aclose(self) -> None:
        await self._http.aclose()

    async def connect(self) -> None:
        try:
            r = await self._http.get(f"{self.base}/")
        except httpx.TransportError as exc:
            raise ZoteroError(
                "Cannot reach Zotero at "
                f"{self.base}. Start Zotero and enable Settings > Advanced > "
                "'Allow other applications on this computer to communicate with Zotero'."
            ) from exc
        if r.status_code == 403:
            raise ZoteroError(
                "Zotero's local API is disabled. Enable Settings > Advanced > "
                "'Allow other applications on this computer to communicate with Zotero'."
            )
        sid = r.headers.get("Zotero-Server-ID")
        if not sid:
            raise ZoteroError(
                "Zotero did not send a Zotero-Server-ID header. This server needs Zotero 10 or later."
            )
        self.server_id = sid
        self.api_version = r.headers.get("Zotero-API-Version")
        if sid != self._key_sid:
            # Different database (or first connect): never reuse a key from another one.
            self._key = self._load_key()
            self._key_remembered = self._key is not None
            self._key_sid = sid

    async def _ensure(self) -> None:
        if self.server_id is None:
            await self.connect()

    # ------------------------------------------------------------------ keys

    @property
    def _key_file(self) -> Path:
        return self.state_dir / "keys.json"

    def _load_key(self) -> str | None:
        try:
            keys = json.loads(self._key_file.read_text())
        except (OSError, ValueError):
            return None
        return keys.get(self.server_id or "")

    def _save_key(self, key: str | None) -> None:
        try:
            keys = json.loads(self._key_file.read_text())
        except (OSError, ValueError):
            keys = {}
        if key is None:
            keys.pop(self.server_id or "", None)
        else:
            keys[self.server_id or ""] = key
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._key_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(keys, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self._key_file)

    @property
    def has_remembered_key(self) -> bool:
        return self._key is not None and self._key_remembered

    async def authorize(self) -> str:
        """Ask Zotero for a write key. Zotero shows a dialog; this call waits for it."""
        await self._ensure()
        try:
            r = await self._http.post(
                f"{self.base}/local/authorize",
                json={"appName": APP_NAME},
                headers={"Zotero-Server-ID": self.server_id or ""},
                timeout=self.auth_timeout,
            )
        except httpx.TimeoutException as exc:
            raise ZoteroError(
                "Nobody answered the Zotero authorization dialog in time."
            ) from exc
        if r.status_code == 403:
            raise ZoteroError("Write access was denied in the Zotero dialog.")
        if r.status_code == 429:
            wait = r.headers.get("Retry-After", "60")
            raise ZoteroError(
                f"Zotero limits authorization dialogs to five per minute. Retry after {wait} s, "
                "and choose 'Always Allow' to stop the dialogs."
            )
        if r.status_code == 412:
            self.server_id = None
            raise ZoteroError("Zotero's database changed (different Server ID). Retry the call.")
        if r.status_code != 200:
            raise ZoteroError(f"Authorization failed: HTTP {r.status_code} {r.text[:200]}")
        body = r.json()
        key, remember = body["key"], bool(body.get("remember"))
        self._key, self._key_remembered, self._key_sid = key, remember, self.server_id
        if remember:
            self._save_key(key)
            self.last_auth_note = None
        else:
            self.last_auth_note = (
                "Zotero granted a single-use key. Each write batch will show the dialog again. "
                "Choose 'Always Allow' next time to avoid this."
            )
        return key

    # ------------------------------------------------------------------ http

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        if path.startswith("/"):
            return f"{self.base}{path}"
        return f"{self.prefix}/{path}"

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        await self._ensure()
        for attempt in range(2):
            try:
                r = await self._http.get(
                    self._url(path),
                    params={"format": "json", **(params or {})},
                    headers={"Zotero-Server-ID": self.server_id or ""},
                )
            except httpx.HTTPError as exc:
                raise ZoteroError(f"Lost the connection to Zotero while reading {path}: {exc!r}") from exc
            if r.status_code == 412 and attempt == 0:
                # Different Zotero database than the one we cached.
                self.server_id = None
                await self.connect()
                continue
            break
        if r.status_code == 404:
            raise ZoteroError(f"Not found: {path}")
        if r.status_code >= 400:
            raise ZoteroError(f"GET {path} failed: HTTP {r.status_code} {r.text[:200]}")
        return r

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return (await self._get(path, params)).json()

    async def _write(
        self,
        method: str,
        path: str,
        body: Any,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        await self._ensure()
        for attempt in range(2):
            key = self._key or await self.authorize()
            headers = {
                "Zotero-Server-ID": self.server_id or "",
                "Zotero-API-Key": key,
                "Content-Type": "application/json",
                **(extra_headers or {}),
            }
            try:
                r = await self._http.request(
                    method, self._url(path), content=json.dumps(body), headers=headers
                )
            except httpx.HTTPError as exc:
                raise ZoteroError(
                    f"Lost the connection to Zotero during a write: {exc!r}. "
                    "This batch may or may not have been saved; check the items."
                ) from exc
            if not self._key_remembered:
                self._key = None  # single-use key is consumed by the first valid write
            if r.status_code == 401:
                # Key revoked (Settings > Advanced > Clear Write Authorizations) or unknown.
                self._key, self._key_remembered = None, False
                self._save_key(None)
                if attempt == 0:
                    continue
                break
            if r.status_code == 428:
                raise ZoteroError("Zotero rejected the write: missing precondition header.")
            if r.status_code == 409:
                raise ZoteroError("The Zotero library is locked (sync in progress?). Retry shortly.")
            return r
        raise ZoteroError("Zotero rejected the write key twice.")

    # ------------------------------------------------------------------ reads

    async def top_items(self, collection: str | None = None, query: str | None = None,
                        fulltext: bool = False) -> list[dict]:
        path = f"collections/{collection}/items/top" if collection else "items/top"
        params: dict[str, Any] = {}
        if query:
            params["q"] = query
            params["qmode"] = "everything" if fulltext else "titleCreatorYear"
        return await self.get_json(path, params)

    async def items_by_keys(self, keys: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(keys), BATCH):
            chunk = keys[i : i + BATCH]
            out.extend(await self.get_json("items", {"itemKey": ",".join(chunk), "includeTrashed": 1}))
        return out

    async def item(self, key: str) -> dict:
        return await self.get_json(f"items/{key}")

    async def children(self, key: str) -> list[dict]:
        return await self.get_json(f"items/{key}/children")

    async def tags(self) -> list[dict]:
        return await self.get_json("tags")

    async def collections(self) -> list[dict]:
        return await self.get_json("collections")

    # ------------------------------------------------------------------ writes

    async def update_items(self, objects: list[dict]) -> WriteResult:
        """Partial updates. Each object needs 'key' and 'version' plus the changed fields."""
        result = WriteResult()
        for i in range(0, len(objects), BATCH):
            chunk = objects[i : i + BATCH]
            try:
                r = await self._write("POST", "items", chunk)
                self._collect(r, chunk, result)
            except ZoteroError as exc:
                # Keep what earlier batches saved; report the rest as not sent.
                result.error = str(exc)
                result.not_sent = [o.get("key", "?") for o in objects[i:]]
                break
        return result

    async def create_items(self, objects: list[dict]) -> WriteResult:
        result = WriteResult()
        for i in range(0, len(objects), BATCH):
            chunk = objects[i : i + BATCH]
            r = await self._write(
                "POST", "items", chunk, {"Zotero-Write-Token": uuid.uuid4().hex}
            )
            self._collect(r, chunk, result, creating=True)
        return result

    async def create_collection(self, name: str, parent: str | None) -> str:
        body = [{"name": name, "parentCollection": parent or False}]
        r = await self._write("POST", "collections", body, {"Zotero-Write-Token": uuid.uuid4().hex})
        if r.status_code != 200:
            raise ZoteroError(f"Creating the collection failed: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        failed = data.get("failed") or {}
        if failed:
            raise ZoteroError(f"Creating the collection failed: {json.dumps(failed)[:300]}")
        ok = data.get("success") or {}
        if ok:
            return next(iter(ok.values()))
        return next(iter((data.get("successful") or {}).values()))["key"]

    @staticmethod
    def _collect(r: httpx.Response, chunk: list[dict], result: WriteResult, creating: bool = False) -> None:
        if r.status_code == 413:
            raise ZoteroError("Zotero refused the batch as too large.")
        if r.status_code == 412:
            # Per-object versions are used, so a whole-request 412 means another database.
            raise ZoteroError("Zotero's database changed (different Server ID). Stopped.")
        if r.status_code != 200:
            raise ZoteroError(f"Write failed: HTTP {r.status_code} {r.text[:300]}")
        data = r.json()
        success = data.get("success") or {}
        successful = data.get("successful") or {}
        for idx in set(success) | set(successful):
            key = success.get(idx) or successful[idx].get("key")
            (result.created if creating else result.succeeded).append(key)
        for idx, key in (data.get("unchanged") or {}).items():
            result.unchanged.append(key if isinstance(key, str) else chunk[int(idx)].get("key", "?"))
        for idx, err in (data.get("failed") or {}).items():
            key = err.get("key") or chunk[int(idx)].get("key", f"new#{idx}")
            result.failed[key] = (int(err.get("code", 0)), str(err.get("message", "")))
