"""MCP Circuit adapter for the Notes demo: one notes.py web server per slot, port 10000+i."""
import json, os, subprocess, sys, urllib.request

from project import TargetBase

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "notes.py")


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
        return json.loads(r.read())


def all_notes(port):
    out, path = [], "/api/notes"
    while path:
        r = get(port, path)
        out += r["items"]
        path = r["next"]
    return out


class Target(TargetBase):
    def pids(self):
        out = subprocess.run(["pgrep", "-f", f"{APP} --port"], capture_output=True, text=True).stdout
        return {int(p) for p in out.split()}

    def launch(self, i, home):
        return subprocess.Popen([sys.executable, APP, "--port", str(self.port(i)), "--data", home],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True).pid

    def ready(self, i):
        return get(self.port(i), "/health").get("ok") is True

    def metrics(self, i):
        return {"notes": len(all_notes(self.port(i)))}

    def agent_env(self, url_host, port, workdir):
        return {"NOTES_URL": f"http://{url_host}:{port}"}

    def check(self, i, spec):
        """Task check: list of {"note": substring, "done"?, "tags"?} (such a note exists) or {"tag", "count"} (that many notes)."""
        notes = all_notes(self.port(i))
        for c in spec:
            if "count" in c:
                if sum(1 for n in notes if c["tag"] in n["tags"]) != c["count"]:
                    return False
            elif not any(c["note"].lower() in n["text"].lower()
                         and ("done" not in c or n["done"] == c["done"])
                         and set(c.get("tags", [])) <= set(n["tags"]) for n in notes):
                return False
        return True
