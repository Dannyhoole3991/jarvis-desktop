"""
Pre-launch creative assessment: ranks a product's QA-passed videos before any test, and says why.

Eight criteria, each 1-5 with the reason: hook clarity, visual clarity, product demonstration, pacing,
offer, call to action, audience suitability and landing-page consistency. They come from the
independent QA review plus the ad research: a concept that matches what long-running adverts for
the same product do scores higher on audience suitability, and our pacing is compared with the
measured shot length of those adverts. That evidence INFORMS the ranking; it is not proof that our
video will sell, so every score is labelled PREDICTION.

No platform gives us a pre-launch performance prediction: Meta's and TikTok's creative tools that
score ads are not available through the APIs we have. That limitation is stated on every ranking;
no predicted CTR, CVR or ROAS is ever invented.
"""

from jarvis_marketing import evidence as E
from jarvis_marketing.agents import adintel

INPUT = {"candidate_ids?": [int]}
OUTPUT = {"rankings": [{"candidate_id": int, "order": [int], "top": [int]}], "limitations": [str]}
CRITERIA = ("hook_clarity", "visual_clarity", "product_demonstration", "pacing", "offer", "call_to_action",
            "audience_suitability", "landing_page_consistency")
NO_PREDICTOR = ("No pre-launch performance prediction is available: Meta and TikTok don't offer one through the APIs "
                "we have, so these are rule-based quality scores, not forecasts of sales.")


def _qa(store, creative_id):
    rows = store.find("qa_reviews", creative_id=creative_id, order="id DESC", limit=1)
    return rows[0] if rows else None


def assess(ctx, creative, found):
    brief = ctx.store.get("briefs", creative["brief_id"])
    q = _qa(ctx.store, creative["id"])
    s = (q or {}).get("scores") or {}

    def g(k):
        return (s.get(k) or {}).get("score", 1)
    out = {}
    out["hook_clarity"] = (g("hook"), (s.get("hook") or {}).get("why", "no QA"))
    out["visual_clarity"] = (round((g("product_accuracy") + g("technical")) / 2, 1), "real product photos and a clean 9:16 export"
                             if g("product_accuracy") >= 4 and g("technical") >= 4 else "accuracy or export problems in QA")
    demo = brief["concept"] in ("demo_closeup", "problem_solution", "objection_comparison")
    out["product_demonstration"] = (min(5, g("benefits") + (1 if demo else 0)),
                                    ("shows the product doing its job" if demo else "shows the product") + f"; QA: {(s.get('benefits') or {}).get('why', '')}")
    pace, why = g("pacing"), (s.get("pacing") or {}).get("why", "")
    median = next((p.get("median_shot_s") for p in found if p.get("median_shot_s")), None)
    scenes = creative["spec"].get("scenes") or []
    ours = round(sum(x["seconds"] for x in scenes) / len(scenes), 2) if scenes else None
    if median and ours and ours > 1.5 * median:
        pace, why = max(1, pace - 1), f"our shots average {ours}s vs {median}s in measured adverts for this product"
    elif median and ours:
        why += f"; our shots average {ours}s vs {median}s in measured adverts"
    out["pacing"] = (pace, why)
    has_price = brief["data"].get("price_stated") is not None
    out["offer"] = (g("offer_cta") if has_price else min(3, g("offer_cta")), "the store price is stated" if has_price else "no price in the video")
    out["call_to_action"] = (5 if brief["data"].get("cta") and brief["data"].get("destination") else 1,
                             f"'{brief['data'].get('cta')}' to the product page")
    match = next((p for p in found if p.get("concept") == brief["concept"]), None)
    if match:
        sc = round(min(5.0, 3 + 2 * match["weight"] + (0.5 if match["long_running_ads"] else 0)), 1)
        why = (f"{match['ads']} analysed adverts for this product use {match['principle']} "
               f"({match['long_running_ads']} kept running 30+ days); evidence of use, not of profit")
    else:
        sc, why = 3, ("no analysed adverts use this angle" if found else "no adverts analysed for this product, so no evidence either way")
    out["audience_suitability"] = (sc, why)
    out["landing_page_consistency"] = (g("landing_page"), (s.get("landing_page") or {}).get("why", "no QA"))
    total = round(sum(v[0] for v in out.values()) / len(out), 2)
    return {"creative_id": creative["id"], "concept": brief["concept"], "total": total,
            "scores": {k: {"score": v[0], "why": v[1]} for k, v in out.items()}, "qa_overall": (q or {}).get("overall"),
            "research_support": match and {k: match[k] for k in ("principle", "ads", "long_running_ads", "example_ad_ids")},
            "label": E.PREDICTION, "simulated": bool(creative["simulated"])}


def run(ctx, inp):
    ids = inp.get("candidate_ids")
    passed = ctx.store.find("creatives", business_id=ctx.business_id, status="qa_passed", limit=5000)
    by = {}
    for c in passed:
        by.setdefault(c["candidate_id"], []).append(c)
    rankings = []
    for cand_id, creatives in sorted(by.items()):
        ctx.check()
        if ids and cand_id not in ids:
            continue
        cand = ctx.store.get("candidates", cand_id)
        found = adintel.principles(ctx.store, ctx.business_id, cand["data"].get("keyword") or "")
        rows = sorted((assess(ctx, c, found) for c in creatives), key=lambda r: (-r["total"], r["creative_id"]))
        for i, r in enumerate(rows, 1):
            strong = sorted(r["scores"].items(), key=lambda kv: -kv[1]["score"])[:2]
            weak = [k for k, v in r["scores"].items() if v["score"] < 3]
            r["rank"] = i
            r["explanation"] = (f"#{i}: strongest on {', '.join(k.replace('_', ' ') for k, _ in strong)}"
                                + (f"; weak on {', '.join(w.replace('_', ' ') for w in weak)}" if weak else "")
                                + (f"; backed by {r['research_support']['ads']} analysed adverts" if r["research_support"] else ""))
        n = ctx.business.get("paid_test", {}).get("max_variants", 3)
        data = {"ranking": rows, "test_first": [r["creative_id"] for r in rows[:n]],
                "why_first": "Highest combined pre-launch score; ties go to the earlier creative. Differences under 0.3 are within "
                             "the noise of rule-based scoring, so treat close ranks as equal.",
                "prediction_service": {"available": False, "why": NO_PREDICTOR}, "label": E.PREDICTION}
        ctx.store.insert("assessments", business_id=ctx.business_id, candidate_id=cand_id, data_json=data, label=E.PREDICTION,
                         created_at=ctx.now(), model_version=ctx.model_version, simulated=ctx.simulated)
        rankings.append({"candidate_id": cand_id, "order": [r["creative_id"] for r in rows], "top": data["test_first"]})
    return {"rankings": rankings, "limitations": [NO_PREDICTOR]}
