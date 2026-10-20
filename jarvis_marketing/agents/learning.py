"""
Learning & Knowledge agent: turns concluded experiments and rejections into lessons that keep their
evidence, and feeds them back into what research and creative try next.

Labels: a lesson is "verified" only when it rests on real (not simulated), final data and a clear
verdict (winner / no_winner). Inconclusive results give a "hypothesis" lesson at most, and SIMULATED
results are always marked simulated. Lessons never invent numbers: each cites experiment, posts,
metrics and order ids.
"""

import time

from jarvis_marketing import evidence as E

INPUT = {}
OUTPUT = {"lessons": [int], "report_ids": [int]}


def priorities(store, business_id):
    """{"keyword:<kw>" or "concept:<c>": weight}. Verified lessons count fully, others half; simulated ones only
    steer simulated runs (a separate database), so live priorities are never moved by mock data."""
    out = {}
    for l in store.find("lessons", business_id=business_id, limit=5000):
        if not l["topic"] or not l["direction"]:
            continue
        w = 1.0 if l["label"] == E.VERIFIED else 0.5
        out[l["topic"]] = out.get(l["topic"], 0) + w * l["direction"]
    return out


def _lesson(ctx, experiment_id, label, text, topic, direction, evidence):
    if ctx.store.one("lessons", business_id=ctx.business_id, experiment_id=experiment_id, topic=topic):
        return None
    return ctx.store.insert("lessons", business_id=ctx.business_id, experiment_id=experiment_id, label=label, text=text[:800],
                            topic=topic, direction=direction, evidence_json=evidence, created_at=ctx.now(),
                            model_version=ctx.model_version, simulated=ctx.simulated)


def learn_from_experiments(ctx):
    made = []
    for exp in ctx.store.find("experiments", business_id=ctx.business_id, status="concluded"):
        vj = exp["result"] or {}
        real_final = not exp["simulated"] and vj.get("data_final")
        verdict = exp["verdict"]
        cand = ctx.store.get("candidates", exp["candidate_id"])
        posts = ctx.store.find("posts", experiment_id=exp["id"])
        metric_ids = [m["id"] for p in posts for m in ctx.store.find("metrics", post_id=p["id"], limit=200)]
        order_ids = [o["id"] for p in posts for o in ctx.store.find("orders", post_id=p["id"])]
        evidence = {"experiment_id": exp["id"], "post_ids": [p["id"] for p in posts], "metric_ids": metric_ids,
                    "order_ids": order_ids, "verdict": verdict, "reasons": vj.get("reasons"), "simulated": bool(exp["simulated"])}
        sim = " (SIMULATED)" if exp["simulated"] else ""
        if verdict == "winner":
            winning_post = ctx.store.get("posts", vj["winner"])
            win = next(v for v in exp["data"]["variants"] if v["creative_id"] == winning_post["creative_id"])
            label = E.VERIFIED if real_final else E.ESTIMATE
            made.append(_lesson(ctx, exp["id"], label, f"{cand['title']}: the '{win['concept']}' concept converted best and made a "
                                f"profit ({vj['attributed_orders']} attributed orders, {vj['attributed_revenue']} revenue){sim}.",
                                f"concept:{win['concept']}", 1, evidence))
            made.append(_lesson(ctx, exp["id"], label, f"'{cand['data']['keyword']}' produced a profitable product{sim}.",
                                f"keyword:{cand['data']['keyword']}", 1, evidence))
        elif verdict == "no_winner":
            label = E.VERIFIED if real_final else E.ESTIMATE
            made.append(_lesson(ctx, exp["id"], label, f"{cand['title']}: no concept made a profit{sim}. Reasons: "
                                + "; ".join(vj.get("reasons") or []), f"keyword:{cand['data']['keyword']}", -1, evidence))
        else:
            made.append(_lesson(ctx, exp["id"], E.HYPOTHESIS, f"{cand['title']}: inconclusive{sim}; nothing is concluded about its "
                                "concepts. " + "; ".join(vj.get("reasons") or []), f"experiment:{exp['id']}", 0, evidence))
        ctx.store.update("experiments", exp["id"], status="learned")
    return [m for m in made if m]


