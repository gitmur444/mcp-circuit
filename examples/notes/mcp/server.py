#!/usr/bin/env python3
"""MCP server for the Notes demo app (stdio, stdlib only). App address: env NOTES_URL.

  MCP_STYLE=good  — focused tools with clear descriptions (default)
  MCP_STYLE=raw   — one generic "http" tool: the agent must guess the API itself
  MCP_STYLE=forms — "good" tools that ask the human via MCP elicitation: confirm a delete, pick a tag for an untagged note
"""
import json, os, sys, urllib.error, urllib.request

URL = os.environ.get("NOTES_URL", "http://127.0.0.1:8000").rstrip("/")
STYLE = os.environ.get("MCP_STYLE", "good")


def http(method, path, body=None):
    req = urllib.request.Request(URL + path, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode(errors="replace")}


def list_all(tag=None):
    """All pages of /api/notes."""
    out, path = [], "/api/notes" + (f"?tag={tag}" if tag else "")
    while path:
        r = http("GET", path)
        if "items" not in r:
            return r
        out += r["items"]
        path = r.get("next")
    return out


CAPS = {}   # client capabilities from initialize
ASK = [0]


def ask(message, schema):
    """MCP elicitation: the client shows the human a form; wait for the reply on stdin."""
    ASK[0] += 1
    rid = f"ask-{ASK[0]}"
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "elicitation/create",
                                 "params": {"message": message, "requestedSchema": schema}}) + "\n")
    sys.stdout.flush()
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == rid and "method" not in msg:
            return msg.get("result") or {"action": "cancel"}
    return {"action": "cancel"}


def asked(a, f):
    return "elicitation" in CAPS and STYLE == "forms"


def delete(a):
    if asked(a, "delete"):
        text = next((n["text"] for n in list_all() if n["id"] == a["id"]), f"#{a['id']}")
        r = ask(f"Delete note «{text}»?", obj({"confirm": {"type": "boolean", "title": "Delete"}}, ["confirm"]))
        if r.get("action") != "accept" or not (r.get("content") or {}).get("confirm"):
            return {"error": "declined", "body": "the human did not confirm the delete"}
    return http("DELETE", f"/api/notes/{a['id']}")


def add(a):
    tags = a.get("tags") or []
    if not tags and asked(a, "add"):
        r = ask(f"Which tag for the new note «{a['text']}»?", obj({"tag": {"type": "string", "title": "Tag",
                "enum": ["inbox", "work", "family"], "enumNames": ["Inbox", "Work", "Family"]}}, ["tag"]))
        if r.get("action") == "accept":
            tags = [(r.get("content") or {}).get("tag")]
    return http("POST", "/api/notes", {"text": a["text"], "tags": tags})


def obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


GOOD = {
    "list_notes": ("List all notes, optionally only those with a tag. Each note: id, text, tags, done.",
                   obj({"tag": {"type": "string"}}), lambda a: list_all(a.get("tag"))),
    "add_note": ("Create a note with text and optional tags.", obj({"text": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, ["text"]),
                 add),
    "update_note": ("Change a note by id: text, tags (replaces all), done (true = completed).",
                    obj({"id": {"type": "integer"}, "text": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}},
                         "done": {"type": "boolean"}}, ["id"]),
                    lambda a: http("PATCH", f"/api/notes/{a['id']}", {k: v for k, v in a.items() if k != "id"})),
    "delete_note": ("Delete a note by id.", obj({"id": {"type": "integer"}}, ["id"]), delete),
}
RAW = {
    "http": ("Send an HTTP request to the app.", obj({"method": {"type": "string"}, "path": {"type": "string"}, "body": {"type": "object"}}, ["method", "path"]),
             lambda a: http(a["method"].upper(), a["path"], a.get("body"))),
}
TOOLS = RAW if STYLE == "raw" else GOOD


def handle(msg):
    m, p = msg.get("method"), msg.get("params") or {}
    if m == "initialize":
        CAPS.update(p.get("capabilities") or {})
        return {"protocolVersion": p.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                "serverInfo": {"name": "notes", "version": "1.0"}}
    if m == "tools/list":
        return {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (d, s, _) in TOOLS.items()]}
    if m == "tools/call":
        tool = TOOLS.get(p.get("name"))
        if not tool:
            return {"content": [{"type": "text", "text": f"unknown tool {p.get('name')}"}], "isError": True}
        try:
            out = tool[2](p.get("arguments") or {})
            return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                    "isError": isinstance(out, dict) and "error" in out}
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text", "text": f"{e.__class__.__name__}: {e}"}], "isError": True}
    if m == "ping":
        return {}
    raise LookupError(m)


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:
            continue  # notification
        try:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "result": handle(msg)}
        except LookupError as e:
            reply = {"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": f"method not found: {e}"}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
