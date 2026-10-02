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
from .live import approval as LAP
from .live import paths as LPATHS
from .live import status as LSTATUS
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

_EMERGENCY = re.compile(r"(?i)\b(emergency stop|kill switch|stop all trading|stop trading now|panic stop)\b")
_DISARM = re.compile(r"(?i)\b(disarm|disable live trading|turn off live trading|switch off live trading)\b")
_START_LIVE = re.compile(r"(?i)\b(start|run|launch)\b.*\b(live trader|live trading engine)\b")
_EXPLAIN = re.compile(r"(?i)(what (strategy|are you running|is live)|which strategy|have you traded|did you trade|any trades?|why (no|didn.t|haven.t).*trade|challenger|promotion status|trading status|live status|how is the live)")
_TYPED = re.compile(r"^\s*(PROMOTE\s+S-\d+\s+V\d+\s+REVIEW\s+\d+|ENABLE LIVE TRADING\s+S-\d+\s+V\d+\s+WITH\s+[\d.]+\s+USDT|ROLLBACK TO\s+S-\d+\s+V\d+|RETIRE LIVE STRATEGY|CLEAR EMERGENCY STOP|CLEAR RISK HALT)\s*$", re.I)

_lock = threading.Lock()
_cache = {"at": 0.0, "value": None}
_proc = {"p": None}

PID_FILE = os.path.join(A.LAB_DIR, "daemon.pid")
STOP_FLAG = os.path.join(A.LAB_DIR, "STOP_RESEARCH")
BATCH_STATUS = os.path.join(A.LAB_DIR, "batch_status.json")

REFUSAL = ("Live trading is built but DISABLED, sir, and I won't switch it on from a casual request. It takes two explicit steps from you: "
           "approve a strategy that has earned promotion (you type its confirmation phrase), then arm live trading with a stated capital "
           "(you type a second phrase). Ask 'trading status' to see exactly what is blocking it now.")


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def product(max_age=10):
    """Everything the Trading section shows about the live / paper / research system (read-only)."""
    reg = R.Registry()
    view = LSTATUS.build(reg)
    view["explain"] = LSTATUS.explain(reg, view)
    view["research_summary"] = summary()
    return view


def emergency_stop_request():
    LPATHS.ensure_dirs()
    with open(LPATHS.EMERGENCY_FILE, "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return "Emergency stop requested. A running live trader closes everything within seconds and latches until you clear it."


def disarm_request():
    was = LAP.disarm(R.Registry(), "disarmed from Jarvis")
    return "Live trading is disarmed." if was else "Live trading was not armed."


def start_live_trader():
    """Launches the live trader process. It refuses to do anything unless armed and every precondition holds."""
    if not os.path.exists(A.FT_VENV_PY):
        return False, "The Freqtrade environment isn't installed."
    flags = 0x00000200 if sys.platform == "win32" else 0
    os.makedirs(LPATHS.LOG_DIR, exist_ok=True)
    log = open(os.path.join(LPATHS.LOG_DIR, "trader_cli.log"), "ab")
    subprocess.Popen([A.FT_VENV_PY, "-u", "-m", "jarvis_trading.live.cli", "start"], cwd=os.path.dirname(A.LAB_DIR), stdout=log, stderr=log,
                     creationflags=flags)
    return True, "Starting the live trader. It will refuse to run unless live trading is armed and every safety check passes; ask for the trading status in a moment."


def typed_control(text):
    """Executes a typed approval/arming/clear phrase. The phrase must match exactly what the system displayed."""
    reg = R.Registry()
    t = text.strip()
    try:
        m = re.match(r"(?i)PROMOTE\s+S-\d+\s+V\d+\s+REVIEW\s+(\d+)", t)
        if m:
            r = LAP.approve_promotion(reg, int(m.group(1)), t)
            return f"Approved. {r['strategy_id']} v{r['version']} is now the LIVE strategy (frozen). {r['note']}"
        if re.match(r"(?i)ENABLE LIVE TRADING", t):
            r = LAP.arm(reg, t)
            return (f"Live trading is ARMED for {r['strategy_id']} v{r['version']} with {r['capital_limit']:g} USDT until {r['expires_at']}. "
                    "Say 'start the live trader' to begin; it will still only trade when a valid signal passes the Risk Engine.")
        m = re.match(r"(?i)ROLLBACK TO\s+(S-\d+)\s+V(\d+)", t)
        if m:
            r = LAP.rollback(reg, m.group(1).upper(), int(m.group(2)), t)
            return f"Rolled back to {r['strategy_id']} v{r['version']}. Live trading is disarmed; arm it again when ready."
        if re.match(r"(?i)RETIRE LIVE STRATEGY", t):
            LAP.retire_live_strategy(reg, t)
            return "The live strategy is retired and live trading is disarmed."
        if re.match(r"(?i)CLEAR EMERGENCY STOP", t):
            LAP.clear_emergency(reg, t)
            return "Emergency stop cleared. Live trading is disarmed and must be armed again."
        if re.match(r"(?i)CLEAR RISK HALT", t):
            LAP.clear_halt(reg, t)
            return "Risk halt cleared."
    except LAP.ApprovalError as error:
        return str(error)
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
                  "stability_fail": (r["stability"] or {}).get("neighbour_fail_fraction"),
                  "vs_passive": r.get("vs_passive_sharpe")} for r in track["leaderboard"][:10]]
        readiness = LR.assess(reg, os.path.join(A.LAB_DIR, "paper"))
        value = {
            "counts": {s: sum(1 for r in track["rows"] if r["status"] == s) for s in sorted({r["status"] for r in track["rows"]})},
            "n_versions": track["n_versions"], "n_trials": track["n_trials"], "funnel": track["funnel"],
            "rejection_reasons": track["rejection_reasons"][:12], "by_family": track["by_family"], "leaderboard": board,
            "strategies": slim_rows, "decisions": reg.recent_decisions(15), "running": research_running(),
            "batch": _read_json(BATCH_STATUS), "paper": _read_json(SUP.STATUS_FILE),
            "mode": "research+paper only (live trading disabled)", "ranking_note": track["ranking_note"],
            "luck_benchmark_sharpe": track["luck_benchmark_sharpe"], "power": track["power"], "near_misses": track["near_misses"], "benchmark": track["benchmark"],
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
    if _TYPED.match(text):
        return typed_control(text)
    if _EMERGENCY.search(text):
        return emergency_stop_request()
    if _DISARM.search(text) and re.search(r"(?i)live|trad", text):
        return disarm_request()
    if _START_LIVE.search(text):
        return start_live_trader()[1]
    if _EXPLAIN.search(text):
        return " ".join(LSTATUS.explain(R.Registry()))
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
