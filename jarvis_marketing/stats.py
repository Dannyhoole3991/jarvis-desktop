"""
Experiment verdicts. The objective is profitable sales: views and likes never pick a winner.

Verdicts:
  insufficient_data  the data isn't final yet (inside the reporting delay / attribution window),
                     or a needed metric is missing or stale
  inconclusive       the test is over but a variant stayed below the minimum sessions, or the
                     difference isn't statistically clear
  winner             one variant converts significantly better AND makes positive contribution profit
  no_winner          all variants clearly fail the success criteria (e.g. no profitable variant)
Per variant, `stop` is recommended when the evidence says it can't reach the goal
(many sessions and no orders, or upper confidence bound of CVR below break-even).
"""

import math


def wilson(successes, trials, z=1.96):
    """95% Wilson interval for a rate. (0, 1) when there are no trials."""
    if not trials:
        return (0.0, 1.0)
    p = successes / trials
    d = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / d
    half = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def two_proportion_p(s1, n1, s2, n2):
    """Two-sided p-value that two rates differ (pooled z-test). 1.0 when it can't be computed."""
    if not n1 or not n2:
        return 1.0
    p = (s1 + s2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = abs(s1 / n1 - s2 / n2) / se
    return math.erfc(z / math.sqrt(2))


def evaluate(variants, criteria, now, ended_at=None):
    """
    variants: [{"id", "sessions", "orders", "revenue", "contribution", "spend", "stale": bool, "missing": [metric,...]}]
              sessions/orders may be None (= not available): that is never treated as 0.
    criteria: {"min_sessions_per_variant", "stop_sessions_without_order", "significance", "data_delay_hours",
               "attribution_window_days", "break_even_cvr" (optional)}
    """
    reasons, per = [], []
    final_after = (ended_at or now) + 3600 * criteria["data_delay_hours"] + 86400 * criteria["attribution_window_days"]
    data_final = ended_at is not None and now >= final_after
    for v in variants:
        row = {"id": v["id"], "stop": False, "why": []}
        sessions, orders = v.get("sessions"), v.get("orders")
        if sessions is None or orders is None:
            row["why"].append("sessions or orders not available")
        else:
            row["cvr"] = orders / sessions if sessions else None
            row["cvr_interval"] = wilson(orders, sessions)
            if orders == 0 and sessions >= criteria["stop_sessions_without_order"]:
                row["stop"] = True
                row["why"].append(f"{sessions} sessions and no orders")
            be = criteria.get("break_even_cvr")
            if be and sessions >= criteria["min_sessions_per_variant"] and row["cvr_interval"][1] < be:
                row["stop"] = True
                row["why"].append("even the optimistic conversion rate is below break-even")
        if v.get("stale"):
            row["why"].append("data is stale")
        if v.get("missing"):
            row["why"].append("missing: " + ", ".join(v["missing"]))
        per.append(row)

    def result(verdict, why, winner=None):
        return {"verdict": verdict, "winner": winner, "reasons": why, "variants": per, "data_final": data_final,
                "note": "Views and likes are diagnostics only; verdicts use sessions, orders and contribution profit."}
    if any(v.get("stale") for v in variants):
        return result("insufficient_data", ["Some readings are stale, so no verdict."])
    usable = [v for v in variants if v.get("sessions") is not None and v.get("orders") is not None]
    if len(usable) < len(variants):
        return result("insufficient_data", ["Sessions/orders aren't available for every variant (never guessed)."])
    thin = [v["id"] for v in usable if v["sessions"] < criteria["min_sessions_per_variant"]]
    stops = [p["id"] for p in per if p["stop"]]
    if not data_final:
        reasons.append("The attribution window and reporting delay haven't passed yet.")
        if thin:
            reasons.append(f"Variants {thin} are below {criteria['min_sessions_per_variant']} sessions so far.")
        return result("insufficient_data", reasons + ([f"Recommend stopping {stops} already."] if stops else []))
    if thin:
        # The test is over and the evidence is still thin: say so plainly. Never a winner.
        return result("inconclusive", [f"The test ended with variants {thin} below {criteria['min_sessions_per_variant']} sessions: "
                                       "too little traffic to tell the concepts apart."])
    profitable = [v for v in usable if (v.get("contribution") or 0) - (v.get("spend") or 0) > 0 and v["orders"] > 0]
    if not profitable:
        return result("no_winner", ["No variant made a contribution profit."])
    best = max(usable, key=lambda v: (v["orders"] / v["sessions"], v.get("contribution") or 0))
    others = [v for v in usable if v is not best]
    if not others:
        if best in profitable:
            return result("inconclusive", ["Only one variant: profitable, but there is nothing to compare it with."])
        return result("no_winner", ["The only variant wasn't profitable."])
    pvals = [two_proportion_p(best["orders"], best["sessions"], o["orders"], o["sessions"]) for o in others]
    if best in profitable and max(pvals) < criteria["significance"]:
        return result("winner", [f"Variant {best['id']} converts better than every other (p<{criteria['significance']}) "
                                 "and makes a contribution profit."], winner=best["id"])
    return result("inconclusive", [f"Differences aren't significant (largest p={max(pvals):.2f})."])
