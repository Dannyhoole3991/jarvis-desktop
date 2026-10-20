"""
Market Research agent: finds product opportunities and records the evidence, never more.

For each keyword: the supplier catalogue (price, how many stores list it, images, options, delivery,
stock), competitor prices, and, only where a real source exists, search demand, short-form video
signals and reviews. Every number is stored as a sourced FACT tied to the read it came from;
anything worked out is an ESTIMATE with its basis; gaps are listed as gaps. Output: opportunities
ranked by the evidence, with their risks.
"""

import math
import re
import statistics
from difflib import SequenceMatcher

from jarvis_marketing import evidence as E
from jarvis_marketing.adapters import Unavailable

INPUT = {"keywords?": [str], "force?": bool}
OUTPUT = {"opportunities": [{"keyword": str, "opportunity_id": int, "rank": int, "score": float, "candidate_ids": [int],
                             "facts": int, "risks": [str], "gaps": [str]}], "skipped": [str]}
RECENT_SECONDS = 7 * 86400


def _deny():
    try:
        from jarvis_store.pipeline import DENY
        return DENY
    except Exception:            # jarvis_store unavailable: a minimal safety list
        return re.compile(r"(?i)\b(baby|infant|kid|kids|child|children|toy|knife|weapon|vape|medical|supplement)\b")


def default_keywords(ctx):
    if ctx.business.get("seed_keywords"):
        return list(ctx.business["seed_keywords"])
    if ctx.simulated:
        from jarvis_marketing.adapters.mock import KEYWORDS
        return list(KEYWORDS)
    from jarvis_store.pipeline import SEED_KEYWORDS
    return list(SEED_KEYWORDS)


def pick_keywords(ctx, explicit=None, force=False):
    from jarvis_marketing.agents import learning
    if explicit:
        return list(explicit)[: ctx.limits["keywords_per_cycle"]], []
    prio = learning.priorities(ctx.store, ctx.business_id)
    words = sorted(default_keywords(ctx), key=lambda k: -prio.get(f"keyword:{k}", 0))
    picked, skipped = [], []
    for kw in words:
        row = ctx.store.one("opportunities", business_id=ctx.business_id, keyword=kw)
        if prio.get(f"keyword:{kw}", 0) < 0:
            skipped.append(f"{kw}: earlier results were poor (see lessons)")
            continue
        if row and not force and ctx.now() - row["updated_at"] < RECENT_SECONDS:
            skipped.append(f"{kw}: researched in the last 7 days")
            continue
        picked.append(kw)
        if len(picked) >= ctx.limits["keywords_per_cycle"]:
            break
    return picked, skipped


def _listing_for(listings, product, spu=None):
    for item in listings:
        if item.get("supplier_product_id") == product["id"]:
            return item
        if spu and (f"cj:{spu}".lower() in [t.lower() for t in item.get("tags") or []]
                    or any((s or "").upper().startswith(spu.upper()) for s in item.get("skus") or [])):
            return item
    return None


def _similar(listings, name):
    for item in listings:
        if SequenceMatcher(None, item["title"].lower(), name.lower()).ratio() >= 0.8:
            return item
    return None


