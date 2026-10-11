# MCP Circuit

**Test MCP servers with real agents on real apps.**

Is your MCP server actually helping agents, or just adding tokens? MCP Circuit runs the same tasks on several
isolated workers — each with its own app instance and its own Claude Code agent in Docker — under different conditions
(no MCP, MCP v1, MCP v2…), checks the result in the app itself, and shows everything live on a dashboard.

```
git clone https://github.com/gitmur444/mcp-circuit && cd mcp-circuit
./circuit demo
```

The demo starts 6 copies of a small Notes web app, gives the agents two tasks under three conditions and prints:

```
condition     solved  avg calls  avg time  avg tokens
good-mcp         2/2        9.0       10s      75,112
none             2/2        4.0       13s      88,626
raw-mcp          2/2        7.0       53s     139,091
```

## Requirements

- macOS or Linux, Python 3.9+, [Docker](https://docs.docker.com/get-docker/) running.
- A Claude subscription (Pro/Max) or API access. The agents use **your** Claude, found in this order:
  1. `CLAUDE_CODE_OAUTH_TOKEN` — run `claude setup-token` once and export the token (works everywhere);
  2. your local Claude Code login (macOS Keychain or `~/.claude/.credentials.json`);
  3. `ANTHROPIC_API_KEY`.

`./circuit doctor` checks all of this and tells you what's missing. No pip installs: standard library only.

## How it works

```
 task + condition ──► worker N ──► fresh app instance (from a profile template)
                               └─► fresh container: claude -p + the condition's MCP ──► app (only its own)
                     result ◄──── check in the app state, tool calls, tokens, time ──► dashboard
```

- **Worker** = one app instance + one agent at a time. Six by default.
- **Condition** = which MCP server the agent gets (or none), env, model, turn limit. Plain JSON.
- **Check** = the task's `"check"` field, evaluated by the adapter against the app's real state, not the agent's words.

## Your own app

```
./circuit init my-app                 # a copy of the demo to start from
CIRCUIT_PROJECT=my-app ./circuit run --file my-app/tasks/demo.json
```

A project folder holds everything app-specific:

| File | What |
|---|---|
| `project.json` | name, workspace the agent sees, MCP name, prompt note — see [`project.py`](project.py) |
| `target.py` | adapter `class Target(TargetBase)`: `launch`, `ready`, `pids`, `agent_env`, optional `check`, `metrics`, `place` |
| `conditions.json` | `{"name": {"server": "mcp folder" \| null, "env": {…}, "model": …, "max_turns": 60, "desc": …}}` |
| `templates/<name>/` | profile folders copied into a worker before each start (saves, databases, configs) |
| `tasks/*.json` | `[{"prompt", "cond", "template"?, "check"?, "repeat"?, "worker"?}]` |

Desktop apps work too: `place()` tiles worker windows 3×2 (`./circuit layout tile|video`).

## Commands

| | |
|---|---|
| `./circuit demo` | setup check, image build, demo run, dashboard |
| `./circuit run --file tasks.json` | run a task set; `--cond X --repeat 3 "prompt"` for ad-hoc tasks, `--hold` to start agents from the dashboard |
| `./circuit up [N]` / `down` / `status` | app workers |
| `./circuit dashboard` | http://localhost:8765/v2 |

## License

MIT
