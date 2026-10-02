"""
Promotion review: the gate between PAPER/CHALLENGER and a LIVE candidate.

This module only EVALUATES and RECORDS. It cannot put anything live. A review
that finds a paper strategy eligible produces a flag ("Strategy B is a
candidate to replace Strategy A") and the exact phrase a human must type to
approve it; approval itself lives in live/approval.py, which the research side
never imports.

The criteria are fixed in advance (frozen, fingerprinted), they sit on TOP of
the research gates (a strategy must already be a CANDIDATE that passed them),
and they are never tuned from results. If nothing qualifies, nothing trades.
"""

import json
from dataclasses import asdict, dataclass

from . import criteria as C
from . import strategies as S


@dataclass(frozen=True)
class PromotionCriteria:
    min_paper_days: int = 60
    min_paper_trades: int = 50
    max_paper_drawdown: float = 0.15
    # to REPLACE an existing live strategy the challenger must be clearly better, not marginally
    min_overlap_days: int = 60
    required_sharpe_margin: float = 0.30
    max_drawdown_excess: float = 0.03
    max_review_age_hours: int = 168

    def fingerprint(self):
        import hashlib
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


def approval_phrase(review_id, strategy_id, version):
    return f"PROMOTE {strategy_id} v{version} REVIEW {review_id}"


def _last(exps, kind):
    return exps[-1] if exps else None


def _live_baseline(registry, live):
    """Metrics the challenger must beat: the live strategy's own live record if it has one, else its paper twin."""
    sid, ver = live
    for kind in ("live_check", "paper_check"):
        exps = registry.experiments(sid, ver, kind)
        if exps:
            m = exps[-1]["metrics"].get("metrics") or {}
            return {"source": kind, "metrics": m, "strategy": f"{sid} v{ver}"}
    return None


