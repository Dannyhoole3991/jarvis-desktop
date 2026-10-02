"""
One read-only view of the whole trading product for the HUD and for Jarvis to explain in plain English:
LIVE / PAPER / RESEARCH status, the live strategy and version, challengers, research progress, account, positions,
P&L, drawdown, risk state, last trade, last rejected trade and why, version and promotion history.

Reads only; changes nothing.
"""

import json
import os
import time

from .. import freqtrade_adapter as A
from .. import promotion as PR
from . import approval, paths


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _tail_jsonl(path, n):
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        return rows[-n:]
    except Exception:
        return []


def _age_seconds(iso):
    try:
        import calendar
        return calendar.timegm(time.gmtime()) - calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None


def trader_status(max_age=60):
    snap = _read_json(paths.STATUS_FILE)
    if not snap:
        return {"running": False, "state": "NOT RUNNING"}
    age = _age_seconds(snap.get("ts"))
    alive = age is not None and age <= max_age
    snap = dict(snap)
    snap["running"] = alive
    if not alive:
        snap["state"] = "NOT RUNNING (last seen " + (f"{age:.0f}s ago)" if age is not None else "unknown)")
    return snap


def build(registry, paper_status=None, research_status=None):
    live = registry.current_live()
    arming = approval.read_arming()
    arm_problems = approval.arming_problems(registry) if arming else ["live trading is not armed"]
    trader = trader_status()
    live_running = bool(trader.get("running")) and trader.get("mode") == "live"
    reviews = []
    for v in registry.list_versions(status="paper"):
        last = next((e for e in registry.promotion_events("review", v["strategy_id"], limit=20) if e["version"] == v["version"]), None)
        checks = registry.experiments(v["strategy_id"], v["version"], "paper_check")
        pm = (checks[-1]["metrics"].get("metrics") if checks else None) or {}
        reviews.append({"strategy_id": v["strategy_id"], "version": v["version"], "template": v["hypothesis"][:60],
                        "paper": pm, "paper_verdict": checks[-1]["metrics"].get("verdict") if checks else None,
                        "eligible": bool(last["eligible"]) if last else None, "review_id": last["id"] if last else None,
                        "blocking": [i["detail"] for i in (last["details"]["items"] if last else []) if not i["ok"]][:4],
                        "phrase": PR.approval_phrase(last["id"], v["strategy_id"], v["version"]) if last and last["eligible"] else None})
    paper = paper_status or _read_json(A.LAB_DIR + os.sep + "paper_status.json") or {}
    research = research_status or _read_json(A.LAB_DIR + os.sep + "batch_status.json") or {}
    return {
        "modes": {"live": live_running, "paper": bool(paper.get("running")), "research": bool(research.get("running"))},
        "live_strategy": None if not live else {"strategy_id": live[0], "version": live[1]},
        "arming": None if not arming else {k: arming[k] for k in ("armed_at", "expires_at", "capital_limit", "strategy_id", "version")},
        "arming_problems": arm_problems, "trader": trader, "challengers": reviews,
        "live_history": registry.live_history(20), "promotion_history": [
            {k: e[k] for k in ("id", "ts", "kind", "strategy_id", "version", "eligible", "actor")} for e in registry.promotion_events(limit=30)],
        "last_trades": _tail_jsonl(paths.TRADES_LOG, 5), "last_events": _tail_jsonl(paths.EVENTS_LOG, 10),
        "emergency_latched": os.path.exists(paths.EMERGENCY_LATCH), "halted": os.path.exists(paths.HALT_FILE),
        "paper": paper, "research": research,
    }


def explain(registry, view=None):
    """Plain-English answers to 'what are you doing?' -- never claims more than the data shows."""
    v = view or build(registry)
    lines = []
    live = v["live_strategy"]
    t = v["trader"]
    if live:
        lines.append(f"The approved live strategy is {live['strategy_id']} v{live['version']}.")
        if v["modes"]["live"]:
            lines.append(f"The live trader is running in state {t.get('state')}.")
        elif v["arming"]:
            lines.append("Live trading is armed but the trader is not running." if not v["arming_problems"] else
                         "Live trading is armed but cannot run: " + "; ".join(v["arming_problems"]))
        else:
            lines.append("Live trading is DISABLED: no real money is at risk. It needs your explicit arming.")
    else:
        lines.append("No strategy has been approved to go live, so no real-money trading is possible, and none is happening.")
    if v["emergency_latched"]:
        lines.append("An emergency stop is latched; a human must clear it before anything can trade.")
    if v["halted"]:
        lines.append("The Risk Engine has halted new entries (drawdown or integrity); a human must clear the halt.")
    if t.get("running"):
        if t.get("last_veto"):
            lines.append(f"The last vetoed entry was {t['last_veto'].get('pair')} at {t['last_veto'].get('ts')}: {t['last_veto'].get('reason')}")
        if (t.get("entries_today") or 0) == 0 and t.get("mode") == "live":
            gate = t.get("gate") or {}
            lines.append("No trade was taken today. " + ("Entries are currently allowed, but no qualifying signal has appeared." if gate.get("allow_entries")
                                                          else f"Entries are currently blocked by the Risk Engine: {gate.get('reason')}."))
    n_chal = len(v["challengers"])
    if n_chal:
        best = max(v["challengers"], key=lambda c: (c["paper"].get("sharpe") or -9))
        lines.append(f"{n_chal} challenger strateg{'y is' if n_chal == 1 else 'ies are'} paper trading; the strongest so far is {best['strategy_id']} v{best['version']}.")
        for c in v["challengers"]:
            if c["eligible"]:
                lines.append(f"{c['strategy_id']} v{c['version']} is a candidate to " + (f"replace {live['strategy_id']} v{live['version']}" if live else "become the first live strategy") +
                             ". It has not been promoted; you must approve it.")
            elif c["blocking"]:
                lines.append(f"{c['strategy_id']} v{c['version']} has not yet passed the promotion requirements: {c['blocking'][0]}.")
    else:
        lines.append("No challenger strategy is paper trading yet; nothing has survived the research gates.")
    r = v["research"]
    if r.get("running"):
        lines.append(f"The research lab is running: {r.get('done', 0)} of {r.get('total', 0)} strategies tested in the current batch.")
    return lines
