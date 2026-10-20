#!/usr/bin/env python3
"""MCP Circuit dashboard: app slots, workers, queue, results by condition. stdlib only.

  python3 dashboard.py [--port 8765] [--demo]   → http://localhost:8765

Slots: live poll of pool.py (port, pid, alive). Workers/queue/results: from the runner state file
<project>/out/e2e-state.json (format below); --demo substitutes a synthetic run to view the page without a runner.

e2e-state.json format (written atomically by the runner):
  {"run": "20261008-120000", "started": <unix>, "conditions": ["none","prod","sandbox"],
   "queue":   [{"id": "S01/prod/1", "scenario": "S01", "condition": "prod", "repeat": 1}, ...],
   "workers": [{"id": 1, "slot": 1, "job": {...}|null, "phase": "boot|agent|oracle|idle", "since": <unix>}, ...],
     calls: agent tool calls (or direct app calls without an agent), requests: model requests, tokens: input+output+cache.
   "done":    [{"id": ..., "scenario": ..., "condition": ..., "repeat": ..., "result": "ok|error|crash…", "solved": true,
                "calls": 14, "requests": 9, "time_s": 83.2, "tokens": 51234, "error": null}, ...]}
"""
import argparse, glob, json, os, random, re, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pool, project  # noqa: E402

OUT = project.OUT
STATE_FILE = os.path.join(OUT, "e2e-state.json")
_slots_cache = {"t": 0, "data": []}
_lock = threading.Lock()


TOGGLING = {}  # worker → "start"|"stop" while being toggled on/off from the dashboard


def toggle_worker(i, on):
    """In background: start the app from a template (~2 min) or stop it; state shows in slots() as toggling."""
    def run():
        try:
            pool.Slot(i).start() if on else pool.Slot(i).stop()
        except Exception as e:  # noqa: BLE001
            print(f"worker {i}: {e}", flush=True)
        finally:
            TOGGLING.pop(i, None)
            _slots_cache["t"] = 0
    TOGGLING[i] = "start" if on else "stop"
    threading.Thread(target=run, daemon=True).start()


def slots():
    """Processes only, no calls to the app: a parallel client may mix up replies to the agent/run (one connection at a time)."""
    with _lock:
        if time.time() - _slots_cache["t"] > 2:
            alive = pool._pids()
            st, lb = pool._load_state(), pool.labels()
            _slots_cache["data"] = [{"slot": i, "port": pool.TARGET.port(i), "pid": st.get(i), "alive": st.get(i) in alive or not pool.TARGET.has_app, "ready": None, "fps": None,
                                     "label": lb.get(i)}
                                    for i in range(1, 7)]
            _slots_cache["t"] = time.time()
        return [dict(x, toggling=TOGGLING.get(x["slot"])) for x in _slots_cache["data"]]


def smoke_state(log):
    """Probe run state from its stdout (e.g. a project smoke script): "checking M", "[k] slot S method: status", "→" at the end."""
    try:
        text = open(log).read()
    except OSError:
        return None
    m = re.search(r"checking (\d+)", text)
    total = int(m.group(1)) if m else 0
    done, last = [], {}
    for meth, where, st in re.findall(r"^(\S+) \[(\w+)\]: (\w+)", text, re.M):  # suspects.py format
        done.append({"id": meth, "scenario": f"{meth} [{where}]", "condition": st, "repeat": "", "solved": st == "ok",
                     "calls": None, "requests": None, "time_s": None, "tokens": None, "error": None})
    for k, slot, meth, st in re.findall(r"^\[(\d+)\] slot (\d+) (\S+): (\w+)$", text, re.M):
        done.append({"id": meth, "scenario": meth, "condition": st, "repeat": int(k), "solved": st == "ok",
                     "calls": None, "requests": None, "time_s": None, "tokens": None, "error": None})
        last[int(slot)] = meth
    finished = "→" in text
    now = time.time()
    workers = [{"id": s, "slot": s, "job": None if finished else {"scenario": f"after {meth}", "condition": "smoke", "repeat": ""},
                "phase": "idle" if finished else "agent", "since": now} for s, meth in sorted(last.items())]
    queue = [{"id": i, "scenario": "method", "condition": "smoke", "repeat": ""} for i in range(max(0, total - len(done)))]
    return {"run": "smoke" + (" (done)" if finished else ""), "started": os.path.getmtime(log) - 1 if finished else
            getattr(os.stat(log), "st_birthtime", os.path.getmtime(log)), "conditions": ["ok", "error", "reload", "timeout", "hang", "crash", "skip"],
            "queue": queue, "workers": workers, "done": done}


