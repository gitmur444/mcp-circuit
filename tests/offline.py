#!/usr/bin/env python3
"""Everything but the agent: demo app workers, the MCP server inside the agent container reaching its worker's app,
the task check, the dashboard API. No Claude login needed.  python3 tests/offline.py"""
import json, os, subprocess, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CIRCUIT_PROJECT"] = os.path.join(ROOT, "examples", "notes")
sys.path.insert(0, ROOT)
import pool, run  # noqa: E402

SLOTS = [pool.Slot(1), pool.Slot(2)]


def mcp_in_container(slot, calls):
    """Run the demo MCP server in the agent image, the way run.py wires it, and send it JSON-RPC lines."""
    fport = run.FWD_BASE + slot.i
    add_host, bind = run.docker_host()
    fwd = subprocess.Popen([sys.executable, os.path.join(ROOT, "docker", "forward.py"), str(fport), str(slot.port), bind])
    try:
        time.sleep(0.5)
        env = pool.TARGET.agent_env("host.docker.internal", fport, run.W)
        lines = "".join(json.dumps({"jsonrpc": "2.0", "id": k, "method": m, "params": p}) + "\n" for k, (m, p) in enumerate(calls, 1))
        out = subprocess.run(["docker", "run", "--rm", "-i"] + add_host + sum((["-e", f"{k}={v}"] for k, v in env.items()), []) +
                             ["-v", f"{os.path.join(os.environ['CIRCUIT_PROJECT'], 'mcp')}:/mcp:ro", run.IMAGE, "python3", "/mcp/server.py"],
                             input=lines, capture_output=True, text=True, timeout=60)
        return [json.loads(x) for x in out.stdout.splitlines()]
    finally:
        fwd.terminate()


def main():
    try:
        for s in SLOTS:
            print(f"slot {s.i}: ready in {s.start('seeded')} s")
        assert {s.pid for s in SLOTS} == {int(p) for s in SLOTS for p in pool._listeners(s.port)}, "slot pids must match port owners"
        r = mcp_in_container(SLOTS[0], [("initialize", {}), ("tools/list", {}),
                                        ("tools/call", {"name": "add_note", "arguments": {"text": "Call mom", "tags": ["family"]}}),
                                        ("tools/call", {"name": "list_notes", "arguments": {}})])
        assert len(r) == 4 and not r[2]["result"]["isError"], r
        assert len(json.loads(r[3]["result"]["content"][0]["text"])) == 31, "list_notes must walk all pages"
        print("MCP in container → app: ok")
        spec = [{"note": "Call mom", "tags": ["family"]}]
        assert pool.TARGET.check(1, spec) is True and pool.TARGET.check(2, spec) is False, "check sees only its own slot"
        print("task check: ok")
        dash = subprocess.Popen([sys.executable, os.path.join(ROOT, "dashboard.py"), "--port", "8799"])
        try:
            time.sleep(1.5)
            st = json.loads(urllib.request.urlopen("http://127.0.0.1:8799/api/state").read())
            assert st["project"]["name"] == "Notes demo" and all(x["alive"] for x in st["slots"][:2]), st["slots"]
            assert "demo.json" in st["sets"]
            print("dashboard: ok")
        finally:
            dash.terminate()
    finally:
        for s in SLOTS:
            s.stop()
    print("all offline checks passed")


if __name__ == "__main__":
    main()
