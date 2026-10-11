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
            _slots_cache["data"] = [{"slot": i, "port": pool.TARGET.port(i), "pid": st.get(i), "alive": st.get(i) in alive, "ready": None, "fps": None,
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


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MCP Circuit</title><style>
:root{--bg:#f4f5f8;--card:#fff;--ink:#1d2330;--mut:#6b7385;--line:#e3e6ec;
--ok:#1f9d55;--ok-bg:#e7f6ed;--ok-bd:#bfe6cd;--run:#3b6fd8;--run-bg:#e9f0fd;--run-bd:#c3d4f7;--off:#c24b4b;--off-bg:#fbeeee;--off-bd:#f0cccc;
--warn:#d98a00;--vio:#7b61c9;--boot:#c98a12;--boot-bg:#fff6e2;--boot-bd:#f1dcaa;--gray:#9aa1b0;--shadow:0 1px 2px rgba(20,30,50,.06),0 4px 14px rgba(20,30,50,.05)}
@media (prefers-color-scheme:dark){:root{--bg:#11141a;--card:#1a1f28;--ink:#e6e9ef;--mut:#8a93a6;--line:#2a313d;
--ok-bg:#16291f;--ok-bd:#22533a;--run-bg:#17223a;--run-bd:#2b4475;--off-bg:#2c1a1c;--off-bd:#5a2d31;--boot-bg:#2f2614;--boot-bd:#5f4b1f;--gray:#5d6575;--shadow:none}}
*{box-sizing:border-box}html,body{overflow-x:hidden}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,system-ui,sans-serif}
header{display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;padding:18px 24px 6px}
h1{font-size:19px;margin:0;letter-spacing:-.01em}h2{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);margin:0 0 10px;font-weight:600}
.mut{color:var(--mut)}
.layout{max-width:1800px;display:grid;grid-template-columns:minmax(0,1fr);gap:16px;padding:14px 24px 28px}aside{order:-1}
@media (max-width:900px){.layout{grid-template-columns:1fr}aside{order:-1}}
main{display:grid;gap:22px;min-width:0}main>section{min-width:0}
.grid{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(190px,1fr))}
.w{border-radius:12px;padding:12px 14px;border:1px solid;min-height:118px;box-shadow:var(--shadow);transition:background .3s,border-color .3s;display:flex;flex-direction:column;gap:4px}
.w.idle{background:var(--ok-bg);border-color:var(--ok-bd)}.w.busy{background:var(--run-bg);border-color:var(--run-bd)}.w.off{background:var(--off-bg);border-color:var(--off-bd)}.w.boot{background:var(--boot-bg);border-color:var(--boot-bd)}
.w .top{display:flex;justify-content:space-between;align-items:center;gap:8px}.w .name{font-weight:650;white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:6px}
.idle .dot{background:var(--ok)}.busy .dot{background:var(--run);animation:pulse 1.4s infinite}.off .dot{background:var(--off)}.boot .dot{background:var(--boot);animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
.w .st{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.idle .st{color:var(--ok)}.busy .st{color:var(--run)}.off .st{color:var(--off)}.boot .st{color:var(--boot)}.w.boot .bar{background:rgba(201,138,18,.2)}.w.boot .bar i{background:var(--boot)}
.w .task{font-weight:500;word-break:break-word}.w .meta{font-size:12px;color:var(--mut)}
.w .bar{height:3px;border-radius:2px;background:rgba(59,111,216,.18);margin-top:auto;overflow:hidden}.w .bar i{display:block;height:100%;background:var(--run)}
aside{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:12px 18px;box-shadow:var(--shadow);display:flex;flex-wrap:wrap;align-items:center;gap:12px 32px}aside>div{display:flex;align-items:center;gap:10px}aside h2{margin:0}aside .note{margin:0;min-height:0}aside .seg{width:240px}aside .reset{width:auto;padding-left:14px;padding-right:14px}aside .kv{display:flex;flex-wrap:wrap;gap:6px 12px}aside .kv .b{display:none}aside .kv>span:not(.lbl):not(.b){display:none}aside .prog{width:160px}aside .big{font-size:22px}aside .push{margin-left:auto}
.run{font-weight:600}.big{font-size:30px;font-weight:700;letter-spacing:-.02em}
.prog{height:8px;border-radius:4px;background:var(--line);overflow:hidden;display:flex}.prog i{display:block;height:100%}
.kv{display:grid;grid-template-columns:auto 1fr auto;gap:8px 10px;align-items:center}
.kv .b{height:6px;border-radius:3px;background:var(--line);overflow:hidden}.kv .b i{display:block;height:100%}
.lbl{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;color:#fff;white-space:nowrap}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;font-variant-numeric:tabular-nums;vertical-align:top}
th{color:var(--mut);font-weight:500;font-size:12px}.wrap{overflow-x:auto}td.txt{font-size:13px;line-height:1.45;max-width:340px;min-width:220px}td.txt div{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px}@media(max-width:900px){.pair{grid-template-columns:1fr}}.card2{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;font-size:13px;line-height:1.55;max-height:360px;overflow:auto}.card2 h3{margin:0 0 8px;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--mut)}.card2 p{margin:0 0 8px}.card2 ol,.card2 ul{margin:0 0 8px;padding-left:20px}.card2 li{margin:2px 0}
.demo{margin:8px 24px 0;padding:10px 14px;border-radius:10px;background:#fff4dc;border:1px solid #f1d38f;color:#7a5200;font-weight:600}
@media (prefers-color-scheme:dark){.demo{background:#3a2e14;border-color:#6b5420;color:#f1d38f}}
.seg{display:grid;grid-template-columns:1fr 1fr;background:var(--line);border-radius:10px;padding:3px;gap:3px}
.seg button{border:0;border-radius:8px;padding:7px 6px;font:inherit;font-size:13px;background:transparent;color:var(--mut);cursor:pointer}
.seg button.on{background:var(--card);color:var(--ink);font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.12)}
.seg button:disabled{cursor:wait;opacity:.6}.note{font-size:12px;color:var(--mut);margin-top:6px;min-height:16px}
tr.row{cursor:pointer}tr.row:hover td{background:rgba(59,111,216,.04)}td.arr{width:14px;padding-right:0;color:var(--mut)}td.arr::before{content:"▸"}tr.row.open td.arr::before{content:"▾"}
tr.det td{background:var(--bg);padding:10px 14px 14px}.calls{width:100%;border:0;box-shadow:none;background:transparent}.calls td,.calls th{border-bottom:1px solid var(--line);padding:4px 8px;font-size:12px}
.calls .args{font-family:ui-monospace,Menlo,monospace;color:var(--mut);word-break:break-all;max-width:620px}.ans{white-space:pre-wrap;font-size:13px;margin-top:10px;max-height:320px;overflow:auto;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px}
.reset{width:100%;display:flex;align-items:center;justify-content:center;gap:8px;border:1px solid var(--off-bd);background:var(--off-bg);color:var(--off);
 border-radius:10px;padding:8px 10px;font:inherit;font-size:13px;font-weight:600;cursor:pointer;transition:filter .15s,transform .05s}
.reset:hover{filter:brightness(.97)}.reset:active{transform:translateY(1px)}.reset:disabled{opacity:.5;cursor:not-allowed}
.reset svg{width:15px;height:15px}.confirm{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.confirm .yes{background:var(--off);color:#fff;border-color:var(--off)}.confirm .no{background:var(--card);color:var(--ink);border-color:var(--line)}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}.filters button{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:3px 11px;cursor:pointer;font:inherit;font-size:13px}
.filters button.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
header,.demo{max-width:1800px}</style></head><body>
<header><h1>MCP Circuit</h1><span id="meta" class="mut"></span></header>
<div id="demo" class="demo" hidden>Demo: synthetic data, no apps running; this is what a run looks like. Real run: dashboard.py without --demo.</div>
<div class="layout">
<main>
<section><h2>Workers</h2><div id="workers" class="grid"></div></section>
<section><h2>Tasks</h2><div id="filters" class="filters"></div><div class="wrap"><table id="tasks"></table></div></section>
</main>
<aside id="side"></aside>
</div><script>
const N=6;
const STATE={wait:['waiting','var(--gray)'],run:['running','var(--run)'],done:['finished','var(--ink)']};
const RES={ok:'var(--ok)',answered:'#2a9d8f',done:'#2a9d8f',fail:'var(--off)',error:'var(--warn)','error+reload':'var(--warn)',reload:'var(--vio)',timeout:'var(--off)',hang:'var(--off)',crash:'var(--off)',skip:'var(--gray)',boot:'var(--gray)'};
const CC={none:'#c9774f',prod:'#7b61c9',sandbox:'#2a9d8f'};const PAL=['#5b7fc7','#c2185b','#8a6d3b','#2e7d32','#6a5acd','#d2691e','#00838f','#9c4f96'];
const ccol=c=>{if(CC[c])return CC[c];let h=0;for(const ch of c)h=(h*31+ch.charCodeAt(0))>>>0;return PAL[h%PAL.length]};
const PH={boot:'starting app',agent:'working',oracle:'checking',idle:'idle'};
const lbl=(t,c)=>`<span class="lbl" style="background:${c}">${t}</span>`;
const res=r=>r?lbl(r,RES[r]||'var(--run)'):'';
const fmt=(v,d=0)=>v==null?'—':Number(v).toLocaleString('en-US',{maximumFractionDigits:d});
const kf=n=>n==null?'—':n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?Math.round(n/1e3)+'k':String(n);
const ctxs=t=>t.ctx_max?`<b>${(100*t.ctx_max/(t.ctx_window||1e6)).toFixed(1)}%</b>`:'—';
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const inl=s=>esc(s).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');
const md=s=>{const out=[];let l=null;for(const raw of String(s||'').split('\n')){const x=raw.trim();const m=x.match(/^(\d+[.)]|[-*•])\s+(.*)/);
 if(m){const t=/\d/.test(m[1])?'ol':'ul';if(l!==t){if(l)out.push(`</${l}>`);out.push(`<${t}>`);l=t}out.push(`<li>${inl(m[2])}</li>`);continue}
 if(l){out.push(`</${l}>`);l=null}if(x)out.push(`<p>${inl(x)}</p>`)}if(l)out.push(`</${l}>`);return out.join('')};
const plain=s=>String(s||'').replace(/\*\*|`/g,'').replace(/\s+/g,' ').trim();
const opened=new Set();let confirmReset=false,resetNote='';
async function doReset(){confirmReset=false;try{const r=await fetch('/api/reset',{method:'POST'});resetNote=(await r.json()).message}catch(e){resetNote='dashboard unreachable'}opened.clear();tick()}
function toggle(k){opened.has(k)?opened.delete(k):opened.add(k);tick()}
let filter='all',layout='tile',note='',moving=false;
async function setLayout(m){if(moving||m===layout)return;moving=true;note='moving windows…';tick();
 try{const r=await fetch('/api/layout?mode='+m,{method:'POST'});const j=await r.json();layout=j.layout;note=r.ok?'windows moved':j.message}catch(e){note='dashboard unreachable'}
 moving=false;tick()}
async function tick(){
 let d;try{d=await (await fetch('/api/state')).json()}catch(e){document.getElementById('meta').textContent='dashboard.py unreachable';return}
 document.getElementById('demo').hidden=!d.demo;
 const now=Date.now()/1000,r=d.runner||{queue:[],workers:[],done:[],run:'—',started:now};
 layout=d.layout||layout;
 const online={};(d.slots||[]).forEach(s=>online[s.slot]=s.alive);
 const ws={};(r.workers||[]).forEach(w=>ws[w.id]=w);
 document.getElementById('meta').textContent=r.run==='—'?'no run':`updated ${new Date().toLocaleTimeString('en-US')}`;
 document.getElementById('workers').innerHTML=[...Array(N)].map((_,k)=>{const id=k+1,w=ws[id],on=!!online[id],busy=!!(w&&w.job);
  const cls=busy?(w.phase==='boot'?'boot':'busy'):on?'idle':'off',el=w?now-w.since:0;
  const st=busy?(PH[w.phase]||w.phase):on?'idle':'offline';
  return `<div class="w ${cls}"><div class="top"><span class="name"><span class="dot"></span>worker ${id}</span><span class="st">${st}</span></div>
  ${busy?`<div class="task">${esc(w.job.scenario||w.job.id)}</div>
   <div class="meta">${w.job.condition&&w.job.condition!=='?'?lbl(w.job.condition,ccol(w.job.condition))+' ':''}${fmt(el)} s</div>
   <div class="bar"><i style="width:${Math.min(100,el/1.2)}%"></i></div>`
   :`<div class="meta">${on?'waiting for task':'app not running'}</div>`}</div>`}).join('');
 const rows=[];
 (r.workers||[]).filter(w=>w.job).forEach(w=>rows.push({st:'run',t:w.job,w:w.id,since:w.since}));
 (r.queue||[]).forEach(t=>rows.push({st:'wait',t}));
 [...(r.done||[])].reverse().forEach(t=>rows.push({st:'done',t,res:t.result||t.condition}));
 const cnt={wait:0,run:0,done:0},rc={};rows.forEach(x=>{cnt[x.st]++;if(x.st=='done')rc[x.res]=(rc[x.res]||0)+1});
 const active=r.run!=='—'&&!/\(/.test(r.run);const total=rows.length||1,pct=Math.round(100*cnt.done/total),rmax=Math.max(1,...Object.values(rc));
 const segs=`<div class="seg">${[['tile','Full screen'],['video','Video 16:9']].map(([k,t])=>`<button class="${layout==k?'on':''}" ${moving?'disabled':''} onclick="setLayout('${k}')">${t}</button>`).join('')}</div>`;
 const rst=confirmReset?`<div class="confirm"><button class="reset yes" onclick="doReset()">Yes, clear</button><button class="reset no" onclick="confirmReset=false;tick()">Cancel</button></div>`
   :`<button class="reset" ${active?'disabled title="run in progress"':''} onclick="confirmReset=true;resetNote='';tick()"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6M10 11v6M14 11v6"/></svg>Clear dashboard</button>`;
 document.getElementById('side').innerHTML=`
  <div><div><div class="run">${esc(r.run)}</div><div class="mut">${r.run==='—'?'':active?`running ${fmt((now-r.started)/60,1)} min`:`ran ${fmt(((r.updated||now)-r.started)/60,1)} min`}</div></div></div>
  <div><span class="big">${pct}%</span><div><div class="prog"><i style="width:${100*cnt.done/total}%;background:var(--ink)"></i><i style="width:${100*cnt.run/total}%;background:var(--run)"></i></div><div class="mut" style="font-size:12px;margin-top:4px">done ${cnt.done} of ${rows.length}</div></div></div>
  <div><div class="kv">${['wait','run','done'].map(k=>`${lbl(STATE[k][0]+' '+cnt[k],STATE[k][1])}`).join('')}</div></div>
  <div><div class="kv">${Object.keys(rc).length?Object.entries(rc).sort((a,b)=>b[1]-a[1]).map(([k,v])=>lbl(k+' '+v,RES[k]||'var(--run)')).join(''):''}</div></div>
  <div class="push"><h2>App windows</h2>${segs}${note?`<span class="note">${esc(note)}</span>`:''}</div>
  <div>${rst}${resetNote?`<span class="note">${esc(resetNote)}</span>`:''}</div>`;
 const keys=['all','wait','run','done',...Object.keys(rc).filter(k=>!STATE[k])];
 document.getElementById('filters').innerHTML=keys.map(k=>`<button class="${filter==k?'on':''}" onclick="filter='${k}';tick()">${k=='all'?'all':STATE[k]?STATE[k][0]:k}</button>`).join('');
 const vis=rows.filter(x=>filter=='all'||x.st==filter||(x.st=='done'&&x.res==filter));
 document.getElementById('tasks').innerHTML='<tr><th></th><th>prompt</th><th>condition</th><th>state</th><th>result</th><th>worker</th><th>time, s</th><th>tool calls</th><th>ctx</th><th>answer / error</th></tr>'+
  vis.map(x=>{const t=x.t;const cond=t.condition&&!['?',x.res].includes(t.condition)?t.condition:'';const key=x.st+':'+(t.id||t.scenario);const op=opened.has(key);
   const calls=(t.calls_detail||[]);
   const det=op?`<tr class="det"><td colspan="10"><div class="pair"><div class="card2"><h3>Prompt</h3>${md(t.prompt||t.scenario)}</div><div class="card2"><h3>${t.answer?'Agent answer':'Error'}</h3>${t.answer||t.error?md(t.answer||t.error):'<p class="mut">none yet</p>'}</div></div>${calls.length?`<table class="calls"><tr><th>#</th><th>tool</th><th>arguments</th><th>s</th><th>outcome</th></tr>${calls.map((c,i)=>`<tr><td>${i+1}</td><td><b>${esc(c.tool)}</b></td><td class="args">${esc(c.input)}</td><td>${c.dur??''}</td><td>${c.error?lbl('error','var(--off)'):''} <span class="args">${esc((c.out||'').slice(0,160))}</span></td></tr>`).join('')}</table>`:'<span class="mut">no tool calls</span>'}
     ${t.usage?`<div class="mut" style="margin-top:8px;font-size:12px">model <b>${esc(t.model||'?')}</b> · window ${kf(t.ctx_window)} · peak context ${kf(t.ctx_max)} · over all ${t.requests} requests: cache read ${kf(t.usage.cache_read)}, cache write ${kf(t.usage.cache_write)}, new input ${kf(t.usage.input)}, model output ${kf(t.usage.output)}</div>`:''}
     </td></tr>`:'';
   return `<tr class="row ${op?'open':''}" onclick="toggle('${key.replace(/'/g,'')}')"><td class="arr"></td><td class="txt"><div>${esc(plain(t.prompt||t.scenario||t.id))}</div></td><td>${cond?lbl(cond,ccol(cond)):''}</td><td>${lbl(STATE[x.st][0],STATE[x.st][1])}</td><td>${x.st=='done'?res(x.res):''}</td>
   <td>${x.w||t.worker||''}</td><td>${x.st=='run'?fmt(now-x.since):fmt(t.time_s,2)}</td><td>${fmt(t.calls)}</td><td>${ctxs(t)}</td><td class="txt a"><div>${esc(plain(t.answer||t.error||t.comment))}</div></td></tr>${det}`}).join('');
}
tick();setInterval(tick,1500);
</script></body></html>"""


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
            if self.path.startswith("/api/state"):
                body = json.dumps({"project": {"name": project.CFG.get("name"), "title": project.CFG.get("title")}, "demo": bool(demo), "layout": pool.get_layout(), "slots": slots(), "sets": sorted({os.path.basename(x) for d in project.TASKS for x in glob.glob(os.path.join(d, "*.json"))}), "runner": demo.state() if demo else smoke_state(a.smoke_log) if a.smoke_log else runner_state()},
                                  ensure_ascii=False, default=str).encode()
                ctype = "application/json"
            elif self.path.startswith("/assets/") and ".." not in self.path:  # v2 header images
                f = os.path.join(HERE, self.path.split("?")[0].lstrip("/"))
                if not os.path.isfile(f):
                    self.send_error(404); return
                body, ctype = open(f, "rb").read(), "image/png"
            elif self.path.startswith("/v2"):  # "technical drawing" variant, read from disk on every request
                body, ctype = open(os.path.join(HERE, "dashboard_v2.html"), "rb").read(), "text/html; charset=utf-8"
            else:
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"http://localhost:{a.port}" + ("  (demo)" if demo else ""), flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