def runner_state():
    """Runner state; if its process is gone, the run is stopped: nothing is "running"."""
    try:
        st = json.load(open(STATE_FILE))
    except (OSError, ValueError):
        return None
    pid = st.get("pid")
    alive = bool(pid) and subprocess.run(["kill", "-0", str(pid)], capture_output=True).returncode == 0
    if not alive and not st.get("manual") and "(done)" not in st.get("run", ""):  # manual probes (manual.py) have no owner process
        st["run"] += " (stopped)"
        for w in st.get("workers", []):
            w.update(job=None, phase="idle")
        st["queue"] = []  # a stopped run waits for nothing
    return st


class Demo:
    """Synthetic run: 6 workers process a matrix of 4 scenarios × 3 conditions × 2 repeats."""
    PHASES = [("boot", 6), ("agent", 14), ("oracle", 3)]

    def __init__(self):
        self.t0 = time.time()
        conds = ["none", "prod", "sandbox"]
        jobs = [{"id": f"{s}/{c}/{r}", "scenario": s, "condition": c, "repeat": r}
                for s in ("S01_lives", "S02_close_windows", "S03_open_door", "S04_m3_move") for c in conds for r in (1, 2)]
        random.shuffle(jobs)
        self.st = {"run": "demo", "started": self.t0, "conditions": conds, "queue": jobs, "done": [],
                   "workers": [{"id": i, "slot": i, "job": None, "phase": "idle", "since": self.t0, "left": 0} for i in range(1, 7)]}
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            now = time.time()
            for w in self.st["workers"]:
                if w["job"] is None:
                    if self.st["queue"]:
                        w.update(job=self.st["queue"].pop(0), phase="boot", since=now, left=random.uniform(3, 8))
                    continue
                if now - w["since"] < w["left"]:
                    continue
                order = [p for p, _ in self.PHASES]
                nxt = order.index(w["phase"]) + 1
                if nxt < len(order):
                    w.update(phase=order[nxt], since=now, left=random.uniform(2, 2 * self.PHASES[nxt][1]))
                else:
                    j, c = w["job"], w["job"]["condition"]
                    base = {"none": (0.35, 40, 210, 160000), "prod": (0.8, 14, 95, 60000), "sandbox": (0.9, 11, 80, 48000)}[c]
                    ok = random.random() < base[0]
                    self.st["done"].append(dict(j, result="ok" if ok else "fail", solved=ok, calls=int(random.gauss(base[1], 3)),
                                                requests=int(random.gauss(base[1] * .7, 2)), time_s=round(random.gauss(base[2], 15), 1),
                                                tokens=int(random.gauss(base[3], 8000)), error=None))
                    w.update(job=None, phase="idle", since=now)
            time.sleep(0.5)

    def state(self):
        return {k: v for k, v in self.st.items()}


