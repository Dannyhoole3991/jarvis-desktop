"""
Creative Strategy agent: genuinely different concepts per product, one variable under test.

Each concept is a different ANGLE (problem/fix, close-up demo, exact price, options, gift, what's in
the box), with its own hook, scene order and first shot. Across a product's briefs only the concept
changes: the product (same supplier id, same photos), price, call to action, destination and length
are held constant, so a difference in results can be put down to the concept.

Briefs are built only from the product's facts. A concept whose facts are missing (no options, no
packing list, no listed price) is not offered. All visible text passes claims.check.
"""

import hashlib
import json
import re
import time
from difflib import SequenceMatcher

from jarvis_marketing import claims, evidence as E
from jarvis_marketing.agents.evaluation import target_for

INPUT = {"candidate_ids?": [int]}
OUTPUT = {"briefs": [{"brief_id": int, "candidate_id": int, "concept": str, "hook": str}], "needs_listing": [int],
          "skipped": [str]}
ORDER = ["problem_solution", "demo_closeup", "price_reveal", "options_showcase", "gift_idea", "unboxing"]
DURATION_S = 15
DISTINCT_MAX_SIMILARITY = 0.6


def identity(cand):
    images = list(cand["data"].get("images") or [])
    return {"supplier": cand["supplier"], "supplier_product_id": cand["supplier_product_id"], "title": cand["title"],
            "images": images, "images_hash": hashlib.sha256(json.dumps(images).encode()).hexdigest()[:16]}


def _facts(ctx, cand):
    f = E.facts(ctx.store, "candidate", f"{cand['supplier']}:{cand['supplier_product_id']}")
    opp = ctx.store.get("opportunities", cand["opportunity_id"]) if cand["opportunity_id"] else None
    of = E.facts(ctx.store, "opportunity", opp["keyword"]) if opp else {}
    return f, of


def _money(x):
    return f"£{x:.2f}"


def concepts_for(ctx, cand):
    """{concept: brief fields} for every concept the facts support."""
    f, of = _facts(ctx, cand)
    listing = cand["data"].get("listing") or {}
    price = f.get("store_listing")
    name = cand["title"]
    target = target_for(cand["data"]["keyword"])
    ship = f.get("uk_shipping") or {}
    free = ctx.business.get("free_uk_delivery")
    options = [o for o in (f.get("options") or []) if len(o.get("values") or []) > 1]
    imgs = len(f.get("images") or [])
    offer = (_money(price) + (" · Free UK delivery" if free else "")) if price is not None else "Collective Finds"
    cta = "Tap the link to see it on Collective Finds"
    out = {}

    def scenes(*rows):
        return [{"n": i + 1, "seconds": s, "visual": v, "on_screen_text": t, "vo": vo} for i, (s, v, t, vo) in enumerate(rows)]
    out["problem_solution"] = dict(
        hypothesis=f"Naming the everyday problem ({target['problem']}) first earns the click.",
        hook=f"Tired of this? {target['problem'].capitalize()}.",
        demo="photo 1 with the problem named, then photos 2-3 showing the product doing its job",
        scenes=scenes((2.5, "photo 1, slow push-in", f"Tired of this?", f"{target['problem'].capitalize()}?"),
                      (4.0, "photo 2, close-up", f"Meet the {name}", f"This is the {name}."),
                      (4.0, "photo 3, zoom out", "Sorted.", "It keeps things simple."),
                      (4.5, "end card with the shop name", offer, cta + ".")))
    if imgs >= 4:
        details = [x for x in (f.get("material"),) if x]
        out["demo_closeup"] = dict(
            hypothesis="A close look at the real product, detail by detail, builds enough trust to click.",
            hook=f"Here's the {name} up close.",
            demo="four supplier photos, one detail each",
            scenes=scenes((2.0, "photo 2, extreme close-up", "Up close", f"Here's the {name} up close."),
                          (3.0, "photo 3, pan", details[0].capitalize() if details else "Every detail", f"Made from {details[0]}." if details else "Take a closer look."),
                          (3.0, "photo 4, pan", "From every angle", "From every angle."),
                          (2.5, "photo 1, full product", name, "Simple and useful."),
                          (4.5, "end card with the shop name", offer, cta + ".")))
    if price is not None:
        out["price_reveal"] = dict(
            hypothesis="Saying the exact price straight away filters for buyers, so fewer clicks but more orders per click.",
            hook=_money(price) + (", delivered free." if free else "."),
            demo="the price first, then the product",
            scenes=scenes((2.5, "photo 1 with the price large", _money(price), f"This costs {_money(price)}."),
                          (3.5, "photo 2", name, f"It's the {name}."),
                          ((4.0, "photo 3", "Free UK delivery", f"Delivery usually takes {ship['min_days']} to {ship['max_days']} days.")
                           if free and ship else (4.0, "photo 3", name, "Take a look.")),
                          (5.0, "end card with the shop name", offer, cta + ".")))
    if options:
        o = options[0]
        vals = o["values"][:4]
        out["options_showcase"] = dict(
            hypothesis=f"Showing every {o['name'].lower()} lets people picture the one they'd pick.",
            hook=f"Which {o['name'].lower()} would you pick?",
            demo=f"one shot per {o['name'].lower()}, using the supplier's photos",
            scenes=scenes((2.5, "photo 1", f"Which {o['name'].lower()}?", f"Which {o['name'].lower()} would you pick?"),
                          (4.5, "photos 2-3, quick cuts", " / ".join(vals), ", ".join(vals[:-1]) + " or " + vals[-1] + "."),
                          (3.5, "photo 4 or 1", name, f"The {name}."),
                          (4.5, "end card with the shop name", offer, cta + ".")))
    month = int(time.strftime("%m", time.gmtime(ctx.now())))
    if of.get("peak_month") in (11, 12) or month in (10, 11, 12):
        out["gift_idea"] = dict(
            hypothesis="Framing it as a small gift reaches people shopping for someone else.",
            hook="A small gift idea that actually gets used.",
            demo="the product as a gift, then what it does",
            scenes=scenes((3.0, "photo 1, soft zoom", "Gift idea", "A small gift idea that actually gets used."),
                          (4.0, "photo 2", name, f"The {name}."),
                          (3.5, "photo 3", "Useful every day", "Useful every day."),
                          (4.5, "end card with the shop name", offer, cta + ".")))
    if f.get("packing_list"):
        out["unboxing"] = dict(
            hypothesis="Showing exactly what arrives removes the 'what do I get?' doubt.",
            hook="What's in the box?",
            demo="the packing list over the product photos",
            scenes=scenes((2.5, "photo 1", "What's in the box?", "What's in the box?"),
                          (4.0, "photo 2", f["packing_list"], f"You get {f['packing_list']}."),
                          (4.0, "photo 3", name, f"The {name}."),
                          (4.5, "end card with the shop name", offer, cta + ".")))
    allowed = [price] if price is not None else []
    allowed += [ship.get("min_days"), ship.get("max_days")] if ship else []
    allowed += [float(n) for n in re.findall(r"\d+(?:\.\d+)?", " ".join(str(f.get(k) or "") for k in ("packing_list", "material")))]
    allowed = [a for a in allowed if a is not None]
    common = {"target_customer": target, "aspect": "9:16", "duration_s": DURATION_S, "captions": True, "cta": cta,
              "destination": listing.get("url"), "price_stated": price, "audience": "UK, TikTok For You feed (organic)",
              "placement": "TikTok organic post", "test_variable": "concept (angle + hook + scene order)",
              "held_constant": ["product", "photos", "price", "call to action", "destination", "length"],
              "allowed_numbers": allowed, "product_identity": identity(cand), "label": E.HYPOTHESIS,
              "hashtags": ctx.business.get("hashtags") or []}
    for key, b in out.items():
        b.update(common, concept=key)
        b["vo_lines"] = [s["vo"] for s in b["scenes"]]
        tail = "" if key == "price_reveal" or price is None else f" {_money(price)}" + (" with free UK delivery" if free else "")
        b["caption"] = f"{b['hook']} {name}{tail}."
    return out


