"""
The thin layer the Jarvis engine talks to: chat phrases, status for the HUD,
and launching research in a separate process (the Freqtrade venv). Standard
library only; it never imports Freqtrade.
"""

import json
import os
import re
import subprocess
import threading

from . import freqtrade_adapter as A
from . import registry as R
from . import report as P
from . import strategies as S

_LIVE_REQUEST = re.compile(r"(?i)\b(real money|live trad|go live|live mode|trade for real|with my money|use leverage|connect.*(binance|exchange|api key))")
_TRADING = re.compile(r"(?i)\b(trading (lab|research|status|strateg)|strategy lab|paper trad|backtest|trading report|trading dashboard|how('s| is) the trading)")
_RUN = re.compile(r"(?i)\b(run|start|do)\b.*\b(trading research|strategy research|backtest|research cycle)")

_lock = threading.Lock()
_running = {"proc": None}

REFUSAL = ("Live trading and real money are not enabled, sir, and I won't switch them on. The lab is research and "
           "paper-trading only: no exchange keys, no leverage. If a strategy ever survives every test, that is a decision for you, "
           "not something I do by myself.")


def summary():
    reg = R.Registry()
    snap = P.snapshot(reg)
    slim = [{"id": r["strategy_id"], "version": r["version"], "template": r["source"].split(":")[-1], "status": r["status"],
             "reason": r["reason"], "parameters": r["parameters"]} for r in snap["rows"]]
    return {"counts": snap["counts"], "strategies": slim, "decisions": snap["decisions"][:15],
            "running": research_running(), "mode": "research+paper only (live trading disabled)",
            "claim": (snap["claim"] or {}).get("summary_3pct", [])[:3], "text": P.chat_summary(reg)}


def research_running():
    proc = _running["proc"]
    return bool(proc and proc.poll() is None)


def start_research(templates=None):
    """Runs the lab in the Freqtrade venv as a background process. Returns (started, message)."""
    with _lock:
        if research_running():
            return False, "A research run is already in progress."
        if not os.path.exists(A.FT_VENV_PY):
            return False, "The Freqtrade environment isn't installed, so I can't run backtests."
        templates = [t for t in (templates or S.TEMPLATES) if t in S.TEMPLATES]
        log = open(os.path.join(A.LAB_DIR, "research.log"), "ab")
        _running["proc"] = subprocess.Popen(
            [A.FT_VENV_PY, "-m", "jarvis_trading.run_lab", "--templates", *templates], cwd=os.path.dirname(A.LAB_DIR),
            stdout=log, stderr=log)
        return True, f"Research started on {len(templates)} template(s)."


def chat_reply(command):
    """Returns the text Jarvis should say, or None when the command isn't about trading."""
    text = command.strip()
    if _LIVE_REQUEST.search(text) and re.search(r"(?i)trad|money|leverage|binance|exchange|bot|strateg", text):
        return REFUSAL
    if _RUN.search(text):
        ok, message = start_research()
        return (message + " Results land in the registry; ask me for the trading status when it's done.") if ok else message
    if _TRADING.search(text):
        s = summary()
        extra = " A research run is in progress." if s["running"] else ""
        return s["text"] + extra
    return None