def keep_places(period=20):
    """Worker windows drift on their own (app start, macOS on profile/display change); every period s put them back in their cell.
    Only between runs: during a task the agent is connected to the app, so we don't open a second connection."""
    def run():
        while True:
            time.sleep(period)
            st = runner_state()
            if st and "(" not in st.get("run", "") and not st.get("manual"):
                continue  # run in progress
            if st and any(w.get("job") for w in st.get("workers", [])):
                continue  # a manual probe is open
            for i in range(1, 7):
                if i in TOGGLING:
                    continue
                try:
                    pool.Slot(i).place()
                except Exception:  # noqa: BLE001 — off or not responding
                    pass
    threading.Thread(target=run, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--smoke-log", help="smoke.py log: show its progress instead of the runner")
    a = ap.parse_args()
    demo = Demo() if a.demo else None

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            """POST /api/layout?mode=tile|video: switch profile and reposition windows of running workers."""
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            code, msg = 200, ""
            q = parse_qs(u.query)
            if u.path == "/api/go":  # run.py --hold: warmed-up workers wait for go.flag (all) or go-N.flag (one)
                i = (q.get("i") or [""])[0]
                open(os.path.join(OUT, f"go-{int(i)}.flag" if i else "go.flag"), "w").close()
                msg = f"worker {i}: agent started" if i else "agents started"
            elif u.path == "/api/stop":  # abort worker i's agent (or the whole run without i)
                i = (q.get("i") or [""])[0]
                ids = [int(i)] if i else list(range(1, 7))
                for k in ids:
                    open(os.path.join(OUT, f"stop-{k}.flag"), "w").close()
                names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True).stdout.split()
                kill = [n for n in names if any(n.startswith(f"stand-w{k}-") for k in ids)]
                if kill:
                    subprocess.run(["docker", "kill"] + kill, capture_output=True)
                if not i:  # whole run: don't continue the queue
                    st = runner_state()
                    if st and st.get("pid") and not st.get("manual"):
                        subprocess.run(["kill", str(st["pid"])], capture_output=True)
                msg = f"worker {i}: agent stopped" if i else "run stopped"
            elif u.path == "/api/warm":  # warm-up: run.py --file <tasks> --hold in background
                f = (q.get("file") or [""])[0]
                st = runner_state()
                path = next((p for p in (os.path.join(d, os.path.basename(f)) for d in project.TASKS) if os.path.isfile(p)), "")
                if st and "(" not in st.get("run", "") and not st.get("manual"):
                    code, msg = 409, "a run is already in progress"
                elif not os.path.isfile(path):
                    code, msg = 404, f"no such set {f}"
                else:
                    subprocess.Popen([sys.executable, os.path.join(HERE, "run.py"), "--file", path, "--hold"], cwd=HERE,
                                     stdout=open(os.path.join(OUT, "warm.log"), "w"), stderr=subprocess.STDOUT, start_new_session=True)
                    msg = f"warming up: {os.path.basename(f)}"
            elif u.path == "/api/reset":
                st = runner_state()
                active = st and "(" not in st.get("run", "")
                if st and st.get("manual"):  # manual probes: busy only while a probe is open
                    active = any(w.get("job") for w in st.get("workers", []))
                if active:
                    code, msg = 409, "run in progress; will clear after it"
                else:
                    try:
                        os.remove(STATE_FILE)
                    except OSError:
                        pass
                    msg = "tasks cleared (run records in <project>/out/runs/ kept)"
            elif u.path == "/api/worker":
                q = parse_qs(u.query)
                i, on = int((q.get("i") or ["0"])[0]), (q.get("on") or [""])[0] == "1"
                st = runner_state()
                busy = st and "(" not in st.get("run", "") and any(w.get("id") == i and w.get("job") for w in st.get("workers", []))
                if not 1 <= i <= 6:
                    code, msg = 400, "i: 1..6"
                elif busy:
                    code, msg = 409, f"worker {i} is running a task; stop the agent first (■ Stop)"
                elif i in TOGGLING:
                    code, msg = 409, f"worker {i} is already {'starting' if TOGGLING[i] == 'start' else 'stopping'}"
                else:
                    toggle_worker(i, on)
                    msg = f"worker {i}: {'starting (~2 min)' if on else 'stopping'}"
            elif u.path != "/api/layout":
                code, msg = 404, "not found"
            else:
                mode = (parse_qs(u.query).get("mode") or [""])[0]
                st = runner_state()
                busy = st and "(" not in st.get("run", "") and any(w.get("job") for w in st.get("workers", []))
                if mode not in ("tile", "video"):
                    code, msg = 400, "mode: tile | video"
                elif busy:
                    code, msg = 409, "run in progress; will move windows after it (one connection to the app at a time)"
                else:
                    pool.set_layout(mode)
                    moved = []
                    for i in sorted(pool._load_state()):
                        try:
                            pool.Slot(i).place(); moved.append(i)
                        except Exception as e:  # noqa: BLE001 — worker still loading or offline
                            msg += f"worker {i}: {e}; "
                    msg = f"moved: {moved}" + (f" ({msg})" if msg else "")
            body = json.dumps({"layout": pool.get_layout(), "message": msg}, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                self._get()
            except Exception as e:  # noqa: BLE001 — show the error instead of an empty reply
                body = json.dumps({"error": f"{e.__class__.__name__}: {e}"}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def _get(self):
            if self.path.startswith("/api/state"):
                body = json.dumps({"project": {"name": project.CFG.get("name"), "title": project.CFG.get("title")}, "demo": bool(demo), "layout": pool.get_layout(), "slots": slots(), "sets": sorted({os.path.basename(x) for d in project.TASKS for x in glob.glob(os.path.join(d, "*.json"))}), "runner": demo.state() if demo else smoke_state(a.smoke_log) if a.smoke_log else runner_state()},
                                  ensure_ascii=False, default=str).encode()
                ctype = "application/json"
            elif self.path.startswith("/assets/") and ".." not in self.path:  # v2 header images
                f = os.path.join(HERE, self.path.split("?")[0].lstrip("/"))
                if not os.path.isfile(f):
                    self.send_error(404); return
                body, ctype = open(f, "rb").read(), "image/png"
            else:  # the page is read from disk on every request
                body, ctype = open(os.path.join(HERE, "dashboard_v2.html"), "rb").read(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"http://localhost:{a.port}" + ("  (demo)" if demo else ""), flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