def research_product(ctx, keyword, item, search_fid, listings, listings_fid):
    """One supplier product: facts, risks, gaps. Returns the candidate id."""
    a, bid = ctx.adapters, ctx.business_id
    key = f"{a.supplier.name}:{item['id']}"
    ev, risks, gaps = [], [], []

    def fact(field, claim, fid, quote_key, value, unit=None):
        ev.append(E.fact(ctx.store, bid, "candidate", key, field, claim, fid, quote_key, value, unit=unit))

    ev.append(E.add(ctx.store, bid, "candidate", key, E.FACT, "supplier_listed", f"{item['listed']} stores list it on the supplier",
                    value=item["listed"], fetch_id=search_fid, quote=E.item_quote(item), confidence=0.8))
    d, dfid = a.supplier.details(item["id"])
    fact("name", "Supplier product name", dfid, "name", d["name"])
    if d.get("images"):
        fact("images", f"{len(d['images'])} supplier photos", dfid, "images", d["images"])
    variants = d.get("variants") or []
    if variants:
        v = variants[0]
        ev.append(E.add(ctx.store, bid, "candidate", key, E.FACT, "supplier_price_usd", f"Supplier price ${v['price_usd']}",
                        value=v["price_usd"], unit="USD", fetch_id=dfid, quote=E.item_quote(v), confidence=0.9))
    for field in ("material", "packing_list", "options", "category"):
        if d.get(field):
            fact(field, f"Supplier {field.replace('_', ' ')}", dfid, field, d[field])
    if d.get("stock") is not None:
        fact("stock", f"Supplier stock {d['stock']}", dfid, "stock", d["stock"])
    else:
        gaps.append("supplier stock not reported")
    shipping = None
    if variants:
        ctx.check()
        opts, sfid = a.supplier.shipping(variants[0]["vid"])
        if opts:
            shipping = min(opts, key=lambda o: o["usd"])
            ev.append(E.add(ctx.store, bid, "candidate", key, E.FACT, "uk_shipping", f"UK shipping ${shipping['usd']}, "
                            f"{shipping['min_days']}-{shipping['max_days']} days", value=shipping, fetch_id=sfid,
                            quote=E.item_quote(shipping), confidence=0.85))
        else:
            gaps.append("no UK shipping option")
    try:
        rv, rfid = a.reviews.complaints(item["id"])
        fact("review_themes", "Review themes", rfid, "themes", rv["themes"])
        risks += [f"Reviews mention: {t}" for t in rv["themes"] if t != "works well"]
    except Unavailable as why:
        gaps.append(f"reviews: {why}")
    name = d["name"] or item["name"]
    denied = _deny().search(f"{name} {keyword}")
    if denied:
        risks.append(f"Restricted product type: '{denied.group(0)}'")
    listing = _listing_for(listings, item, d.get("spu"))
    similar = None if listing else _similar(listings, name)
    if listing:
        ev.append(E.add(ctx.store, bid, "candidate", key, E.FACT, "store_listing", f"Listed in our store at {listing['price_min']}",
                        value=listing["price_min"], fetch_id=listings_fid, quote=E.item_quote(listing), confidence=0.95))
    if similar:
        risks.append(f"Very similar to '{similar['title']}', already in our store")
    data = {"keyword": keyword, "name": name, "images": d.get("images") or [], "options": d.get("options") or [],
            "variants": variants, "material": d.get("material"), "packing_list": d.get("packing_list"),
            "spu": d.get("spu"), "price_usd": variants[0]["price_usd"] if variants else None, "listed": item["listed"],
            "shipping": shipping, "stock": d.get("stock"), "listing": listing, "similar_to": similar["title"] if similar else None,
            "denied": denied.group(0) if denied else None, "risks": risks, "gaps": gaps, "evidence_ids": ev}
    row = ctx.store.one("candidates", business_id=bid, supplier=a.supplier.name, supplier_product_id=item["id"])
    now = ctx.now()
    if row:
        ctx.store.update("candidates", row["id"], title=name, status="researched", data_json=data, updated_at=now,
                         reasons_json=[], score=None)
        return row["id"]
    return ctx.store.insert("candidates", business_id=bid, opportunity_id=None, supplier=a.supplier.name,
                            supplier_product_id=item["id"], title=name, status="researched", data_json=data, reasons_json=[],
                            created_at=now, updated_at=now, simulated=ctx.simulated, model_version=ctx.model_version)


