#!/usr/bin/env python3
"""Elicitation hook inside the agent container: answers MCP forms instead of a human, by the task's "answers" rules.

/cfg/answers.json: [{"match": "regex on the form message", "keys": ["down", "enter"]}, ...]
  keys — what a human presses in the form, like in Claude Code: focus starts on the first field; in a list (enum,
  yes/no) up/down move the ❯ cursor (it starts on the default), enter picks and goes to the next field, the last
  enter submits; "type:text" types into a text field; tab — next field without picking; esc — cancel the form.
  Keys ran out before submit → cancel. Instead of keys: "action": "accept|decline|cancel", "content": {"field": value},
  "pick": {"field": "regex of an enum option"}.
Rules are matched in order; "once": true uses a rule only for the first matching form. No match → decline
(the scenario didn't expect this form). Every form and answer goes to /log/elicit.jsonl (a host folder), the runner reads it live.
"""
import json, os, re, sys, time

LOG = "/log/elicit.jsonl"


def options(prop):
    """Enum values of a schema field with their titles: enum/enumNames, oneOf/anyOf [{const, title}], items of an array."""
    if prop.get("type") == "array":
        prop = prop.get("items") or {}
    if "enum" in prop:
        names = prop.get("enumNames") or prop["enum"]
        return list(zip(prop["enum"], names))
    return [(o.get("const"), o.get("title", o.get("const"))) for o in prop.get("oneOf") or prop.get("anyOf") or [] if "const" in o]


def fields(props):
    """Form fields as the human sees them: title, kind, options (value, title), cursor start."""
    out = []
    for n, p in props.items():
        opts = options(p)
        if p.get("type") == "boolean":
            opts = [(True, "Yes"), (False, "No")]
        start = next((k for k, (v, _) in enumerate(opts) if v == p.get("default")), 0)
        out.append({"name": n, "title": p.get("title") or n, "type": p.get("type"), "options": [t for _, t in opts],
                    "values": [v for v, _ in opts], "start": start, "default": p.get("default")})
    return out


def press(fs, keys):
    """Plays keys over the form → (action, content, error). Cursor per field, picks on enter."""
    cur, f, content = [x["start"] for x in fs], 0, {}
    for key in keys:
        k = key.lower()
        if k in ("esc", "escape"):
            return "cancel", None, None
        if f >= len(fs):
            return "accept", content, f"extra key after submit: {key}"
        x = fs[f]
        if k in ("down", "up") and x["options"]:
            cur[f] = (cur[f] + (1 if k == "down" else -1)) % len(x["options"])
        elif k.startswith("type:"):
            content[x["name"]] = key[5:] if x["type"] == "string" else json.loads(key[5:])
        elif k in ("enter", "tab"):
            if k == "enter" and x["options"]:
                v = x["values"][cur[f]]
                content[x["name"]] = [v] if x["type"] == "array" else v
            elif k == "enter" and x["name"] not in content and x["default"] is not None:
                content[x["name"]] = x["default"]
            f += 1
            if f == len(fs) and k == "enter":
                return "accept", content, None
        else:
            return "cancel", None, f"unknown key {key!r} on field {x['title']!r}"
    return "cancel", None, "keys ran out before the form was submitted"


def main():
    ev = json.load(sys.stdin)
    msg, schema = ev.get("message", ""), ev.get("requested_schema") or {}
    props = schema.get("properties") or {}
    try:
        rules = json.load(open("/cfg/answers.json"))
    except (OSError, ValueError):
        rules = []
    used = set()
    if os.path.exists(LOG):
        used = {json.loads(line).get("rule") for line in open(LOG) if line.strip()}
    rule, k = None, None
    for i, r in enumerate(rules):
        if re.search(r.get("match", ""), msg, re.I | re.S) and not (r.get("once") and i in used):
            rule, k = r, i
            break
    action, content, err = "decline", None, None
    if rule:
        action = rule.get("action", "accept")
        if rule.get("keys") is not None:
            action, content, err = press(fields(props), rule["keys"])
        elif action == "accept":
            content = dict(rule.get("content") or {})
            for field, want in (rule.get("pick") or {}).items():
                opts = options(props.get(field) or {})
                hit = next((v for v, t in opts if re.search(want, f"{v} {t}", re.I)), None)
                if hit is None:
                    err = f"pick {field}~{want!r}: no such option among {[t for _, t in opts]}"
                    action, content = "decline", None
                    break
                content[field] = [hit] if (props[field].get("type") == "array") else hit
    with open(LOG, "a") as f:
        f.write(json.dumps({"ts": time.time(), "server": ev.get("mcp_server_name"), "message": msg, "fields": fields(props),
            "keys": (rule or {}).get("keys"), "rule": k, "action": action,
            "content": content, "error": err}, ensure_ascii=False) + "\n")
    out = {"hookEventName": "Elicitation", "action": action}
    if content is not None:
        out["content"] = content
    print(json.dumps({"hookSpecificOutput": out}))


if __name__ == "__main__":
    main()
