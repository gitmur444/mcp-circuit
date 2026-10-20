#!/usr/bin/env python3
"""Pool of app slots: each run gets its own app instance from a profile template. The app is driven by the project adapter
(project.py: target.py in the project folder).

Slot i: port target.port(i), profile folder <HOMES>/i<i> — a fresh template copy before every start.
Instances not started by the pool are left alone. Slot state: <HOMES>/pool.json (pid per slot).

  python3 pool.py up [N]         — start N slots (default 6), starts spaced per project "stagger"
  python3 pool.py restart I      — restart slot I from template (between runs)
  python3 pool.py status         — slots: pid, port, ready, adapter metrics
  python3 pool.py down           — close all slots
  python3 pool.py layout tile|video — window layout profile (tile: full screen, video: fit to 16:9) and rearrange
  python3 pool.py stand [tile|video] — bring up the whole stand: 6 workers in place + dashboard localhost:8765

From code:  with Pool(6) as pool: slot = pool.acquire(); ...; pool.release(slot)  — release restarts the slot.
"""
import json, os, queue, shutil, subprocess, sys, threading, time, webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import project  # noqa: E402

TARGET = project.target()
HOMES = project.HOMES
STATE = os.path.join(HOMES, "pool.json")
STATE_LOCK = threading.Lock()
GRID = (3, 2)
MARGIN = 16  # margin from screen edges, px
GAP = 12     # gap between windows, px


def _screen():
    """Main screen work area: x0, y0 (below menu bar), x1, y1. Only matters for adapters that place windows."""
    out = subprocess.run(["osascript", "-e", 'tell application "Finder" to get bounds of window of desktop'],
                         capture_output=True, text=True).stdout if sys.platform == "darwin" else ""
    try:
        x0, y0, x1, y1 = (int(v) for v in out.split(","))
    except ValueError:
        x0, y0, x1, y1 = 0, 0, 1920, 1080
    return x0, y0 + 25, x1, y1


SCREEN = _screen()
SCREEN_H = SCREEN[3]
LAYOUT_FILE = os.path.join(HOMES, "layout")


def _region(layout):
    """Tiling area. tile: full screen below menu bar; video: centered, fit to 16:9 (for video recording)."""
    x0, y0, x1, y1 = SCREEN
    if layout == "video":
        w, h = x1 - x0, y1 - y0
        if w * 9 > h * 16:  # screen wider than 16:9: crop width
            vw = h * 16 // 9
            x0 += (w - vw) // 2; x1 = x0 + vw
        else:
            vh = w * 9 // 16
            y0 += (h - vh) // 2; y1 = y0 + vh
    return x0, y0, x1, y1


def get_layout():
    try:
        return open(LAYOUT_FILE).read().strip() or "tile"
    except OSError:
        return "tile"


def set_layout(layout):
    if layout not in ("tile", "video"):
        raise SystemExit("profile: tile | video")
    os.makedirs(HOMES, exist_ok=True)
    open(LAYOUT_FILE, "w").write(layout)
# heavy apps: project.json "stagger": {"seconds": 12, "from": 4} — up to 4 slots start at once, more with 12 s spacing
STAGGER = (project.CFG.get("stagger") or {}).get("seconds", 0)
STAGGER_FROM = (project.CFG.get("stagger") or {}).get("from", 0)


def stagger(n):
    return STAGGER if n > STAGGER_FROM else 0
READY_TIMEOUT = project.CFG.get("ready_timeout", 400)


def _pids():
    return TARGET.pids()


def _listeners(port):
    """PIDs listening on a local TCP port (empty if lsof is missing)."""
    try:
        return subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout.split()
    except FileNotFoundError:
        return []


def _load_state():
    try:
        return {int(k): v for k, v in json.load(open(STATE)).items()}
    except (OSError, ValueError):
        return {}


def _save_state(st):
    os.makedirs(HOMES, exist_ok=True)
    tmp = f"{STATE}.{os.getpid()}.{threading.get_ident()}"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, STATE)


