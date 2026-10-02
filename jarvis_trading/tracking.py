"""
Experiment tracking: everything Danny wants to see about the research effort,
derived read-only from the append-only registry.

  - how many strategies were tested, and how many survived each gate (funnel)
  - why strategies were rejected (by reason and by family)
  - best candidates ranked by RISK-ADJUSTED, luck-adjusted metrics -- never raw profit
  - development vs out-of-sample vs paper performance side by side
  - drawdown, fees/slippage share, parameter stability, statistical confidence

The ranking score is the out-of-sample Sharpe minus the Sharpe that pure luck
would be expected to produce as the best of `n_trials` tries over the same
number of days (Bailey / Lopez de Prado). A score near or below zero means the
strategy is indistinguishable from the best of many random ones.
"""

import json
import os

from . import criteria as C
from . import stats as ST

BENCHMARK_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trading_lab", "reports", "benchmark.json")


def load_benchmark(path=None):
    """Passive buy-and-hold results (see benchmark.py), or None if not computed yet."""
    try:
        with open(path or BENCHMARK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

# Funnel stages in pipeline order. "reached" counts versions whose evidence shows they got that far.
STAGES = [
    ("registered", "Strategies registered"),
    ("leakage_clean", "Passed leakage checks (static + dynamic)"),
    ("backtested", "Backtested (training + validation)"),
    ("early_gates", "Passed early gates (profitable in training and validation, distinct, enough trades, plausible)"),
    ("development", "Passed development gates (parameter stability, regimes)"),
    ("candidate", "Passed held-out test + statistics (CANDIDATE)"),
    ("paper", "Reached paper trading"),
]


def _last(exps, kind):
    items = exps.get(kind) or []
    return items[-1] if items else None


def _row(version, exps, n_trials, criteria):
    sid, ver = version["strategy_id"], version["version"]
    template = version["source"].split(":", 1)[-1] if "source" in version else ""
    lint, look = _last(exps, "static_lint"), _last(exps, "lookahead")
    train, val = _last(exps, "backtest_training"), _last(exps, "backtest_validation")
    test, mc = _last(exps, "backtest_test"), _last(exps, "monte_carlo")
    rob, verdict = _last(exps, "robustness"), _last(exps, "verdict")
    paper = _last(exps, "paper_check")
    in_paper = version["status"] in ("paper", "retired") or paper is not None

    leakage_clean = bool(look and look["metrics"].get("clean") is True and (lint is None or lint["passed"]))
    reasons = (verdict or {}).get("metrics", {}).get("reasons", []) if verdict else []
    codes = [r[0] for r in reasons]

    reached = {
        "registered": True,
        "leakage_clean": leakage_clean,
        "backtested": bool(train and val),
        "early_gates": rob is not None,
        "development": test is not None,
        "candidate": version["status"] in ("candidate", "paper", "retired") or in_paper,
        "paper": in_paper,
    }

    oos = test["metrics"] if test else None
    dev = None
    if train and val:
        n = (train["metrics"].get("n_trades", 0) or 0) + (val["metrics"].get("n_trades", 0) or 0)
        dev = {"n_trades": n, "train_return": train["metrics"].get("total_return"), "validation_return": val["metrics"].get("total_return"),
               "max_drawdown": max(train["metrics"].get("max_drawdown") or 0, val["metrics"].get("max_drawdown") or 0),
               "sharpe": train["metrics"].get("sharpe")}
    score = None
    if oos and oos.get("sharpe") is not None:
        luck = ST.noise_sharpe_benchmark(max(2, n_trials), max(2, int(oos.get("days") or 2)))
        score = oos["sharpe"] - luck
    mcm = mc["metrics"] if mc else {}
    return {
        "strategy_id": sid, "version": ver, "template": template, "status": version["status"], "reason": version.get("reason"),
        "parameters": version.get("parameters"), "reached": reached, "codes": codes,
        "development": dev,
        "out_of_sample": None if not oos else {k: oos.get(k) for k in (
            "n_trades", "total_return", "annualised_return", "avg_daily_return", "sharpe", "sortino", "calmar", "profit_factor",
            "max_drawdown", "win_rate", "fees_total", "fees_pct_of_gross_profit", "turnover", "longest_losing_streak", "days")},
        "paper": None if not paper else paper["metrics"].get("metrics"),
        "paper_verdict": None if not paper else paper["metrics"].get("verdict"),
        "stability": None if not rob else {"neighbour_fail_fraction": rob["metrics"].get("neighbour_fail_fraction"),
                                          "neighbours_tested": rob["metrics"].get("neighbours_tested")},
        "confidence": None if not mcm else {"p_value": mcm.get("p_value"), "required_p": criteria.alpha / max(1, n_trials),
                                            "ruin_probability": mcm.get("ruin_probability")},
        "score": score,
    }


def build(registry, criteria=None, benchmark_path=None):
    criteria = criteria or C.ResearchCriteria()
    bench = load_benchmark(benchmark_path)
    passive = ((bench or {}).get("results", {}).get("test", {}).get("universe_equal_weight") or {})
    versions = registry.list_versions()
    meta = {}
    grouped = registry.all_experiments_grouped()
    n_trials = max(1, registry.count_trials())
    rows = []
    for v in versions:
        if v["strategy_id"] not in meta:
            meta[v["strategy_id"]] = registry.get(v["strategy_id"])
        full = dict(v)
        full["source"] = meta[v["strategy_id"]]["source"]
        pv = registry.get_version(v["strategy_id"], v["version"])
        full["parameters"] = pv["parameters"] if pv else None
        row = _row(full, grouped.get((v["strategy_id"], v["version"]), {}), n_trials, criteria)
        oos_sharpe = (row["out_of_sample"] or {}).get("sharpe")
        row["vs_passive_sharpe"] = None if oos_sharpe is None or passive.get("sharpe") is None else oos_sharpe - passive["sharpe"]
        rows.append(row)

    funnel = [{"stage": key, "label": label, "count": sum(1 for r in rows if r["reached"][key])} for key, label in STAGES]
    reasons, by_family = {}, {}
    for r in rows:
        fam = by_family.setdefault(r["template"], {"tested": 0, "candidate": 0, "paper": 0, "rejected": 0, "best_score": None})
        fam["tested"] += 1
        fam["candidate"] += r["status"] == "candidate"
        fam["paper"] += r["status"] == "paper"
        fam["rejected"] += r["status"] == "rejected"
        if r["score"] is not None and (fam["best_score"] is None or r["score"] > fam["best_score"]):
            fam["best_score"] = r["score"]
        if r["status"] == "rejected":
            for code in (r["codes"] or ["UNSPECIFIED"]):
                reasons[code] = reasons.get(code, 0) + 1
    near_misses = [r for r in rows if r["status"] == "rejected" and r["codes"] and set(r["codes"]) <= {"NOT_SIGNIFICANT"}]
    oos_days = next((r["out_of_sample"]["days"] for r in rows if r["out_of_sample"] and r["out_of_sample"].get("days")), 820)
    leaderboard = sorted((r for r in rows if r["score"] is not None),
                         key=lambda r: (r["score"], (r["out_of_sample"] or {}).get("calmar") or 0), reverse=True)
    return {
        "n_versions": len(rows), "n_trials": n_trials, "funnel": funnel,
        "rejection_reasons": sorted(reasons.items(), key=lambda kv: -kv[1]),
        "by_family": by_family, "leaderboard": leaderboard[:25], "rows": rows, "benchmark": passive or None,
        "near_misses": [{"strategy_id": r["strategy_id"], "template": r["template"], "oos_sharpe": (r["out_of_sample"] or {}).get("sharpe"),
                         "p_value": (r["confidence"] or {}).get("p_value")} for r in near_misses[:20]],
        "power": {"oos_days": oos_days, "min_detectable_sharpe": ST.min_detectable_sharpe(n_trials, oos_days, criteria.alpha),
                  "note": "With this many parameter sets tried and this much held-out data, a strategy needs a true Sharpe of at least this "
                          "to be reliably confirmed. Weaker genuine edges cannot be told apart from luck on historical data alone; "
                          "only fresh forward (paper) data can test them. Near-misses are NOT candidates and are not paper traded."},
        "luck_benchmark_sharpe": ST.noise_sharpe_benchmark(max(2, n_trials), 365),
        "ranking_note": "Ranked by out-of-sample Sharpe minus the Sharpe luck alone would produce as the best of "
                        f"{n_trials} tries; not by raw profit. Only CANDIDATE or PAPER status means a strategy passed every gate.",
    }