def _distinct(briefs):
    """True if no two hooks or first shots are near-copies."""
    for i, a in enumerate(briefs):
        for b in briefs[i + 1:]:
            if SequenceMatcher(None, a["hook"].lower(), b["hook"].lower()).ratio() > DISTINCT_MAX_SIMILARITY:
                return False
            if a["scenes"][0]["on_screen_text"] == b["scenes"][0]["on_screen_text"]:
                return False
    return True


def run(ctx, inp):
    from jarvis_marketing.agents import learning
    ids = inp.get("candidate_ids")
    cands = [ctx.store.get("candidates", i) for i in ids] if ids else ctx.store.find("candidates", business_id=ctx.business_id, status="shortlisted")
    prio = learning.priorities(ctx.store, ctx.business_id)
    made, needs_listing, skipped = [], [], []
    for cand in cands:
        ctx.check()
        if not (cand["data"].get("listing") or {}).get("url"):
            needs_listing.append(cand["id"])
            continue
        available = concepts_for(ctx, cand)
        order = sorted(available, key=lambda c: (-prio.get(f"concept:{c}", 0), ORDER.index(c)))
        chosen = []
        for concept in order:
            b = available[concept]
            try:
                claims.check_all([b["hook"], b["caption"], b["cta"], *b["vo_lines"], *(s["on_screen_text"] for s in b["scenes"])],
                                 b["allowed_numbers"])
            except claims.UnsupportedClaim as why:
                skipped.append(f"{cand['title']} / {concept}: {why}")
                continue
            if _distinct(chosen + [b]):
                chosen.append(b)
            if len(chosen) >= ctx.limits["briefs_per_product"]:
                break
        for b in chosen:
            if ctx.store.one("briefs", candidate_id=cand["id"], concept=b["concept"]):
                skipped.append(f"{cand['title']} / {b['concept']}: brief already exists")
                continue
            bid = ctx.store.insert("briefs", business_id=ctx.business_id, candidate_id=cand["id"], concept=b["concept"], status="ready",
                                   label=E.HYPOTHESIS, data_json=b, created_at=ctx.now(), model_version=ctx.model_version,
                                   simulated=ctx.simulated)
            made.append({"brief_id": bid, "candidate_id": cand["id"], "concept": b["concept"], "hook": b["hook"]})
        if cand["status"] == "shortlisted":
            ctx.store.update("candidates", cand["id"], status="in_creative", updated_at=ctx.now())
    return {"briefs": made, "needs_listing": needs_listing, "skipped": skipped}
