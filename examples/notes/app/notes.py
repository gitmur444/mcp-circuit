#!/usr/bin/env python3
"""Notes: the demo app under test. A tiny web app with a JSON API, stdlib only.

  python3 notes.py --port 10001 --data /tmp/profile   # notes live in <data>/notes.json

UI: http://127.0.0.1:<port>/   API: GET /api/notes[?tag=x][&page=N] → {"items", "page", "next"} (10 per page), POST /api/notes {"text", "tags"?},
PATCH /api/notes/<id> {"done"?, "text"?, "tags"?}, DELETE /api/notes/<id>, GET /health.
"""
import argparse, html, json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.Lock()
PAGE_SIZE = 10


class Store:
    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.path = os.path.join(folder, "notes.json")

    def load(self):
        try:
            return json.load(open(self.path))
        except (OSError, ValueError):
            return {"next_id": 1, "notes": []}

    def save(self, db):
        tmp = self.path + ".tmp"
        json.dump(db, open(tmp, "w"), ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)


PAGE = """<!doctype html><meta charset="utf-8"><title>Notes · {port}</title>
<meta http-equiv="refresh" content="2">
<style>body{{font:15px system-ui;margin:24px;background:#fbfaf7;color:#222}}h1{{font-size:18px}}
li{{margin:6px 0}}.done{{text-decoration:line-through;color:#999}}.tag{{font-size:12px;background:#e8e4da;border-radius:4px;padding:1px 6px;margin-left:6px}}</style>
<h1>Notes <small style="color:#999">port {port} · {n} notes</small></h1><ul>{items}</ul>"""


def make_handler(store, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return None

        def note_id(self):
            try:
                return int(urlparse(self.path).path.rsplit("/", 1)[1])
            except ValueError:
                return None

        def do_GET(self):
            u = urlparse(self.path)
            with LOCK:
                db = store.load()
            if u.path == "/health":
                return self.reply(200, {"ok": True})
            if u.path == "/api/notes":
                q = parse_qs(u.query)
                tag, page = (q.get("tag") or [None])[0], int((q.get("page") or ["1"])[0])
                found = [x for x in db["notes"] if not tag or tag in x["tags"]]
                items = found[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
                nxt = f"/api/notes?page={page + 1}" + (f"&tag={tag}" if tag else "") if page * PAGE_SIZE < len(found) else None
                return self.reply(200, {"items": items, "page": page, "next": nxt})
            if u.path == "/":
                items = "".join(f'<li class="{"done" if x["done"] else ""}">#{x["id"]} {html.escape(x["text"])}'
                                + "".join(f'<span class="tag">{html.escape(t)}</span>' for t in x["tags"]) + "</li>"
                                for x in db["notes"])
                return self.reply(200, PAGE.format(port=port, n=len(db["notes"]), items=items), "text/html; charset=utf-8")
            self.reply(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/notes":
                return self.reply(404, {"error": "not found"})
            b = self.body()
            if not b or not isinstance(b.get("text"), str) or not b["text"].strip():
                return self.reply(400, {"error": "text is required"})
            with LOCK:
                db = store.load()
                note = {"id": db["next_id"], "text": b["text"].strip(), "tags": list(b.get("tags") or []), "done": False}
                db["next_id"] += 1
                db["notes"].append(note)
                store.save(db)
            self.reply(201, note)

        def do_PATCH(self):
            nid, b = self.note_id(), self.body()
            if nid is None or b is None:
                return self.reply(400, {"error": "bad request"})
            with LOCK:
                db = store.load()
                note = next((x for x in db["notes"] if x["id"] == nid), None)
                if not note:
                    return self.reply(404, {"error": f"no note {nid}"})
                note.update({k: v for k, v in b.items() if k in ("text", "tags", "done")})
                store.save(db)
            self.reply(200, note)

        def do_DELETE(self):
            nid = self.note_id()
            with LOCK:
                db = store.load()
                left = [x for x in db["notes"] if x["id"] != nid]
                if len(left) == len(db["notes"]):
                    return self.reply(404, {"error": f"no note {nid}"})
                db["notes"] = left
                store.save(db)
            self.reply(200, {"deleted": nid})
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", default=".")
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(Store(a.data), a.port)).serve_forever()


if __name__ == "__main__":
    main()
