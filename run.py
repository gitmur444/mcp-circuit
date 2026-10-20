#!/usr/bin/env python3
"""Circuit runner: a task for an agent on a worker. Each job gets a fresh app instance from template + a fresh Docker container.

  python3 run.py --job 1 prod "Open the settings window" --job 2 none "What is on the screen now?"
  python3 run.py --cond sandbox --repeat 3 "Close all windows"        # no number: any free worker
  python3 run.py --file tasks.json --hold                             # warm up workers, start agents via dashboard button
  python3 run.py --file tasks.json                                    # [{"prompt", "cond", "worker"?, "repeat"?, "fresh"?, "template"?}]

Multi-step scenarios (a task in --file): "steps": [{"prompt", "check"?}, ...] instead of "prompt" — turns of one
conversation in one container (--continue), like a human's follow-ups; a step's "check" is a checkpoint, a failed one
ends the scenario. "answers": [{"match", "action"?, "content"?, "pick"?, "once"?}] — the human's answers to MCP forms
(elicitation), see docker/elicit.py; a form no rule matches is declined and fails the scenario. "expect_forms": [regex]
— forms that must be shown. "name" — scenario title on the dashboard, "timeout" — seconds for the whole scenario.

App restart before a job ("fresh"): --job: no (worker's app as is); queue and --file: yes (clean
experiment). Override: --fresh / --reuse for the whole run or "fresh": true|false per job. An app not responding
at run start is always restarted.

App failure under the agent: job result is crash (process died) or hang (alive, not responding); the agent's answer
is kept. Then, per --revive / --no-revive or "revive": true|false per job: restart the app before the worker's next
job, or mark them skip. Default: no restart (competition: a crashed worker drops out).

Conditions: a JSON file (--conditions, default conditions.json in the project): {"name": {"server": MCP folder path | null,
  "entry": "server.py", "env": {...}, "model": ..., "max_turns": 60, "prompt_suffix": "...", "show_skills": false, "desc": ...}}. The agent sees the project workspace (ro, without hidden folders), its worker's
app via port forwarding, MCP per condition. Progress and results on the dashboard (dashboard.py), records in <project>/out/runs/<run>/.
"""
import argparse, glob, json, os, queue, re, shutil, socket, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pool, project  # noqa: E402
from runstate import RunState  # noqa: E402

CFG = project.CFG
WORKSPACE = project.path(CFG["workspace"]) if CFG.get("workspace") else None
W = "/work"
IMAGE = CFG.get("image", "mcp-circuit")
FWD_BASE = 20000          # container connects to host.docker.internal:20000+N → worker app 127.0.0.1:<port of slot N>
HIDE = CFG.get("hide", [".git"])
SKILLS = CFG.get("skills")
MCP_NAME = CFG.get("mcp_name", "app")
# the agent can't see skills or MCP code (only what the condition provides); hide .git in big repos, otherwise claude -p
# spends minutes at startup on git status through the bind mount
CONDITIONS = {}  # loaded in main(): --conditions <file>, default conditions.json in the project
MAX_TURNS, TIMEOUT = 60, 1200
NOTE = CFG.get("prompt_note", "")
OUT = project.OUT


def up(port, host="127.0.0.1"):
    try:
        socket.create_connection((host, port), 1).close()
        return True
    except OSError:
        return False