LABELS = os.path.join(HOMES, "labels.json")  # slot → {"template": worker template, "profile": profile template} of the last start
WORKER_TEMPLATES = project.WORKER_TEMPLATES


def worker_template(name):
    """Worker template = condition + profile template (README, "Terms")."""
    t = json.load(open(WORKER_TEMPLATES))[name]
    return dict(t, name=name)


def labels():
    try:
        return {int(k): v for k, v in json.load(open(LABELS)).items()}
    except (OSError, ValueError):
        return {}


def _set_label(i, v):
    with STATE_LOCK:
        lb = labels()
        lb[i] = v
        os.makedirs(HOMES, exist_ok=True)
        tmp = f"{LABELS}.{os.getpid()}.{threading.get_ident()}"
        json.dump(lb, open(tmp, "w"), ensure_ascii=False)
        os.replace(tmp, LABELS)


class Slot:
    def __init__(self, i):
        self.i, self.port, self.home = i, TARGET.port(i), f"{HOMES}/i{i}"
        self.title = f"Worker {i}"
        self.pid = _load_state().get(i)

    def _own(self, pid):
        self.pid = pid
        with STATE_LOCK:
            st = _load_state(); st[self.i] = pid; _save_state(st)

    def ready(self):
        if not TARGET.has_app:
            return True
        try:
            return TARGET.ready(self.i)
        except Exception:  # noqa: BLE001 — not up yet
            return False

    def alive(self):
        if not TARGET.has_app:
            return True
        return bool(self.pid) and self.pid in _pids()

    def stop(self):
        if TARGET.has_app and self.alive():
            subprocess.run(["kill", str(self.pid)])
            for _ in range(30):
                if not self.alive():
                    break
                time.sleep(0.5)
            else:
                subprocess.run(["kill", "-9", str(self.pid)])
        with STATE_LOCK:
            st = _load_state(); st.pop(self.i, None); _save_state(st)
        self.pid = None

    def start(self, template=None, worker_tpl=None):
        """Fresh profile from template (folder in the project templates/, default from project.json) → launch → wait until ready.
        Returns seconds."""
        if not TARGET.has_app:  # remote target (e.g. a public API): nothing to start
            _set_label(self.i, {"profile": None, "template": worker_tpl})
            return 0
        self.stop()
        # a stray instance (not in pool.json) may hold the slot port; it would answer instead of the new one
        held = _listeners(self.port)
        for p in held:
            if int(p) in _pids():
                subprocess.run(["kill", p])
        if held:
            time.sleep(2)
        template = template or project.CFG.get("default_template")
        shutil.rmtree(self.home, ignore_errors=True)
        src = os.path.join(project.TEMPLATES, template) if template else None
        if src and sys.platform == "darwin":
            subprocess.run(["cp", "-cR", src, self.home], check=True)  # APFS clone: instant even for big profiles
        elif src:
            shutil.copytree(src, self.home)
        else:
            os.makedirs(self.home)
        label = {"profile": template, "template": worker_tpl}
        _set_label(self.i, dict(label, booting=time.time()))  # dashboard: "warming up" until the app has loaded
        t0, before = time.time(), _pids()
        pid = TARGET.launch(self.i, self.home)  # adapters that know the pid return it
        while not pid and time.time() - t0 < 30 and not (_pids() - before):
            time.sleep(0.5)
        new = _pids() - before
        if not pid and not new:
            raise RuntimeError(f"slot {self.i}: process did not appear")
        self._own(pid or min(new))  # provisional when guessed: slots starting together see each other's new pids
        while time.time() - t0 < READY_TIMEOUT:
            if self.ready():
                if not pid:  # the process listening on this slot's port is ours
                    mine = [int(p) for p in _listeners(self.port) if int(p) in _pids()]
                    if mine:
                        self._own(mine[0])
                self.place()
                _set_label(self.i, label)
                return round(time.time() - t0)
            time.sleep(2)
        raise RuntimeError(f"slot {self.i}: not loaded within {READY_TIMEOUT} s")

    def place(self):
        if not TARGET.has_app:
            return
        """3x2 tiling over the layout region; the adapter moves the window."""
        x0, y0, x1, y1 = _region(get_layout())
        x0, y0, x1, y1 = x0 + MARGIN, y0 + MARGIN, x1 - MARGIN, y1 - MARGIN
        cw = (x1 - x0 - GAP * (GRID[0] - 1)) // GRID[0]
        ch = (y1 - y0 - GAP * (GRID[1] - 1)) // GRID[1]
        k = (self.i - 1) % (GRID[0] * GRID[1])
        x, y = x0 + (k % GRID[0]) * (cw + GAP), y0 + (k // GRID[0]) * (ch + GAP)
        TARGET.place(self.i, x, y, cw, ch, self.title, SCREEN_H)

    def status(self):
        s = {"slot": self.i, "port": self.port, "pid": self.pid, "alive": self.alive()}
        if s["alive"]:
            try:
                s["ready"] = TARGET.ready(self.i)
                s.update(TARGET.metrics(self.i))
            except Exception as e:  # noqa: BLE001
                s["error"] = str(e)
        return s


class Pool:
    """Slots 1..n. acquire() returns a ready slot, release() restarts it from template in the background."""

    def __init__(self, n=6):
        self.slots = [Slot(i) for i in range(1, n + 1)]
        self.free = queue.Queue()

    def __enter__(self):
        for k, s in enumerate(self.slots):
            if k:
                time.sleep(stagger(len(self.slots)))
            threading.Thread(target=self._boot, args=(s,), daemon=True).start()
        return self

    def __exit__(self, *_):
        for s in self.slots:
            s.stop()

    def _boot(self, s):
        try:
            s.start()
            self.free.put(s)
        except Exception as e:  # noqa: BLE001
            print(f"slot {s.i} failed to start: {e}", file=sys.stderr)

    def acquire(self, timeout=None):
        return self.free.get(timeout=timeout)

    def release(self, s):
        threading.Thread(target=self._boot, args=(s,), daemon=True).start()


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "up":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 6
        threads = []
        for i in range(1, n + 1):
            s = Slot(i)
            t = threading.Thread(target=lambda s=s: print(f"slot {s.i}: ready in {s.start()} s", flush=True))
            t.start(); threads.append(t)
            time.sleep(stagger(n))
        for t in threads:
            t.join()
    elif cmd == "restart":
        s = Slot(int(sys.argv[2]))  # pool.py restart N [worker_template from worker_templates.json]
        wt = worker_template(sys.argv[3]) if len(sys.argv) > 3 else None
        print(f"slot {s.i}: ready in {s.start(wt and wt['profile'], wt and wt['name'])} s")
    elif cmd == "layout":  # pool.py layout tile|video — switch profile and rearrange running instances
        set_layout(sys.argv[2])
        for i in sorted(_load_state()):
            Slot(i).place()
        print(f"profile {get_layout()}: windows rearranged")
    elif cmd == "stand":  # pool.py stand [tile|video] — whole stand: 6 workers + layout + dashboard
        if len(sys.argv) > 2:
            set_layout(sys.argv[2])
        dash = os.path.join(HERE, "dashboard.py")
        if not _listeners(8765):
            subprocess.Popen([sys.executable, dash, "--port", "8765"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        webbrowser.open("http://localhost:8765")
        sys.argv = [sys.argv[0], "up", "6"]
        return main()
    elif cmd == "down":
        for i in sorted(_load_state()):
            Slot(i).stop(); print(f"slot {i}: closed")
    for i in sorted(_load_state()) if cmd in ("status", "up", "restart") else []:
        print(json.dumps(Slot(i).status(), ensure_ascii=False))


if __name__ == "__main__":
    main()
