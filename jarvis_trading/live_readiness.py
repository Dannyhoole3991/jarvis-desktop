"""
The path from paper trading to a tiny real-money deployment, as an executable checklist.

`assess()` only REPORTS. It cannot arm anything: no live venue exists
(execution.LiveVenue refuses construction) and the Risk Engine rejects mode
"live" in all circumstances. Passing every item below means a human may START
a separate review of a live module -- it is never an automatic switch.

Quantitative items are checked from the registry; human items are checked by
the presence of files only a person creates. All thresholds are fixed here,
in advance, and not tunable from results.
"""

import json
import os
from dataclasses import dataclass

from . import freqtrade_adapter as A
from . import metrics as M

MIN_PAPER_DAYS = 60
MIN_PAPER_TRADES = 50
MAX_PAPER_DRAWDOWN = 0.15
SIGNOFF_FILE = os.path.join(A.LAB_DIR, "LIVE_SIGNOFF.json")
LIVE_MODULE_REVIEW_FILE = os.path.join(A.LAB_DIR, "LIVE_MODULE_REVIEWED.json")


@dataclass
class Item:
    key: str
    ok: bool
    detail: str
    kind: str = "automatic"   # automatic | human


def _paper_versions(registry):
    return registry.list_versions(status="paper")


def assess(registry, paper_dir=None):
    """Returns {"ready_for_review": bool, "items": [Item...], "best_paper": {...}|None}. Never changes anything."""
    items = []
    in_paper = _paper_versions(registry)
    items.append(Item("strategy_in_paper", bool(in_paper),
                      f"{len(in_paper)} strategy version(s) in paper trading" if in_paper else
                      "No strategy has survived every research gate, so nothing is paper trading yet"))

    best = None
    for v in in_paper:
        checks = registry.experiments(v["strategy_id"], v["version"], "paper_check")
        if not checks:
            continue
        m = checks[-1]["metrics"].get("metrics", {})
        if best is None or (m.get("days") or 0) > (best["metrics"].get("days") or 0):
            best = {"strategy_id": v["strategy_id"], "version": v["version"], "metrics": m,
                    "verdict": checks[-1]["metrics"].get("verdict")}

    days = (best or {}).get("metrics", {}).get("days") or 0
    trades = (best or {}).get("metrics", {}).get("n_trades") or 0
    dd = (best or {}).get("metrics", {}).get("max_drawdown")
    items.append(Item("paper_duration", days >= MIN_PAPER_DAYS, f"{days:.0f} paper days (need at least {MIN_PAPER_DAYS})"))
    items.append(Item("paper_trade_count", trades >= MIN_PAPER_TRADES, f"{trades} paper trades (need at least {MIN_PAPER_TRADES})"))
    items.append(Item("paper_matches_backtest", bool(best) and best["verdict"] == "CONTINUE",
                      f"latest paper verdict: {best['verdict'] if best else 'none'} (must be CONTINUE: results consistent with the backtest)"))
    items.append(Item("paper_drawdown", dd is not None and dd <= MAX_PAPER_DRAWDOWN,
                      f"paper max drawdown {dd:.1%}" if dd is not None else "no paper drawdown data yet"))

    halted = False
    if paper_dir and os.path.isdir(paper_dir):
        for name in os.listdir(paper_dir):
            state = os.path.join(paper_dir, name, "risk_state.json")
            if os.path.exists(state):
                with open(state, "r", encoding="utf-8") as f:
                    halted = halted or bool(json.load(f).get("halted"))
    items.append(Item("no_risk_halts", not halted, "Risk Engine has not halted any paper run" if not halted else "A paper run hit a Risk Engine halt"))

    items.append(Item("human_signoff", os.path.exists(SIGNOFF_FILE),
                      "Danny's written sign-off file exists" if os.path.exists(SIGNOFF_FILE) else
                      f"Missing {os.path.basename(SIGNOFF_FILE)}: a person must decide to proceed, naming the strategy and the capital cap", "human"))
    items.append(Item("live_module_reviewed", os.path.exists(LIVE_MODULE_REVIEW_FILE),
                      "A separately written live execution module has been independently reviewed" if os.path.exists(LIVE_MODULE_REVIEW_FILE) else
                      "No live execution module exists, and none has been reviewed. It must be written and reviewed separately", "human"))
    items.append(Item("exchange_key_restrictions", False,
                      "Human step, not verifiable here: a NEW exchange API key with trading permission only (withdrawals disabled), "
                      "IP-restricted, held outside this repository", "human"))
    items.append(Item("capital_cap", False,
                      "Human step: decide a capital amount you can afford to lose completely, funded separately", "human"))
    return {"ready_for_review": all(i.ok for i in items), "items": items, "best_paper": best,
            "note": "Passing all items only permits a human to START a live-module review. Nothing here enables trading."}