def credentials(tmp):
    """Docker args that log the agent in with the user's own Claude: subscription token from `claude setup-token`
    (CLAUDE_CODE_OAUTH_TOKEN), else the local Claude Code login (macOS Keychain / ~/.claude/.credentials.json),
    else ANTHROPIC_API_KEY."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return ["-e", "CLAUDE_CODE_OAUTH_TOKEN"]
    cred = ""
    if sys.platform == "darwin":
        cred = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                              capture_output=True, text=True).stdout.strip()
    if not cred:
        try:
            cred = open(os.path.expanduser("~/.claude/.credentials.json")).read().strip()
        except OSError:
            pass
    if cred:
        cf = os.path.join(tmp, "credentials.json")
        with open(os.open(cf, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
            f.write(cred)
        os.chmod(cf, 0o644)  # container uid differs; the tmp dir itself is 0700
        return ["-v", f"{cf}:/home/agent/.claude/.credentials.json:ro"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ["-e", "ANTHROPIC_API_KEY"]
    raise SystemExit("no Claude login: run `claude setup-token` and export CLAUDE_CODE_OAUTH_TOKEN (see `circuit doctor`)")


def docker_host():
    """How the container reaches the host, and which host address the forwarder listens on."""
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return [], "127.0.0.1"  # Docker Desktop routes host.docker.internal to the host's localhost
    gw = subprocess.run(["docker", "network", "inspect", "bridge", "-f", "{{(index .IPAM.Config 0).Gateway}}"],
                        capture_output=True, text=True).stdout.strip() or "172.17.0.1"
    return ["--add-host", "host.docker.internal:host-gateway"], gw


HOST_MCP = 21000  # condition "host": true → the MCP runs on the host, the container reaches it at host.docker.internal:21000+N


def host_mcp(slot, srv, cfg, bind):
    """MCP on the host for one job: a TCP server; each connection (one per claude -p) gets its own server process with
    the host env + the condition's "host_env". For MCPs that launch/kill the app themselves (that's the host's job)."""
    env = dict(os.environ, **{k: os.path.expanduser(v) for k, v in (cfg.get("host_env") or {}).items()})
    ls = socket.socket()
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind((bind, HOST_MCP + slot.i))
    ls.listen()
    procs = []

    def serve(conn):
        p = subprocess.Popen([sys.executable, os.path.join(srv, cfg.get("entry", "server.py"))], cwd=srv, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        procs.append(p)

        def down():
            for b in iter(lambda: p.stdout.read1(65536), b""):
                conn.sendall(b)
            conn.close()
        threading.Thread(target=down, daemon=True).start()
        try:
            for b in iter(lambda: conn.recv(65536), b""):
                p.stdin.write(b)
                p.stdin.flush()
        except OSError:
            pass
        p.terminate()

    def loop():
        while True:
            try:
                c, _ = ls.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()

    def close():
        ls.close()
        for p in procs:
            p.terminate()
    return close


def server_dir(cond):
    s = CONDITIONS[cond].get("server")
    return None if not s else project.path(s)


def run_agent(slot, job, outdir, model, on_live=None):
    """Container running claude -p against the worker's app. Returns metrics and the answer."""
    fport = FWD_BASE + slot.i
    add_host, bind = docker_host()
    fwd = subprocess.Popen([sys.executable, os.path.join(HERE, "docker", "forward.py"), str(fport), str(slot.port), bind],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) if pool.TARGET.has_app else None
    tmp, name, close_host = tempfile.mkdtemp(prefix="stand-run-"), None, None
    try:
        for _ in range(30 if fwd else 0):
            if fwd.poll() is not None:
                raise RuntimeError(f"port forwarder {fport} → {slot.port} failed (port busy? a stale forward.py)")
            if up(fport, bind):
                break
            time.sleep(0.1)
        auth = credentials(tmp)
        cfg = CONDITIONS[job["condition"]]
        srv = server_dir(job["condition"])
        env = pool.TARGET.agent_env("host.docker.internal", fport, W)
        env.update(cfg.get("env") or {})
        mcp = {"mcpServers": {}} if not srv else {"mcpServers": {MCP_NAME: {
            "type": "stdio", "command": "python3", "args": ["/mcp/" + cfg.get("entry", "server.py")], "env": env}}}
        if srv and cfg.get("host"):
            close_host = host_mcp(slot, srv, cfg, bind)
            mcp = {"mcpServers": {MCP_NAME: {"type": "stdio", "command": "python3",
                                             "args": ["/cfg/bridge.py", "host.docker.internal", str(HOST_MCP + slot.i)]}}}
        if cfg.get("mcp"):  # a ready MCP server config (e.g. remote http); ${VAR} taken from the host env
            mcp = {"mcpServers": {MCP_NAME: json.loads(os.path.expandvars(json.dumps(cfg["mcp"])))}}
            if "${" in json.dumps(mcp["mcpServers"]):
                raise RuntimeError(f"condition {job['condition']}: unset env var in its mcp config")
        json.dump(mcp, open(os.path.join(tmp, "mcp.json"), "w"))
        vol = (["-v", f"{WORKSPACE}:{W}:ro"] if WORKSPACE else []) + auth + [
               "-v", f"{tmp}/mcp.json:/cfg/mcp.json:ro", "--tmpfs", "/home/agent:uid=1000,gid=1000,mode=0755"]
        if srv and not cfg.get("host"):
            vol += ["-v", f"{srv}:/mcp:ro"]
        vol += ["-v", f"{os.path.join(HERE, 'docker', 'bridge.py')}:/cfg/bridge.py:ro"]
        for d in HIDE if not cfg.get("show_skills") else [h for h in HIDE if h != SKILLS]:
            if WORKSPACE and os.path.isdir(os.path.join(WORKSPACE, d)):
                vol += ["--tmpfs", f"{W}/{d}:ro,mode=0555"]
        logdir = os.path.join(tmp, "log")
        os.makedirs(logdir)
        os.chmod(logdir, 0o777)  # the hook writes here as the container user
        json.dump(job.get("answers") or [], open(os.path.join(tmp, "answers.json"), "w"), ensure_ascii=False)
        json.dump({"hooks": {"Elicitation": [{"hooks": [{"type": "command", "command": "python3 /cfg/elicit.py"}]}]}},
                  open(os.path.join(tmp, "settings.json"), "w"))
        vol += ["-v", f"{tmp}/answers.json:/cfg/answers.json:ro", "-v", f"{tmp}/settings.json:/cfg/settings.json:ro",
                "-v", f"{os.path.join(HERE, 'docker', 'elicit.py')}:/cfg/elicit.py:ro", "-v", f"{logdir}:/log"]
        model = cfg.get("model") or model
        envs = ["-e", "GIT_CONFIG_COUNT=1", "-e", "GIT_CONFIG_KEY_0=safe.directory",
                "-e", "GIT_CONFIG_VALUE_0=*", "-e", "DISABLE_AUTOUPDATER=1", "-e", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"]
        claude = ('exec claude -p "$PROMPT" --output-format stream-json --verbose --dangerously-skip-permissions '
                  f'--max-turns {cfg.get("max_turns", MAX_TURNS)} --strict-mcp-config --mcp-config /cfg/mcp.json '
                  '--settings /cfg/settings.json' + (f" --model {model}" if model else ""))
        name = f"stand-w{slot.i}-{os.getpid()}-{int(time.time())}"
        # one container for the whole scenario: steps are turns of one conversation (--continue), like a human's follow-ups
        subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-w", W] + add_host + vol + envs + [IMAGE, "sleep", "infinity"],
                       check=True, capture_output=True)
        subprocess.run(["docker", "exec", name, "sh", "-c", 'echo \'{"hasCompletedOnboarding":true,"bypassPermissionsModeAccepted":true}\' > ~/.claude.json'],
                       check=True, capture_output=True)
        steps = job.get("steps") or [{"prompt": job["prompt"]}]
        t0, calls, final, results, pending, ctx_max, last_live, live_model, live_usage = time.time(), [], "", [], {}, 0, 0.0, None, {}
        forms, step_res, failed, cur = [], [], None, [1]
        flog = os.path.join(logdir, "elicit.jsonl")
        timeout = job.get("timeout") or TIMEOUT
        killer = threading.Timer(timeout, lambda: subprocess.run(["docker", "kill", name], capture_output=True))
        killer.start()

        def read_forms(now, call=None):
            """New forms the hook answered. A form is shown while a tool call runs: tie it to that call (`call` = its
            index in calls), so the dashboard puts it right after it in the timeline."""
            try:
                lines = open(flog).read().splitlines()
            except OSError:
                return
            for line in lines[len(forms):]:
                f = json.loads(line)
                f["step"], f["t"], f["call"] = cur[0], now, len(calls) - 1 if call is None else call
                forms.append(f)

        log_path = os.path.join(outdir, job["id"].replace("/", "_") + ".jsonl")
        with open(log_path, "w", buffering=1) as log:
            for n, step in enumerate(steps, 1):
                cur[0], final = n, ""
                prompt = step["prompt"] + ((cfg.get("prompt_suffix") or "") + cfg.get("prompt_note", NOTE).format(port=fport) if n == 1 else "")
                if len(steps) > 1:
                    calls.append({"tool": f"▶ step {n}/{len(steps)}", "t": round(time.time() - t0, 1), "dur": 0, "input": step["prompt"][:300], "out": ""})
                cmd = ["docker", "exec", "-i", "-e", "PROMPT=" + prompt, name, "sh", "-c", claude + (" --continue" if n > 1 else "")]
                result = {}
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in p.stdout:
                    log.write(line)
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    now = round(time.time() - t0, 1)
                    if e.get("type") == "system" and e.get("model"):
                        live_model = e["model"]
                    if e.get("type") == "assistant":
                        mu = e["message"].get("usage") or {}
                        live_usage[e["message"].get("id")] = mu  # one request = one message, with several events  # this request's context = everything the model sees at once
                        ctx_max = max(ctx_max, sum(mu.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                                              "cache_creation_input_tokens")))
                        for c in e["message"].get("content", []):
                            if c.get("type") == "tool_use":
                                pending[c["id"]] = len(calls)
                                calls.append({"tool": c["name"].replace(f"mcp__{MCP_NAME}__", ""), "t": now, "ts": time.time(),
                                              "input": json.dumps(c.get("input"), ensure_ascii=False)[:300]})
                            elif c.get("type") == "text":
                                final = c["text"]
                    elif e.get("type") == "user" and isinstance((e.get("message") or {}).get("content"), list):
                        for c in e["message"]["content"]:
                            if c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                                k = pending.pop(c["tool_use_id"])
                                calls[k]["ts_end"] = time.time()
                                read_forms(now, k)  # forms logged before this result were asked during this call
                                body = c.get("content")
                                text = body if isinstance(body, str) else " ".join(
                                    x.get("text", "[" + x.get("type", "?") + "]") for x in body or [] if isinstance(x, dict))
                                calls[k].update(dur=round(now - calls[k]["t"], 1), error=bool(c.get("is_error")), out=text[:300])
                    if on_live and time.time() - last_live >= 1:  # dashboard: live tool calls, at most once per second
                        last_live = time.time()
                        on_live({"calls": len(calls), "calls_detail": [dict(c) for c in calls], "forms": list(forms),
                                 "steps": step_res + [{"prompt": step["prompt"], "answer": final or None}], "ctx_max": ctx_max or None, "answer": final or None, "model": live_model,
                                 "requests": len(live_usage), "usage": {k: sum(u.get(f) or 0 for u in live_usage.values()) for k, f in
                                           (("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens"),
                                            ("input", "input_tokens"), ("output", "output_tokens"))}})
                    if e.get("type") == "result":
                        result = e
                        final = e.get("result") or final
                p.wait()
                read_forms(round(time.time() - t0, 1))
                results.append(result)
                sr = {"prompt": step["prompt"], "answer": final,
                      "result": "error" if (not result or result.get("is_error")) else "answered"}
                if sr["result"] == "answered" and step.get("check") is not None:  # checkpoint between steps: the app state now
                    sr["solved"] = pool.TARGET.check(slot.i, step["check"], final)
                    if sr["solved"] is False and getattr(pool.TARGET, "why", None):
                        sr["why"] = pool.TARGET.why
                step_res.append(sr)
                if sr["result"] != "answered" or sr.get("solved") is False:
                    failed = n
                    break
        killer.cancel()
        for k in pending.values():  # agent cut off mid-call: the call stays in the table, flagged
            calls[k].update(dur=round(time.time() - t0 - calls[k]["t"], 1), error=True, out="interrupted")
        rs = [r for r in results if r]
        u = {f: sum((r.get("usage") or {}).get(f) or 0 for r in rs) for f in
             ("cache_read_input_tokens", "cache_creation_input_tokens", "input_tokens", "output_tokens")} if rs else \
            {f: sum(x.get(f) or 0 for x in live_usage.values()) for f in
             ("cache_read_input_tokens", "cache_creation_input_tokens", "input_tokens", "output_tokens")}
        mu = {}
        for r in rs:
            mu.update(r.get("modelUsage") or {})
        window = max([m.get("contextWindow") or 0 for m in mu.values()] or [0]) or None
        usage = {"cache_read": u.get("cache_read_input_tokens"), "cache_write": u.get("cache_creation_input_tokens"),
                 "input": u.get("input_tokens"), "output": u.get("output_tokens")}
        tokens = sum(u.values())
        timed_out = time.time() - t0 >= timeout - 1
        last = step_res[-1] if step_res else {"result": "error"}
        out = {"time_s": round(time.time() - t0, 1), "calls": len([c for c in calls if c["tool"][0] not in "▶⇄"]), "calls_detail": calls,
               "requests": sum(r.get("num_turns") or 0 for r in rs) or len(live_usage), "tokens": tokens or None, "ctx_max": ctx_max or None,
               "ctx_window": window, "usage": usage, "model": next(iter(mu), None) or live_model, "answer": final, "log": log_path,
               "result": "timeout" if timed_out else last["result"], "forms": forms}
        if len(steps) > 1:
            out["steps"] = step_res
        # forms: each expected one must have been shown; a form no rule answers fails the scenario that has rules
        missing = [x for x in job.get("expect_forms") or [] if not any(re.search(x, f["message"], re.I | re.S) for f in forms)]
        unexpected = [f["message"][:80] for f in forms if f.get("rule") is None or f.get("error")] if job.get("answers") else []
        why = ([f"step {failed}/{len(steps)}: " + ("checkpoint failed" + (f" ({last['why']})" if last.get("why") else "") if last.get("solved") is False else last["result"])] if failed and len(steps) > 1 else []) + \
              [f"form not shown: {x}" for x in missing] + [f"unexpected form: {x}" for x in unexpected]
        if not failed and any("solved" in s for s in step_res) and not job.get("check"):
            out.update(result="solved", solved=True)  # every checkpoint passed, no final check: the scenario is solved
        if failed and last.get("solved") is False or missing or unexpected:
            out.update(result="failed", solved=False, error="; ".join(why))
        return out
    finally:
        if fwd:
            fwd.terminate()
        if close_host:
            close_host()
        if name:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)