def learn_from_rejections(ctx):
    """A keyword whose every candidate was rejected is a fact-based lesson (with the rejection reasons)."""
    made = []
    for opp in ctx.store.find("opportunities", business_id=ctx.business_id):
        cands = ctx.store.find("candidates", opportunity_id=opp["id"])
        if cands and all(c["status"] == "rejected" for c in cands):
            reasons = sorted({r for c in cands for r in (c["reasons"] or [])})[:6]
            lid = _lesson(ctx, None, E.ESTIMATE, f"Every '{opp['keyword']}' product failed the thresholds: " + "; ".join(reasons),
                          f"keyword:{opp['keyword']}", -1, {"opportunity_id": opp["id"], "candidate_ids": [c["id"] for c in cands],
                                                            "reasons": reasons, "simulated": bool(opp["simulated"])})
            if lid:
                made.append(lid)
    return made


def report(ctx, kind="daily"):
    """A daily/weekly report: actuals (from records) kept apart from estimates. One per period."""
    now = ctx.now()
    period = time.strftime("%Y-%m-%d", time.gmtime(now)) if kind == "daily" else time.strftime("%Y-W%W", time.gmtime(now))
    span = 86400 if kind == "daily" else 7 * 86400
    since = now - span
    s, bid = ctx.store, ctx.business_id
    orders = s.sql("SELECT COUNT(*) AS n, COALESCE(SUM(total),0) AS rev, COALESCE(SUM(post_id IS NOT NULL),0) AS attributed "
                   "FROM orders WHERE business_id=? AND created_at>=?", (bid, since))[0]
    posts = s.sql("SELECT status, COUNT(*) AS n FROM posts WHERE business_id=? GROUP BY status", (bid,))
    exps = s.sql("SELECT verdict, COUNT(*) AS n FROM experiments WHERE business_id=? GROUP BY verdict", (bid,))
    cands = s.sql("SELECT status, COUNT(*) AS n FROM candidates WHERE business_id=? GROUP BY status", (bid,))
    pending = len(s.find("approvals", business_id=bid, status="pending"))
    data = {"actual": {"orders": orders["n"], "revenue": round(orders["rev"], 2), "attributed_orders": orders["attributed"],
                       "spend": 0.0, "posts": {r["status"]: r["n"] for r in posts}},
            "pipeline": {r["status"]: r["n"] for r in cands}, "verdicts": {str(r["verdict"]): r["n"] for r in exps},
            "pending_approvals": pending, "simulated": ctx.simulated}
    sim = "SIMULATED " if ctx.simulated else ""
    text = (f"{sim}{kind.title()} marketing report {period}: {orders['n']} orders, {orders['rev']:.2f} revenue "
            f"({orders['attributed']} attributed to posts), spend 0.00 (paid is off). Posts: "
            + (", ".join(f"{r['n']} {r['status']}" for r in posts) or "none")
            + ". Experiments: " + (", ".join(f"{r['n']} {r['verdict'] or 'running'}" for r in exps) or "none")
            + f". {pending} waiting for your approval.")
    existing = s.one("reports", business_id=bid, kind=kind, period=period)
    if existing:
        s.update("reports", existing["id"], text=text, data_json=data, created_at=now)
        return existing["id"]
    return s.insert("reports", business_id=bid, kind=kind, period=period, text=text, data_json=data, created_at=now)


def run(ctx, inp):
    lessons = learn_from_experiments(ctx) + learn_from_rejections(ctx)
    reports = [report(ctx, "daily")]
    if time.gmtime(ctx.now()).tm_wday == 0 or not ctx.store.find("reports", business_id=ctx.business_id, kind="weekly"):
        reports.append(report(ctx, "weekly"))
    return {"lessons": lessons, "report_ids": reports}
