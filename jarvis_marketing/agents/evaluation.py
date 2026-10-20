"""
Product Evaluation agent: turns researched candidates into a prioritised shortlist, with the sums shown.

Inputs come from FACTS only (supplier price, shipping, listing price, competitor prices, images,
delivery, stock). Currency conversion and the costs worked out from it are ESTIMATES with their
basis. If the product is already listed in our store, its live price is used (that's what a customer
pays); otherwise a price is PROPOSED (never set: price changes are disabled).
"""

from jarvis_marketing import evidence as E, scoring

INPUT = {"candidate_ids?": [int]}
OUTPUT = {"shortlist": [{"candidate_id": int, "title": str, "score": float, "contribution": float, "price": float,
                         "listed": bool}],
          "rejected": [{"candidate_id": int, "title": str, "reasons": [str]}], "reserve": [int]}

TARGETS = {  # who it's for and the problem, by product word. HYPOTHESES to test, never facts.
    "organi": ("people with cluttered drawers, desks or cables", "things get tangled and lost"),
    "stand": ("people who watch or video-call on their phone", "holding the phone gets tiring"),
    "lint": ("people who want clothes and sofas to look fresh", "bobbles and fluff make clothes look old"),
    "hook": ("people short of storage space", "nowhere to hang things"),
    "rack": ("home cooks with crowded cupboards", "can't find the jar you need"),
    "light": ("people who get up in the dark", "fumbling for a switch at night"),
}


def target_for(keyword):
    for word, (who, problem) in TARGETS.items():
        if word in keyword:
            return {"customer": who, "problem": problem, "label": E.HYPOTHESIS}
    return {"customer": f"UK shoppers looking for a {keyword}", "problem": f"needs a simple {keyword}", "label": E.HYPOTHESIS}


def signals_for(ctx, cand):
    key = f"{cand['supplier']}:{cand['supplier_product_id']}"
    f = E.facts(ctx.store, "candidate", key)
    opp = ctx.store.get("opportunities", cand["opportunity_id"]) if cand["opportunity_id"] else None
    comp = [r["value"] for r in E.for_subject(ctx.store, "opportunity", opp["keyword"], E.SOURCED) if r["field"] == "competitor_price"] if opp else []
    ship = f.get("uk_shipping") or {}
    n_facts = len(E.for_subject(ctx.store, "candidate", key, E.SOURCED))
    return {"supplier_listed": f.get("supplier_listed"), "competitor_prices": comp,
            "delivery_days_max": ship.get("max_days"), "images": len(f["images"]) if f.get("images") else None,
            "stock": f.get("stock"), "fact_count": n_facts + len(comp), "denied": cand["data"].get("denied"),
            "duplicate_of": cand["data"].get("similar_to"), "_price_usd": f.get("supplier_price_usd"),
            "_shipping_usd": ship.get("usd"), "_listing_price": f.get("store_listing"), "_key": key}