def ready(slot, tries=4):
    """Worker's app is running and responds. One failed reply (another connection to the app, busy frame) is no reason
    to restart it: retry several times."""
    for k in range(tries):
        if not slot.alive():
            return False
        if slot.ready():
            return True
        time.sleep(2)
    return False


BOOT = {"lock": threading.Lock(), "last": 0.0, "gap": 0.0}


def boot_turn():
    """Pause only between app restarts: simultaneous starts saturate disk and CPU. Reuse starts immediately."""
    with BOOT["lock"]:
        wait = BOOT["last"] + BOOT["gap"] - time.time()
        if wait > 0:
            time.sleep(wait)
        BOOT["last"] = time.time()


GO = os.path.join(OUT, "go.flag")  # --hold: agents wait for this file ("Start agents" button on the dashboard)
HOLD = False


def stop_flag(slot):
    """Dashboard "■ Stop" button: dashboard drops this file and kills the agent container."""
    return os.path.join(OUT, f"stop-{slot.i}.flag")


def wait_go(slot, st):
    """--hold: app is warmed up and in place; wait for the command to start agents."""
    mine = GO.replace(".flag", f"-{slot.i}.flag")  # start a single worker via the button on its card
    if HOLD and not (os.path.exists(GO) or os.path.exists(mine)):
        st.phase(slot.i, "ready")
        while not (os.path.exists(GO) or os.path.exists(mine) or os.path.exists(stop_flag(slot))):
            time.sleep(1)
        st.phase(slot.i, "agent")


