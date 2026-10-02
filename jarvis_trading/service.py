"""
The thin layer the Jarvis engine talks to: chat phrases, status for the HUD,
and launching the research daemon in a separate process (the Freqtrade venv).
Standard library only; it never imports Freqtrade.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time

from . import freqtrade_adapter as A
from . import live_readiness as LR
from . import registry as R
from . import report as P
from . import strategies as S
from . import supervisor as SUP
from . import tracking as T

_LIVE_REQUEST = re.compile(r"(?i)\b(real money|live trad|go live|live mode|trade for real|with my money|use leverage|connect.*(binance|exchange|api key))")
_RUN = re.compile(r"(?i)\b(run|start|resume|kick off|begin)\b.*\b(trading research|strategy research|research daemon|trading lab|backtest|research cycle)")
_STOP = re.compile(r"(?i)\b(stop|pause|halt)\b.*\b(trading research|strategy research|research daemon|trading lab)")
_READY = re.compile(r"(?i)\b(live readiness|ready for live|ready to go live|path to live|when can we trade)")
_LEADER = re.compile(r"(?i)\b(leaderboard|best strateg|top strateg|trading results|how many strateg)")
_TRADING = re.compile(r"(?i)\b(trading (lab|research|status|strateg)|strategy lab|paper trad|trading report|trading dashboard|how('s| is) the trading)")

_lock = threading.Lock()
_cache = {"at": 0.0, "value": None}
_proc = {"p": None}

PID_FILE = os.path.join(A.LAB_DIR, "daemon.pid")
STOP_FLAG = os.path.join(A.LAB_DIR, "STOP_RESEARCH")
BATCH_STATUS = os.path.join(A.LAB_DIR, "batch_status.json")

REFUSAL = ("Live trading and real money are not enabled, sir, and I won't switch them on. The lab is research and "
           "paper-trading only: no exchange keys, no leverage. If a strategy ever survives every test and a long paper run, "
           "going live is a decision for you and a separately reviewed module, not something I do by myself.")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def research_running():
    pid = None
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except Exception:
        pass
    p = _proc["p"]
    return bool((p and p.poll() is None) or (pid and SUP.process_alive(pid)))


def summary(max_age=20):
    """Dashboard payload; cached briefly because the HUD polls it."""
    with _lock:
        if _cache["value"] is not None and time.time() - _cache["at"] < max_age:
            return _cache["value"]
        reg = R.Registry()
        track = T.build(reg)
        slim_rows = [{"id": r["strategy_id"], "version": r["version"], "template": r["template"], "status": r["status"],
                      "reason": (r["reason"] or "")[:240], "score": r["score"]} for r in track["rows"][-60:]]
        board = [{"id": r["strategy_id"], "template": r["template"], "status": r["status"], "score": r["score"],
                  "oos_sharpe": (r["out_of_sample"] or {}).get("sharpe"), "oos_dd": (r["out_of_sample"] or {}).get("max_drawdown"),
                  "oos_calmar": (r["out_of_sample"] or {}).get("calmar"), "fees_share": (r["out_of_sample"] or {}).get("fees_pct_of_gross_profit"),
                  "p": (r["confidence"] or {}).get("p_value"), "required_p": (r["confidence"] or {}).get("required_p"),
                  "stability_fail": (r["stability"] or {}).get("neighbour_fail_fraction")} for r in track["leaderboard"][:10]]
        readiness = LR.assess(reg, os.path.join(A.LAB_DIR, "paper"))
        value = {
            "counts": {s: sum(1 for r in track["rows"] if r["status"] == s) for s in sorted({r["status"] for r in track["rows"]})},
            "n_versions": track["n_versions"], "n_trials": track["n_trials"], "funnel": track["funnel"],
            "rejection_reasons": track["rejection_reasons"][:12], "by_family": track["by_family"], "leaderboard": board,
            "strategies": slim_rows, "decisions": reg.recent_decisions(15), "running": research_running(),
            "batch": _read_json(BATCH_STATUS), "paper": _read_json(SUP.STATUS_FILE),
            "mode": "research+paper only (live trading disabled)", "ranking_note": track["ranking_note"],
            "luck_benchmark_sharpe": track["luck_benchmark_sharpe"], "power": track["power"], "near_misses": track["near_misses"],
            "readiness": {"ready_for_review": readiness["ready_for_review"],
                          "items": [{"key": i.key, "ok": i.ok, "detail": i.detail, "kind": i.kind} for i in readiness["items"]]},
            "claim": ((P.snapshot(reg)["claim"] or {}).get("summary_3pct", []))[:3],
            "text": P.chat_summary(reg),
        }
        _cache.update(at=time.time(), value=value)
        return value


def start_research(templates=None, per_family=15):
    """Starts the research daemon (batch research + paper supervision) as a background process."""
    with _lock:
        if research_running():
            return False, "The research daemon is already running."
        if not os.path.exists(A.FT_VENV_PY):
            return False, "The Freqtrade environment isn't installed, so I can't run backtests."
        if os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
        os.makedirs(os.path.join(A.LAB_DIR, "logs"), exist_ok=True)
        log = open(os.path.join(A.LAB_DIR, "logs", "daemon.log"), "ab")
        flags = 0x00004000 | 0x00000200 if sys.platform == "win32" else 0  # below-normal priority, new process group
        _proc["p"] = subprocess.Popen([A.FT_VENV_PY, "-u", "-m", "jarvis_trading.daemon", "--per-family", str(per_family)],
                                      cwd=os.path.dirname(A.LAB_DIR), stdout=log, stderr=log, creationflags=flags)
        return True, "Research daemon started: it will keep testing new strategies in parallel and keep any survivor paper trading."


def stop_research():
    """Asks the daemon to stop after the strategies currently in flight finish."""
    os.makedirs(A.LAB_DIR, exist_ok=True)
    with open(STOP_FLAG, "w", encoding="utf-8") as f:
        f.write(str(time.time()))
    return "Stop requested. Strategies already running will finish; nothing new will start."


def _readiness_text():
    r = LR.assess(R.Registry(), os.path.join(A.LAB_DIR, "paper"))
    missing = [i for i in r["items"] if not i.ok]
    head = ("Every checklist item is satisfied, which only means a person may start reviewing a live module."
            if r["ready_for_review"] else f"Not ready for any live step. {len(missing)} of {len(r['items'])} checklist items are unmet:")
    return head + " " + "; ".join(i.detail for i in missing[:6])


def _leaderboard_text():
    s = summary()
    funnel = {f["stage"]: f["count"] for f in s["funnel"]}
    lines = [f"{s['n_versions']} strategy versions tested ({s['n_trials']} parameter sets tried). "
             f"{funnel.get('leakage_clean', 0)} passed leakage checks, {funnel.get('early_gates', 0)} passed the early gates, "
             f"{funnel.get('development', 0)} passed development gates, {funnel.get('candidate', 0)} became candidates, "
             f"{funnel.get('paper', 0)} reached paper trading."]
    if s["rejection_reasons"]:
        lines.append("Top rejection reasons: " + ", ".join(f"{k} x{v}" for k, v in s["rejection_reasons"][:4]) + ".")
    if s["leaderboard"]:
        top = s["leaderboard"][0]
        lines.append(f"Best by luck-adjusted risk score: {top['id']} ({top['template']}), {top['status']}, "
                     f"out-of-sample Sharpe {top['oos_sharpe']:.2f}, score {top['score']:+.2f} "
                     f"(0 or below means no better than the best of many random strategies).")
    lines.append(f"Statistical power: with {s['n_trials']} parameter sets tried, the held-out data can only confirm a true Sharpe of about "
                 f"{s['power']['min_detectable_sharpe']:.1f} or better; {len(s['near_misses'])} strategies failed only that significance test.")
    return " ".join(lines)


def chat_reply(command):
    """Returns the text Jarvis should say, or None when the command isn't about trading."""
    text = command.strip()
    if _READY.search(text):  # a question about readiness gets the honest checklist, not a refusal
        return _readiness_text()
    if _LIVE_REQUEST.search(text) and (re.search(r"(?i)trad|money|leverage|binance|exchange|bot|strateg", text) or len(text.split()) <= 4):
        return REFUSAL
    if _STOP.search(text):
        return stop_research()
    if _RUN.search(text):
        ok, message = start_research()
        return message
    if _LEADER.search(text):
        return _leaderboard_text()
    if _TRADING.search(text):
        s = summary()
        extra = " The research daemon is running." if s["running"] else ""
        return s["text"] + " " + _leaderboard_text() + extra
    return None
