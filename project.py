"""Project = what the circuit tests: the target app (adapter), its MCP conditions, profile templates, output folder.

The project folder is chosen by (first match): env CIRCUIT_PROJECT, "project" in circuit.json next to this file,
the bundled demo examples/notes.
Its project.json:
  {"name": "My App", "title": "header line on the dashboard",
   "target": "target.py",              # adapter module with class Target (interface: TargetBase below)
   "homes": "/tmp/mcp-circuit/myapp",  # per-slot profile folders + pool state
   "templates": "templates", "default_template": "clean",   # profile templates: folders copied into a slot before start
   "workspace": "~/src/my-app",        # mounted read-only into the agent container at /work (null: empty /work)
   "hide": [".git"], "skills": ".claude",   # hidden from the agent; skills shown only if the condition says show_skills
   "mcp_name": "app",                  # MCP server name in the agent config (tool prefix mcp__<name>__ is stripped in logs)
   "prompt_note": "...{port}...",      # appended to every prompt; {port} = app port as seen from the container
   "image": "mcp-circuit", "ready_timeout": 400,
   "stagger": {"seconds": 12, "from": 4},   # optional: space out starts of heavy apps
   "tasks": ["../scenarios"]}               # optional: folders of task sets for the dashboard "Warm up" (default tasks/, out/demo)
Next to it: conditions.json, worker_templates.json (optional), tasks/ (task sets), out/ (runs, dashboard state).
"""
import importlib.util, json, os

HERE = os.path.dirname(os.path.abspath(__file__))


def _dir():
    d = os.environ.get("CIRCUIT_PROJECT")
    if not d:
        try:
            d = json.load(open(os.path.join(HERE, "circuit.json")))["project"]
        except (OSError, ValueError, KeyError):
            d = "examples/notes"  # the bundled demo
    return os.path.abspath(os.path.join(HERE, os.path.expanduser(d)))


DIR = _dir()
CFG = json.load(open(os.path.join(DIR, "project.json")))
OUT = os.path.join(DIR, "out")
HOMES = os.path.expanduser(CFG.get("homes") or f"/tmp/mcp-circuit/{os.path.basename(DIR)}")
TEMPLATES = os.path.join(DIR, CFG.get("templates", "templates"))
CONDITIONS = os.path.join(DIR, "conditions.json")
WORKER_TEMPLATES = os.path.join(DIR, "worker_templates.json")
TASKS = [os.path.abspath(os.path.join(DIR, os.path.expanduser(p))) for p in CFG.get("tasks", [])] or [os.path.join(DIR, "tasks"), os.path.join(OUT, "demo")]  # task sets for the dashboard "Warm up"


def path(p):
    """Project-relative path (or ~) → absolute."""
    return os.path.abspath(os.path.join(DIR, os.path.expanduser(p)))


class TargetBase:
    """Adapter interface. Slot i = one app instance on port port(i), own profile folder `home`.
    has_app = False: nothing to launch (the target is remote, e.g. a public API); only check() matters."""
    has_app = True

    def __init__(self, cfg):
        self.cfg = cfg

    def port(self, i):
        """Port the agent (and MCP) talks to; forwarded into the container."""
        return 10000 + i

    def pids(self):
        """PIDs of all running instances of the app (the pool finds a new instance as the new pid)."""
        if not self.has_app:
            return set()
        raise NotImplementedError

    def launch(self, i, home):
        """Start instance i with profile folder `home`; return its pid if known (else the pool finds it), don't wait for readiness."""
        raise NotImplementedError

    def ready(self, i):
        """Instance i finished loading and accepts commands."""
        raise NotImplementedError

    def place(self, i, x, y, w, h, title, screen_h):
        """Optional: put the window into the cell (top-left x, y from screen top; size w×h) and title it."""

    def metrics(self, i):
        """Optional: live numbers for status, e.g. {"fps": 30}."""
        return {}

    def agent_env(self, url_host, port, workdir):
        """Env for the MCP server inside the container: how to reach the app at url_host:port."""
        return {}

    def check(self, i, spec, answer=None):
        """Optional: was the task solved? `spec` is the task's "check" field, `answer` the agent's final text.
        Return True/False; None = can't tell."""
        return None


def target():
    import sys
    if DIR not in sys.path:
        sys.path.insert(0, DIR)  # the adapter's own helpers
    spec = importlib.util.spec_from_file_location("circuit_target", os.path.join(DIR, CFG.get("target", "target.py")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Target(CFG)