def worker(slot, q, st, outdir, model, results, lock):
    crashed = False  # worker's app crashed or hung in this run
    while True:
        try:
            job = q.get_nowait()
        except queue.Empty:
            return
        if crashed and not job["revive"]:
            st.take(slot.i, slot.i, job["id"], "agent")
            res = {"result": "skip", "error": "worker's app crashed in this run, restart disabled (--revive)"}
            res["comment"] = res["error"]
            st.finish(slot.i, res)
            with lock:
                results.append(dict(job, **res))
                print(f"[{job['id']}] worker {slot.i}: skip · {res['error']}", flush=True)
            continue
        try:
            setup = job.get("setup")  # the adapter prepares the app itself
            if setup is not None:
                st.take(slot.i, slot.i, job["id"], "boot")
                pool.TARGET.setup(slot.i, setup)
                st.phase(slot.i, "agent")
            elif job["fresh"] or not ready(slot):
                st.take(slot.i, slot.i, job["id"], "boot")
                boot_turn()
                slot.start(job.get("template"), job.get("worker_template"))  # fresh app from template (templates/<template>), tiling, title
                time.sleep(5)
                slot.place()                  # the app's startup layout may override the first placement: place again
                st.phase(slot.i, "agent")
            else:
                st.take(slot.i, slot.i, job["id"], "agent")  # reuse: worker's app as is
            wait_go(slot, st)
            if os.path.exists(stop_flag(slot)):
                res = {"result": "stopped", "error": "stopped from dashboard before agent start"}
            else:
                res = run_agent(slot, job, outdir, model, lambda d: st.live(slot.i, d))
            if os.path.exists(stop_flag(slot)):
                os.remove(stop_flag(slot))
                res["result"] = "stopped"
            if setup is not None:
                if job.get("check") is not None and res.get("result") in ("answered", "error", "timeout"):
                    solved = pool.TARGET.check(slot.i, job["check"], res.get("answer"))
                    if solved is not None:
                        res["solved"], res["result"] = solved, "solved" if solved else "failed"
                    if solved is False and getattr(pool.TARGET, "why", None):
                        res["error"] = "check: " + pool.TARGET.why
            elif not ready(slot):               # crashed or hung under the agent: result is crash, keep the agent's answer
                crashed = True
                res["result"] = "crash" if not slot.alive() else "hang"
            elif job.get("check") is not None and res.get("result") in ("answered", "error", "timeout"):
                solved = pool.TARGET.check(slot.i, job["check"], res.get("answer"))  # the adapter decides (app state, or the answer vs known truth)
                if solved is not None:
                    res["solved"], res["result"] = solved, "solved" if solved else "failed"
        except Exception as e:  # noqa: BLE001 — the job fails, the worker moves on
            res = {"result": "error", "error": f"{e.__class__.__name__}: {e}"[:300]}
        if job.get("setup") is not None and hasattr(pool.TARGET, "teardown"):
            try:
                pool.TARGET.teardown(slot.i, job["setup"])
            except Exception as e:  # noqa: BLE001
                print(f"[{job['id']}] teardown: {e}", flush=True)
        res["comment"] = (res.get("answer") or res.get("error") or "")[:300]
        st.finish(slot.i, res)
        with lock:
            results.append(dict(job, **res))
            print(f"[{job['id']}] worker {slot.i}: {res['result']} · {res.get('time_s', '—')} s · calls {res.get('calls', '—')}"
                  f" · {res.get('comment', '')[:120]}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", nargs=3, action="append", metavar=("WORKER", "CONDITION", "PROMPT"), default=[])
    ap.add_argument("--conditions", default=project.CONDITIONS, help="experiment conditions file")
    ap.add_argument("--cond", help="condition for queued prompts (default: the first in the file)")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--file")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--model")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--fresh", action="store_true", help="fresh app from template before each job")
    g.add_argument("--reuse", action="store_true", help="no restart: worker's app as is")
    r = ap.add_mutually_exclusive_group()
    r.add_argument("--revive", action="store_true", help="restart an app that crashed during the run before the worker's next job")
    r.add_argument("--no-revive", action="store_true", help="don't restart a crashed app: the worker's remaining jobs are skipped (default)")
    ap.add_argument("prompts", nargs="*")
    ap.add_argument("--hold", action="store_true", help="warm up apps (templates, layout) and wait for the \"Start agents\" dashboard button")
    a = ap.parse_args()
    global HOLD
    HOLD = a.hold
    for f in glob.glob(GO.replace(".flag", "*.flag")) + glob.glob(os.path.join(OUT, "stop-*.flag")):
        os.remove(f)
    CONDITIONS.update(json.load(open(a.conditions)))
    a.cond = a.cond or next(iter(CONDITIONS))

    # app restart before a job: --job: no (one-off tasks); queue and file: yes (experiment); flags and
    # the job's "fresh" field in the file override the default
    tasks = [{"worker": int(w), "cond": c, "prompt": p, "fresh": False} for w, c, p in a.job]
    tasks += [{"cond": a.cond, "prompt": p, "fresh": True} for p in a.prompts]
    if a.file:
        tasks += [dict({"fresh": True}, **t) for t in json.load(open(a.file))]
    for t in tasks:  # "worker_template": name from worker_templates.json → condition + profile template
        if t.get("worker_template"):
            wt = pool.worker_template(t["worker_template"])
            t.setdefault("cond", wt["cond"]); t.setdefault("template", wt["profile"])
    for t in tasks:
        if a.fresh or a.reuse:
            t["fresh"] = a.fresh
    if not tasks:
        ap.error("no jobs")
    for t in tasks:
        if not (t.get("prompt") or t.get("steps")):
            ap.error(f"task without prompt or steps: {t}")
        if t["cond"] not in CONDITIONS:
            ap.error(f"condition {t['cond']}: available: {', '.join(CONDITIONS)}")

    run_id = time.strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(OUT, "runs", run_id)
    os.makedirs(outdir, exist_ok=True)
    jobs, k = [], 0
    for t in tasks:
        for r in range(1, t.get("repeat", a.repeat) + 1):
            k += 1
            prompt = t.get("prompt") or "\n→ ".join(s["prompt"] for s in t["steps"])  # multi-step: all steps, for the dashboard
            jobs.append({"id": f"{k:02d}", "scenario": (t.get("name") or prompt)[:80], "prompt": prompt,
                         "steps": t.get("steps"), "setup": t.get("setup"), "answers": t.get("answers"), "expect_forms": t.get("expect_forms"), "timeout": t.get("timeout"), "condition": t["cond"],
                         "repeat": r, "worker": t.get("worker"), "fresh": t["fresh"], "template": t.get("template"), "worker_template": t.get("worker_template"), "check": t.get("check"),
                         "revive": t.get("revive", False) if not (a.revive or a.no_revive) else a.revive})
    n = max([a.workers] + [j["worker"] or 0 for j in jobs])
    pinned = {i: queue.Queue() for i in range(1, n + 1)}
    free = queue.Queue()
    for j in jobs:
        (pinned[j["worker"]] if j["worker"] else free).put(j)
    st = RunState(f"run {run_id}", sorted(CONDITIONS), [dict(j) for j in jobs], n)
    print(f"run {run_id}: {len(jobs)} jobs, {n} workers; dashboard http://localhost:8765; records {outdir}", flush=True)

    class Both:  # worker-pinned jobs first, then the shared queue
        def __init__(self, a_, b_):
            self.a, self.b = a_, b_

        def get_nowait(self):
            try:
                return self.a.get_nowait()
            except queue.Empty:
                return self.b.get_nowait()

    results, lock, ts = [], threading.Lock(), []
    busy = [i for i in range(1, n + 1) if not pinned[i].empty()] + [i for i in range(1, n + 1) if pinned[i].empty()]
    BOOT["gap"] = pool.stagger(n)
    for i in busy[:max(len(jobs), 1)]:
        t = threading.Thread(target=worker, args=(pool.Slot(i), Both(pinned[i], free), st, outdir, a.model, results, lock))
        t.start(); ts.append(t)
    for t in ts:
        t.join()
    st.close()
    json.dump(results, open(os.path.join(outdir, "results.json"), "w"), ensure_ascii=False, indent=1)
    print(f"done → {outdir}/results.json", flush=True)


if __name__ == "__main__":
    main()