def evaluate_one(ctx, cand):
    s, econ_cfg, bid = signals_for(ctx, cand), ctx.economics, ctx.business_id
    key, fx = s["_key"], econ_cfg["usd_to_business"]
    econ, ev = None, []
    if s["_price_usd"] is not None and s["_shipping_usd"] is not None:
        cost = round(s["_price_usd"] * fx, 2)
        ship = round(s["_shipping_usd"] * fx, 2)
        ev.append(E.add(ctx.store, bid, "candidate", key, E.ESTIMATE, "product_cost", f"Product cost about {cost:.2f}", value=cost,
                        basis=f"supplier ${s['_price_usd']} x fixed rate {fx}", confidence=0.7))
        ev.append(E.add(ctx.store, bid, "candidate", key, E.ESTIMATE, "shipping_cost", f"Shipping about {ship:.2f}", value=ship,
                        basis=f"supplier ${s['_shipping_usd']} x fixed rate {fx}", confidence=0.7))
        fees = {k: econ_cfg[k] for k in ("payment_fee_pct", "payment_fee_fixed", "return_rate", "other_variable_cost")}
        listed = s["_listing_price"] is not None
        price = s["_listing_price"] if listed else scoring.propose_price(cost, ship, econ_cfg["target_contribution"], **fees)
        econ = scoring.unit_economics(price, cost, ship, **fees)
        econ["price_source"] = "live store price (fact)" if listed else "PROPOSED (not set: price changes are disabled)"
        ev.append(E.add(ctx.store, bid, "candidate", key, E.ESTIMATE, "contribution", f"Contribution about {econ['contribution']:.2f} per order",
                        value=econ["contribution"], basis=econ["formula"] + f"; return rate {fees['return_rate']} assumed", confidence=0.6))
    result = scoring.score(econ, s, ctx.thresholds, ctx.business["weights"])
    kw = cand["data"]["keyword"]
    comp = s["competitor_prices"]
    card = {"description": {"name": cand["title"], "material": cand["data"].get("material"),
                            "options": cand["data"].get("options"), "packing_list": cand["data"].get("packing_list")},
            "supplier": {"name": cand["supplier"], "product_id": cand["supplier_product_id"], "listed_by": s["supplier_listed"],
                         "delivery_days_max": s["delivery_days_max"], "stock": s["stock"]},
            "target": target_for(kw),
            "competitor_pricing": {"count": len(comp), "min": min(comp) if comp else None, "max": max(comp) if comp else None,
                                   "median": sorted(comp)[len(comp) // 2] if comp else None},
            "economics": econ, "score": result, "risks": cand["data"].get("risks") or [], "gaps": cand["data"].get("gaps") or [],
            "evidence_ids": cand["data"].get("evidence_ids", []) + ev,
            "ad_angles": [{"angle": a, "label": E.HYPOTHESIS} for a in ("show the problem, then the fix", "close-up demo of it working",
                                                                         "the exact price and free delivery")],
            "test_budget": {"money": 0.0, "posts_per_variant": ctx.testing["posts_per_experiment"],
                            "note": "Organic posts only: paid spend is disabled in this milestone."},
            "success_criteria": {"primary": "contribution profit from attributed orders",
                                 "min_sessions_per_variant": ctx.testing["min_sessions_per_variant"],
                                 "attribution_window_days": ctx.testing["attribution_window_days"],
                                 "break_even_cac": econ["break_even_cac"] if econ else None},
            "listed": s["_listing_price"] is not None,
            "next_step": None if s["_listing_price"] is not None else
            "Not in the store yet: list it (a jarvis_store draft Danny publishes) before any post can link to it."}
    return result, econ, card


def run(ctx, inp):
    ids = inp.get("candidate_ids")
    cands = [ctx.store.get("candidates", i) for i in ids] if ids else \
        ctx.store.find("candidates", business_id=ctx.business_id, status=["researched", "reserve"])
    passed, rejected = [], []
    for cand in cands:
        ctx.check()
        result, econ, card = evaluate_one(ctx, cand)
        data = dict(cand["data"], card=card)
        if result["rejected"]:
            ctx.store.update("candidates", cand["id"], status="rejected", score=result["score"], reasons_json=result["rejections"],
                             data_json=data, updated_at=ctx.now())
            rejected.append({"candidate_id": cand["id"], "title": cand["title"], "reasons": result["rejections"]})
        else:
            ctx.store.update("candidates", cand["id"], score=result["score"], reasons_json=[], data_json=data, updated_at=ctx.now())
            passed.append({"id": cand["id"], "score": result["score"], "confidence": result["confidence"], "title": cand["title"],
                           "econ": econ, "listed": card["listed"]})
    ranked = scoring.rank(passed)
    n = ctx.limits["shortlist_size"]
    # Listed products first within the shortlist size: only they can be tested now.
    ranked = sorted(ranked, key=lambda p: not p["listed"])      # stable: score order kept within each group
    shortlist, reserve = [], []
    for i, p in enumerate(ranked):
        status = "shortlisted" if i < n else "reserve"
        ctx.store.update("candidates", p["id"], status=status, updated_at=ctx.now())
        if status == "shortlisted":
            shortlist.append({"candidate_id": p["id"], "title": p["title"], "score": float(p["score"]),
                              "contribution": float(p["econ"]["contribution"]), "price": float(p["econ"]["price"]),
                              "listed": p["listed"]})
        else:
            reserve.append(p["id"])
    ctx.store.audit("evaluation", "shortlist", "ok", business_id=ctx.business_id,
                    shortlist=[s["candidate_id"] for s in shortlist], rejected=len(rejected))
    return {"shortlist": shortlist, "rejected": rejected, "reserve": reserve}