def review(registry, strategy_id, version, criteria=None, research_criteria=None, record=True):
    """
    Evaluates a PAPER strategy version for promotion. Returns a dict:
    {eligible, items:[{key, ok, detail}], vs_live, review_id, phrase, criteria_fingerprint}.
    """
    crit = criteria or PromotionCriteria()
    rc = research_criteria or C.ResearchCriteria()
    items = []

    def add(key, ok, detail):
        items.append({"key": key, "ok": bool(ok), "detail": detail})

    status = registry.status(strategy_id, version)
    add("status_is_paper", status == "paper", f"status is '{status}' (must be paper/challenger)")

    verdicts = registry.experiments(strategy_id, version, "verdict")
    research_ok = any(v["passed"] for v in verdicts) and verdicts and verdicts[-1]["metrics"].get("criteria") == rc.fingerprint()
    add("passed_research_gates", research_ok,
        "passed every research gate under the current criteria" if research_ok else
        "no CANDIDATE verdict under the current research criteria (criteria changed or never passed)")

    # reproducibility: the stored code must regenerate byte-for-byte from its recorded template and parameters
    reproducible = False
    try:
        meta = registry.get(strategy_id)
        stored = registry.get_version(strategy_id, version)
        _, regenerated, _ = S.generate(meta["source"].split(":", 1)[1], stored["parameters"])
        reproducible = regenerated == stored["code"]
    except Exception:
        pass
    add("reproducible_code", reproducible, "stored code regenerates identically from template + parameters" if reproducible else
        "stored code does not regenerate identically from its template and parameters")

    checks = registry.experiments(strategy_id, version, "paper_check")
    last = _last(checks, "paper_check")
    pm = (last["metrics"].get("metrics") if last else None) or {}
    days, trades, dd = pm.get("days") or 0, pm.get("n_trades") or 0, pm.get("max_drawdown")
    pv = last["metrics"].get("verdict") if last else None
    add("paper_duration", days >= crit.min_paper_days, f"{days:.0f} paper days (need {crit.min_paper_days})")
    add("paper_trade_count", trades >= crit.min_paper_trades, f"{trades} paper trades (need {crit.min_paper_trades})")
    add("paper_matches_backtest", pv == "CONTINUE", f"latest paper verdict: {pv or 'none'} (must be CONTINUE)")
    add("paper_drawdown", dd is not None and dd <= crit.max_paper_drawdown,
        f"paper max drawdown {dd:.1%} (limit {crit.max_paper_drawdown:.0%})" if dd is not None else "no paper drawdown data yet")
    rejections = [c for c in registry.experiments(strategy_id, version, "paper_check")
                  if c["metrics"].get("verdict") == "REJECTED"]
    add("no_paper_breach", not rejections, "no paper check ever breached a kill condition" if not rejections else "a paper check breached a kill condition")

    vs_live = None
    live = registry.current_live()
    if live and live != (strategy_id, version):
        base = _live_baseline(registry, live)
        if base is None:
            add("beats_live_strategy", False, f"the live strategy {live[0]} v{live[1]} has no performance record to compare against")
        else:
            bm = base["metrics"]
            overlap = min(days, bm.get("days") or 0)
            cs, ls = pm.get("sharpe"), bm.get("sharpe")
            margin = None if cs is None or ls is None else cs - ls
            excess = None if dd is None or bm.get("max_drawdown") is None else dd - bm["max_drawdown"]
            vs_live = {"live": base["strategy"], "baseline_source": base["source"], "overlap_days": overlap,
                       "challenger_sharpe": cs, "live_sharpe": ls, "sharpe_margin": margin, "drawdown_excess": excess}
            add("comparison_window", overlap >= crit.min_overlap_days, f"{overlap:.0f} days of overlapping evidence (need {crit.min_overlap_days})")
            add("beats_live_sharpe", margin is not None and margin >= crit.required_sharpe_margin,
                f"Sharpe margin over the live strategy {margin if margin is None else round(margin, 2)} (need at least {crit.required_sharpe_margin})")
            add("drawdown_not_worse", excess is not None and excess <= crit.max_drawdown_excess,
                f"drawdown excess vs live {excess if excess is None else round(excess, 3)} (limit {crit.max_drawdown_excess})")

    eligible = all(i["ok"] for i in items)
    result = {"eligible": eligible, "items": items, "vs_live": vs_live, "strategy_id": strategy_id, "version": version,
              "criteria_fingerprint": crit.fingerprint(), "review_id": None, "phrase": None,
              "replaces": f"{live[0]} v{live[1]}" if live and live != (strategy_id, version) else None}
    if record:
        result["review_id"] = registry.record_promotion_event("review", strategy_id, version, eligible, result, "promotion_review")
        if eligible:
            result["phrase"] = approval_phrase(result["review_id"], strategy_id, version)
            registry.log_decision("promotion_flag", f"{strategy_id} v{version} is a candidate to " +
                                  (f"replace {result['replaces']}" if result["replaces"] else "become the first live strategy") +
                                  ". It has NOT been promoted; a human must approve.", strategy_id, version)
    return result


def review_if_changed(registry, strategy_id, version, max_age_hours=24, **kw):
    """Re-reviews only when the outcome changed or the last review is old, so the log is not flooded."""
    import calendar
    import time
    previous = [e for e in registry.promotion_events("review", strategy_id, limit=50) if e["version"] == version]
    fresh = review(registry, strategy_id, version, record=False, **kw)
    if previous:
        last = previous[0]
        same = last["details"].get("items") == fresh["items"] and bool(last["eligible"]) == fresh["eligible"]
        age = calendar.timegm(time.gmtime()) - calendar.timegm(time.strptime(last["ts"], "%Y-%m-%dT%H:%M:%S"))
        if same and age < max_age_hours * 3600:
            fresh["review_id"] = last["id"]
            fresh["phrase"] = approval_phrase(last["id"], strategy_id, version) if fresh["eligible"] else None
            return fresh
    return review(registry, strategy_id, version, **kw)


def challengers(registry, **kw):
    """Reviews every paper strategy; returns the list of results (eligible ones are flagged in the decision log)."""
    return [review_if_changed(registry, v["strategy_id"], v["version"], **kw) for v in registry.list_versions(status="paper")]
