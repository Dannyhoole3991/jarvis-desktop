"""
Paper-trading supervisor: keeps every eligible strategy paper trading, continuously.

Each tick it
  1. starts paper trading for any CANDIDATE (survived every research gate), up to a concurrency cap;
  2. restarts paper runs whose process has died (with a restart-storm limit);
  3. rewrites each run's Risk Engine gate file from the real paper-account state;
  4. periodically compares paper results with the backtest and rejects/stops runs that degrade.

Paper only: it builds on paper.PaperManager, which refuses anything but dry-run.
"""

import json
import os
import subprocess
import sys
import time

from . import freqtrade_adapter as A
from . import paper as P

STATUS_FILE = os.path.join(A.LAB_DIR, "paper_status.json")
MAX_RESTARTS_PER_HOUR = 5


def process_alive(pid):
    """True if a process with this pid exists. (os.kill(pid, 0) would TERMINATE on Windows, so it is never used.)"""
    if not pid:
        return False
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"], capture_output=True, text=True, timeout=20).stdout
            return str(int(pid)) in out
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


class Supervisor:
    def __init__(self, registry, manager, max_concurrent=3, evaluate_every=3600, alive=process_alive, clock=time.time, log=None):
        self.registry = registry
        self.manager = manager
        self.max_concurrent = max_concurrent
        self.evaluate_every = evaluate_every
        self.alive = alive
        self.clock = clock
        self.log = log or (lambda category, detail, sid=None, ver=None: registry.log_decision(category, detail, sid, ver))
        self._last_eval = {}
        self._restarts = {}   # (sid, ver) -> [timestamps]

    def _pid(self, sid, ver):
        path = P._paths(sid, ver)["pid"]
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return int(f.read().strip())
        except Exception:
            return None

    def _can_restart(self, key):
        now = self.clock()
        recent = [t for t in self._restarts.get(key, []) if now - t < 3600]
        self._restarts[key] = recent
        return len(recent) < MAX_RESTARTS_PER_HOUR

    def tick(self):
        actions = []
        running = []
        for v in self.registry.list_versions(status="paper"):
            sid, ver = v["strategy_id"], v["version"]
            key = (sid, ver)
            if self.alive(self._pid(sid, ver)):
                running.append(key)
            elif self._can_restart(key):
                try:
                    self.manager.start(sid, ver)
                    self._restarts.setdefault(key, []).append(self.clock())
                    running.append(key)
                    actions.append(f"restarted {sid} v{ver}")
                    self.log("paper_restart", "Paper run was not running; restarted.", sid, ver)
                except Exception as error:
                    actions.append(f"restart failed {sid} v{ver}: {error}")
                    self.log("paper_error", f"Could not restart: {error}", sid, ver)
            else:
                actions.append(f"{sid} v{ver}: too many restarts this hour, leaving it stopped")

        for v in self.registry.list_versions(status="candidate"):
            if len(running) >= self.max_concurrent:
                break
            sid, ver = v["strategy_id"], v["version"]
            try:
                self.manager.start(sid, ver)
                running.append((sid, ver))
                actions.append(f"started paper trading {sid} v{ver}")
            except Exception as error:
                actions.append(f"could not start {sid} v{ver}: {error}")
                self.log("paper_error", f"Could not start paper trading: {error}", sid, ver)

        verdicts = {}
        for sid, ver in list(running):
            try:
                self.manager.write_gate(sid, ver)
                if self.clock() - self._last_eval.get((sid, ver), 0) >= self.evaluate_every:
                    verdict, paper = self.manager.evaluate(sid, ver)
                    self._last_eval[(sid, ver)] = self.clock()
                    verdicts[f"{sid} v{ver}"] = verdict.summary()
                    if verdict.status == "REJECTED":
                        actions.append(f"{sid} v{ver} REJECTED in paper: {verdict.summary()}")
            except Exception as error:
                actions.append(f"supervision error {sid} v{ver}: {error}")
        self._write_status(running, actions, verdicts)
        return actions

    def _write_status(self, running, actions, verdicts):
        state = {"ts": self.clock(), "running": [f"{s} v{v}" for s, v in running], "last_actions": actions[-10:], "verdicts": verdicts,
                 "max_concurrent": self.max_concurrent}
        try:
            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, STATUS_FILE)
        except Exception:
            pass