def research_keyword(ctx, keyword, listings, listings_fid):
    a, bid = ctx.adapters, ctx.business_id
    risks, gaps, opp_ev = [], [], []
    items, sfid = a.supplier.search(keyword)
    items = sorted(items, key=lambda i: -i["listed"])[: ctx.limits["products_per_keyword"]]
    if not items:
        gaps.append("supplier has no products for this keyword")
    try:
        comp, cfid = a.competitors.prices(keyword)
        for c in comp:
            fid = c.pop("fetch_id", None) or cfid
            opp_ev.append(E.add(ctx.store, bid, "opportunity", keyword, E.FACT, "competitor_price",
                                f"{c['store']} sells '{c['title']}' at {c['price']}", value=c["price"], fetch_id=fid,
                                quote=E.item_quote(c), confidence=0.8))
        if comp:
            opp_ev.append(E.add(ctx.store, bid, "opportunity", keyword, E.ESTIMATE, "saturation",
                                f"{len(comp)} competitor listings found", value=round(min(1.0, len(comp) / 10), 2),
                                basis=f"{len(comp)} matching listings in the competitor stores checked / 10", confidence=0.4))
        else:
            gaps.append("no competitor prices found")
    except Unavailable as why:
        gaps.append(f"competitor prices: {why}")
    trend = None
    try:
        t, tfid = a.trends.interest(keyword)
        opp_ev.append(E.fact(ctx.store, bid, "opportunity", keyword, "trend_12w", "12-week change in search interest", tfid, "trend_12w", t["trend_12w"]))
        opp_ev.append(E.fact(ctx.store, bid, "opportunity", keyword, "peak_month", "Seasonal peak month", tfid, "peak_month", t["peak_month"]))
        trend = t["trend_12w"]
    except Unavailable as why:
        gaps.append(f"search demand: {why}")
    try:
        s, ssid = a.shortform.signals(keyword)
        opp_ev.append(E.fact(ctx.store, bid, "opportunity", keyword, "shortform_median_views", "Median views of sampled videos", ssid,
                             "median_views", s["median_views"]))
    except Unavailable as why:
        gaps.append(f"short-form video signals: {why}")
    cids = []
    for item in items:
        ctx.check()
        try:
            cids.append(research_product(ctx, keyword, item, sfid, listings, listings_fid))
        except E.FabricatedEvidence:
            raise
        except Exception as error:
            gaps.append(f"{item['id']}: couldn't read ({str(error)[:120]})")
    listed = [i["listed"] for i in items]
    demand = math.log10(statistics.median(listed) + 1) / 3.5 if listed else 0.0
    score = round(100 * min(1.0, demand) * (1.0 if trend is None else (1.1 if trend > 0 else 0.9)), 1)
    facts = sum(1 for _ in opp_ev) + sum(len(ctx.store.get("candidates", c)["data"]["evidence_ids"]) for c in cids)
    return {"keyword": keyword, "candidate_ids": cids, "score": min(score, 100.0), "facts": facts, "risks": risks, "gaps": gaps,
            "evidence_ids": opp_ev}


def run(ctx, inp):
    keywords, skipped = pick_keywords(ctx, inp.get("keywords"), inp.get("force", False))
    listings, lfid = [], None
    try:
        listings, lfid = ctx.adapters.shop.products()
    except Exception as error:
        ctx.log(f"Couldn't read our own store's products: {error}")
    found = []
    for kw in keywords:
        ctx.check()
        found.append(research_keyword(ctx, kw, listings, lfid))
    found.sort(key=lambda o: (-o["score"], o["keyword"]))
    out = []
    for rank, o in enumerate(found, 1):
        now = ctx.now()
        data = {k: o[k] for k in ("candidate_ids", "facts", "risks", "gaps", "evidence_ids")}
        row = ctx.store.one("opportunities", business_id=ctx.business_id, keyword=o["keyword"])
        if row:
            ctx.store.update("opportunities", row["id"], status="ranked", rank=rank, score=o["score"], data_json=data, updated_at=now)
            oid = row["id"]
        else:
            oid = ctx.store.insert("opportunities", business_id=ctx.business_id, keyword=o["keyword"], status="ranked", rank=rank,
                                   score=o["score"], data_json=data, created_at=now, updated_at=now, simulated=ctx.simulated)
        for cid in o["candidate_ids"]:
            ctx.store.update("candidates", cid, opportunity_id=oid)
        out.append({"keyword": o["keyword"], "opportunity_id": oid, "rank": rank, "score": float(o["score"]),
                    "candidate_ids": o["candidate_ids"], "facts": o["facts"], "risks": o["risks"], "gaps": o["gaps"]})
    return {"opportunities": out, "skipped": skipped}
