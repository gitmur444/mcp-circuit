"""Shared run state for the dashboard (dashboard.py): queue, workers, finished jobs → <project>/out/e2e-state.json.

    st = RunState("smoke", conditions=["ok", "error", "reload", "hang", "crash"], jobs=[{...}, ...])
    st.take(worker_id, slot, job_id, phase)  # worker took a job
    st.phase(worker_id, "oracle")            # phase change
    st.finish(worker_id, result_dict)        # job finished: job + result → done
Writes are atomic, from any thread. Format is described in dashboard.py.
"""
import json, os, threading, time

import project  # noqa: E402

PATH = os.path.join(project.OUT, "e2e-state.json")


class RunState:
    def __init__(self, run, conditions, jobs, workers=0):
        self.lock = threading.Lock()
        self.st = {"run": run, "started": time.time(), "conditions": conditions, "queue": list(jobs), "done": [], "pid": os.getpid(), "updated": time.time(),
                   "workers": [{"id": i, "slot": i, "job": None, "phase": "idle", "since": time.time()} for i in range(1, workers + 1)]}
        self._write()

    def _w(self, wid):
        for w in self.st["workers"]:
            if w["id"] == wid:
                return w
        w = {"id": wid, "slot": wid, "job": None, "phase": "idle", "since": time.time()}
        self.st["workers"].append(w)
        self.st["workers"].sort(key=lambda x: x["id"])
        return w

    def take(self, wid, slot, job_id, phase="boot"):
        with self.lock:
            job = next((j for j in self.st["queue"] if j["id"] == job_id), None)
            if job:
                self.st["queue"].remove(job)
            self._w(wid).update(slot=slot, job=job or {"id": job_id, "scenario": job_id, "condition": "", "repeat": ""},
                                phase=phase, since=time.time())
            self._write()

    def phase(self, wid, phase):
        with self.lock:
            self._w(wid).update(phase=phase, since=time.time())
            self._write()

    def live(self, wid, data):
        """Intermediate job progress (tool calls, context); the dashboard shows it on the "in progress" row."""
        with self.lock:
            self._w(wid)["live"] = data
            self._write()

    def finish(self, wid, result):
        with self.lock:
            w = self._w(wid)
            self.st["done"].append(dict(w["job"] or {}, **result))
            w.update(job=None, phase="idle", since=time.time(), live=None)
            self._write()

    def close(self):
        with self.lock:
            for w in self.st["workers"]:
                w.update(job=None, phase="idle")
            self.st["run"] += " (done)"
            self._write()

    def _write(self):
        self.st["updated"] = time.time()
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        json.dump(self.st, open(tmp, "w"), ensure_ascii=False, default=str)
        os.replace(tmp, PATH)
