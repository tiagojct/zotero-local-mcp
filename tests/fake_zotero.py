"""In-memory imitation of the Zotero 10 local API, detailed enough for the tests.

Implements: GET /api/ (Server ID), POST /api/local/authorize (keys, single-use or
remembered, deny), reads of items/top, items?itemKey=, items/<key>, children,
collections, and multi-object POST writes with per-object version checks.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx

ALPHABET = "23456789ABCDEFGHIJKLMNPQRSTUVWXYZ"

_COMMON = {"title": "", "creators": [{"creatorType": "author", "firstName": "", "lastName": ""}],
           "abstractNote": "", "date": "", "language": "", "shortTitle": "", "url": "",
           "accessDate": "", "extra": "", "tags": [], "collections": [], "relations": {}}
TEMPLATES = {
    "journalArticle": {"itemType": "journalArticle", **_COMMON, "publicationTitle": "", "volume": "",
                       "issue": "", "pages": "", "series": "", "journalAbbreviation": "", "DOI": "",
                       "ISSN": ""},
    "book": {"itemType": "book", **_COMMON, "series": "", "edition": "", "place": "", "publisher": "",
             "numPages": "", "ISBN": ""},
    "bookSection": {"itemType": "bookSection", **_COMMON, "bookTitle": "", "place": "", "publisher": "",
                    "pages": "", "ISBN": ""},
    "preprint": {"itemType": "preprint", **_COMMON, "repository": "", "archiveID": "", "DOI": ""},
    "document": {"itemType": "document", **_COMMON, "publisher": ""},
    "attachment": {"itemType": "attachment", "linkMode": "imported_url", "title": "", "accessDate": "",
                   "url": "", "note": "", "contentType": "", "charset": "", "filename": "", "md5": None,
                   "mtime": None, "tags": [], "relations": {}},
}


def new_key() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(8))


class FakeZotero:
    def __init__(self, server_id: str = "srvTEST0001") -> None:
        self.server_id = server_id
        self.lib_version = 1
        self.items: dict[str, dict] = {}
        self.collections: dict[str, dict] = {}
        self.remember = True
        self.deny = False
        self.keys: dict[str, bool] = {}  # key -> remembered
        self.auth_prompts = 0
        self.write_requests = 0
        self.seen_headers: list[dict] = []
        self.fulltext: dict[str, dict] = {}
        self.local_templates = False  # like real Zotero 10: no /api/items/new
        self.web_template_requests = 0
        self.uploads: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}

    # ------------------------------------------------------------ fixtures

    def add_item(self, **data) -> str:
        key = data.pop("key", None) or new_key()
        itype = data.get("itemType", "journalArticle")
        base = json.loads(json.dumps(TEMPLATES.get(itype, {})))  # real items carry every field of their type
        base.update({"key": key, "version": self.lib_version, "itemType": itype, "title": "",
                     "creators": [], "date": "", "extra": "", "abstractNote": "", "tags": [],
                     "collections": [], "relations": {}, "dateAdded": "2020-01-01T00:00:00Z"})
        if itype in ("note", "attachment"):
            base.pop("abstractNote", None)
        base.update(data)
        self.items[key] = base
        return key

    def add_collection(self, name: str, parent: str | None = None) -> str:
        key = new_key()
        self.collections[key] = {"key": key, "version": self.lib_version, "name": name,
                                 "parentCollection": parent or False}
        return key

    def touch(self, key: str, **fields) -> None:
        """Simulate an edit in the Zotero window."""
        self.lib_version += 1
        self.items[key].update(fields, version=self.lib_version)

    # ------------------------------------------------------------ plumbing

    def _wrap(self, data: dict) -> dict:
        return {"key": data["key"], "version": data["version"], "library": {}, "links": {},
                "meta": {}, "data": json.loads(json.dumps(data))}

    def handle(self, method: str, url: str, headers: dict[str, str], body: bytes):
        headers = {k.lower(): v for k, v in headers.items()}
        self.seen_headers.append(headers)
        parts = urlsplit(url)
        path = parts.path
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        base_h = {"Zotero-Server-ID": self.server_id, "Zotero-API-Version": "3",
                  "Last-Modified-Version": str(self.lib_version)}

        def resp(status: int, payload=None, extra=None):
            h = {**base_h, **(extra or {})}
            data = b"" if payload is None else json.dumps(payload).encode()
            if payload is not None:
                h["Content-Type"] = "application/json"
            return status, h, data

        if headers.get("user-agent", "").startswith("Mozilla/"):
            return resp(400, {"error": "browser"})
        sid = headers.get("zotero-server-id")
        if sid and sid != self.server_id:
            return resp(412, {"error": "server id mismatch"})
        if path in ("/api", "/api/"):
            return resp(200, {})
        if parts.netloc == "api.zotero.org":
            # The public web API serves item templates; Zotero 10's local API does not.
            self.web_template_requests += 1
            if path != "/items/new":
                return resp(404, {"error": "Not found"})
            t = q.get("itemType")
            if t not in TEMPLATES:
                return resp(400, {"error": "Invalid item type"})
            return resp(200, json.loads(json.dumps(TEMPLATES[t])))
        if path == "/api/items/new":
            if self.local_templates:
                return resp(200, json.loads(json.dumps(TEMPLATES[q.get("itemType")])))
            return resp(404, {"error": "Not found"})
        if path.startswith("/api/local/uploads/") and method == "POST":
            up = path.rsplit("/", 1)[-1]
            if up not in self.uploads:
                return resp(400, {"error": "bad upload key"})
            self.uploads[up]["bytes"] = body
            return resp(201, None)
        if path == "/api/local/authorize" and method == "POST":
            if not sid:
                return resp(428, {"error": "Zotero-Server-ID required"})
            self.auth_prompts += 1
            if self.deny:
                return resp(403, {"denied": True})
            key = secrets.token_hex(16)
            self.keys[key] = self.remember
            return resp(200, {"key": key, "remember": self.remember})
        prefix = "/api/users/0/"
        if not path.startswith(prefix):
            return resp(404, {"error": "not found"})
        rest = path[len(prefix):]

        if method == "GET":
            live = [i for i in self.items.values() if q.get("includeTrashed") == "1" or not i.get("deleted")]
            if rest == "items/top":
                items = [i for i in live if not i.get("parentItem")]
                if "q" in q:
                    items = [i for i in items if q["q"].lower() in i.get("title", "").lower()]
                return resp(200, [self._wrap(i) for i in items])
            if rest == "items":
                keys = q.get("itemKey", "").split(",") if "itemKey" in q else None
                items = [i for i in live if keys is None or i["key"] in keys]
                return resp(200, [self._wrap(i) for i in items])
            if rest.startswith("items/") and rest.endswith("/fulltext"):
                key = rest.split("/")[1]
                if key not in self.fulltext:
                    return resp(404, {"error": "no full text"})
                return resp(200, self.fulltext[key])
            if rest.startswith("items/") and rest.endswith("/children"):
                parent = rest.split("/")[1]
                return resp(200, [self._wrap(i) for i in live if i.get("parentItem") == parent])
            if rest.startswith("items/"):
                key = rest.split("/")[1]
                if key not in self.items:
                    return resp(404, {"error": "not found"})
                return resp(200, self._wrap(self.items[key]))
            if rest.startswith("collections/") and rest.endswith("/items/top"):
                ck = rest.split("/")[1]
                items = [i for i in live if ck in i.get("collections", []) and not i.get("parentItem")]
                return resp(200, [self._wrap(i) for i in items])
            if rest.startswith("collections/") and rest.count("/") == 1:
                c = self.collections.get(rest.split("/")[1])
                if c is None:
                    return resp(404, {"error": "not found"})
                return resp(200, {"key": c["key"], "version": c["version"], "data": dict(c)})
            if rest == "collections":
                return resp(200, [{"key": c["key"], "version": c["version"], "meta": {"numItems": 0},
                                   "data": dict(c)} for c in self.collections.values()])
            return resp(404, {"error": "not found"})

        if method in ("PUT", "PATCH", "DELETE"):
            return resp(405, {"error": "not used by this client"})

        # POST writes
        if not sid:
            return resp(428, {"error": "Zotero-Server-ID required"})
        if rest.startswith("items/") and rest.endswith("/file"):
            return self._file(rest.split("/")[1], headers, body, resp)
        if not sid:
            return resp(428, {"error": "Zotero-Server-ID required"})
        key = headers.get("zotero-api-key")
        if key not in self.keys:
            return resp(401, {"error": "bad key"}, {"WWW-Authenticate": 'Zotero-API-Key realm="Zotero Local API"'})
        if not self.keys[key]:
            del self.keys[key]  # single-use key consumed
        self.write_requests += 1
        objs = json.loads(body)
        if len(objs) > 50:
            return resp(413, {"error": "too many"})
        success, successful, unchanged, failed = {}, {}, {}, {}
        changed_any = False
        new_version = self.lib_version + 1
        store = self.items if rest == "items" else self.collections if rest == "collections" else None
        if store is None:
            return resp(404, {"error": "not found"})
        for idx, obj in enumerate(objs):
            idx = str(idx)
            okey = obj.get("key")
            if okey and okey in store:
                cur = store[okey]
                if int(obj.get("version", -1)) != cur["version"]:
                    failed[idx] = {"key": okey, "code": 412, "message": "Item has been modified"}
                    continue
                patch = {k: v for k, v in obj.items() if k not in ("key", "version")}
                if rest == "items":
                    bad = [k for k in patch if k not in cur and k not in ("deleted", "citationKey")]
                    if bad:
                        failed[idx] = {"key": okey, "code": 400, "message": f"invalid field {bad}"}
                        continue
                if all(cur.get(k) == v for k, v in patch.items()):
                    unchanged[idx] = okey
                    continue
                cur.update(patch)
                cur["version"] = new_version
                changed_any = True
                success[idx] = okey
                successful[idx] = self._wrap(cur) if rest == "items" else {"key": okey, "data": cur}
            else:
                okey = okey or new_key()
                data = dict(obj, key=okey, version=new_version)
                if rest == "items":
                    data.setdefault("tags", [])
                    data.setdefault("dateAdded", "2026-09-24T00:00:00Z")
                store[okey] = data
                changed_any = True
                success[idx] = okey
                successful[idx] = {"key": okey, "data": data}
        if changed_any:
            self.lib_version = new_version
        return resp(200, {"success": success, "successful": successful,
                          "unchanged": unchanged, "failed": failed})

    def _file(self, key, headers, body, resp):
        import hashlib
        from urllib.parse import parse_qs as pq
        api_key = headers.get("zotero-api-key")
        if api_key not in self.keys:
            return resp(401, {"error": "bad key"})
        if not self.keys[api_key]:
            del self.keys[api_key]
        item = self.items.get(key)
        if not item or item.get("itemType") != "attachment" or item.get("linkMode") not in ("imported_file", "imported_url"):
            return resp(400, {"error": "not a stored-file attachment"})
        form = {k: v[0] for k, v in pq(body.decode()).items()}
        if "upload" in form:
            up = self.uploads.pop(form["upload"], None)
            if not up or "bytes" not in up:
                return resp(400, {"error": "upload missing"})
            if hashlib.md5(up["bytes"]).hexdigest() != up["md5"]:
                return resp(400, {"error": "md5 mismatch"})
            self.files[key] = up["bytes"]
            self.lib_version += 1
            item.update(md5=up["md5"], filename=up["filename"], version=self.lib_version)
            return resp(204, None)
        upload_key = secrets.token_hex(8)
        self.uploads[upload_key] = {"md5": form["md5"], "filename": form["filename"]}
        return resp(200, {"url": f"/api/local/uploads/{upload_key}", "uploadKey": upload_key,
                          "contentType": "application/pdf", "prefix": "", "suffix": ""})

    # ------------------------------------------------------------ adapters

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            status, headers, data = self.handle(request.method, str(request.url),
                                                dict(request.headers), request.content)
            return httpx.Response(status, headers=headers, content=data)
        return httpx.MockTransport(handler)

    def serve(self) -> tuple[ThreadingHTTPServer, str]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                url = f"http://{self.headers.get('Host')}{self.path}"
                status, headers, data = fake.handle(self.command, url, dict(self.headers), body)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _do

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}/api"
