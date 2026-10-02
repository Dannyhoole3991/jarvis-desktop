"""
The complete path from research to real money as an executable checklist. `assess()` only REPORTS; it cannot
approve, arm or enable anything.

Automatic items come from the registry and the live files; human items are things only a person can do or decide.
Thresholds are fixed in advance (see promotion.PromotionCriteria) and are not tunable from results.
"""

import os
import time
from dataclasses import dataclass

from .. import freqtrade_adapter as A
from .. import promotion as PR
from . import approval, config, creds, paths

REHEARSAL_OK_FILE_NAME = "rehearsal_ok.json"
LIVE_MODULE_REVIEW_FILE = os.path.join(A.LAB_DIR, "LIVE_MODULE_REVIEWED.json")
REHEARSAL_MAX_AGE_DAYS = 30


@dataclass
class Item:
    key: str
    ok: bool
    detail: str
    kind: str = "automatic"   # automatic | human


def rehearsal_ok_path():
    return os.path.join(paths.LIVE_DIR, REHEARSAL_OK_FILE_NAME)


def _best_paper(registry):
    best = None
    pool = registry.list_versions(status="paper")
    live = registry.current_live()
    if live:
        pool = pool + [{"strategy_id": live[0], "version": live[1]}]
    for v in pool:
        checks = registry.experiments(v["strategy_id"], v["version"], "paper_check")
        if not checks:
            continue
        m = checks[-1]["metrics"].get("metrics", {})
        if best is None or (m.get("days") or 0) > (best["metrics"].get("days") or 0):
            best = {"strategy_id": v["strategy_id"], "version": v["version"], "metrics": m, "verdict": checks[-1]["metrics"].get("verdict")}
    return best


def assess(registry, paper_dir=None):
    crit = PR.PromotionCriteria()
    items = []
    live = registry.current_live()
    in_paper = registry.list_versions(status="paper")
    items.append(Item("strategy_in_paper", bool(in_paper) or bool(live),
                      f"{len(in_paper)} strategy version(s) in paper trading" + (f"; {live[0]} v{live[1]} is the approved live strategy" if live else "") if (in_paper or live)
                      else "No strategy has survived every research gate, so nothing is paper trading yet"))
    best = _best_paper(registry)
    m = (best or {}).get("metrics", {})
    days, trades, dd = m.get("days") or 0, m.get("n_trades") or 0, m.get("max_drawdown")
    items.append(Item("paper_duration", days >= crit.min_paper_days, f"{days:.0f} paper days (need at least {crit.min_paper_days})"))
    items.append(Item("paper_trade_count", trades >= crit.min_paper_trades, f"{trades} paper trades (need at least {crit.min_paper_trades})"))
    items.append(Item("paper_matches_backtest", bool(best) and best["verdict"] in ("CONTINUE", "n/a"),
                      f"latest paper verdict: {best['verdict'] if best else 'none'} (must be CONTINUE: results consistent with the backtest)"))
    items.append(Item("paper_drawdown", dd is not None and dd <= crit.max_paper_drawdown,
                      f"paper max drawdown {dd:.1%}" if dd is not None else "no paper drawdown data yet"))

    halted = False
    if paper_dir and os.path.isdir(paper_dir):
        import json
        for name in os.listdir(paper_dir):
            state = os.path.join(paper_dir, name, "risk_state.json")
            if os.path.exists(state):
                with open(state, "r", encoding="utf-8") as f:
                    halted = halted or bool(json.load(f).get("halted"))
    items.append(Item("no_risk_halts", not halted, "Risk Engine has not halted any paper run" if not halted else "A paper run hit a Risk Engine halt"))
    items.append(Item("promotion_approved", bool(live), f"{live[0]} v{live[1]} has been approved as the live strategy" if live else
                      "No strategy has been approved as LIVE (needs an eligible promotion review and your typed approval)"))

    ok_file = rehearsal_ok_path()
    fresh = os.path.exists(ok_file) and (time.time() - os.path.getmtime(ok_file)) < REHEARSAL_MAX_AGE_DAYS * 86400
    items.append(Item("rehearsal_passed", fresh, "The full live pipeline passed a dry-run rehearsal (incl. emergency stop) in the last 30 days" if fresh else
                      "The live pipeline has not passed a recent rehearsal: run `python -m jarvis_trading.live.cli rehearse --test-emergency`"))

    try:
        have = creds.have_credentials()
    except Exception:
        have = False
    items.append(Item("credentials_stored", have, "Exchange API credentials are stored (encrypted, outside the project)" if have else
                      "No exchange credentials: create a spot-only, withdrawals-DISABLED, IP-restricted key and run `python -m jarvis_trading.live.setup_credentials`", "human"))
    try:
        capital = config.build_limits(config.load_settings()).capital_limit
    except Exception:
        capital = 0.0
    items.append(Item("capital_configured", capital > 0, f"Capital limit configured: {capital:g} USDT" if capital > 0 else
                      "No capital limit is configured (live_config.json); with none, nothing can trade", "human"))
    review_ok = os.path.exists(LIVE_MODULE_REVIEW_FILE)
    items.append(Item("independent_review", review_ok, "The live module has had an independent code review" if review_ok else
                      "Recommended: have someone else independently review jarvis_trading/live before real money is involved", "human"))
    armed = approval.read_arming() is not None and not approval.arming_problems(registry)
    items.append(Item("armed", armed, "Live trading is explicitly ARMED" if armed else "Live trading is not armed (your explicit typed step)", "human"))
    blockers = [i for i in items if not i.ok]
    return {"ready_for_review": not blockers, "ready": not blockers, "items": items, "best_paper": best, "blockers": blockers,
            "note": "This checklist only reports. Real-money trading starts only if you approve a promotion, arm it with a typed phrase, and start the trader."}
