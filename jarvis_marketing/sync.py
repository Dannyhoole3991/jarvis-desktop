"""
Multi-platform safeguards: stock, prices, duplicates, SKU mapping and orders.

`health()` looks for the problems that hurt multichannel sellers:
    selling stock the supplier no longer has; a price that no longer covers cost and fees; the same
    product listed twice (same supplier product or same SKU on two records); variants that don't map to a
    supplier variant; supplier data too old to trust; a platform listing with no reliable stock/price/order
    sync; a marketplace listing whose dropshipping conditions aren't confirmed.

It never fixes these by changing prices, stock or listings: price changes, pausing a live listing, supplier
orders and refunds stay behind Danny's approval (and price changes/purchases are off in code). It reports
what's wrong and what to do.

`route_order()` decides what happens to one incoming order line: forward through the supplier's own
integration (Shopify + CJ app) or HOLD for Danny. Jarvis itself never places a supplier order.
"""

from jarvis_marketing import platforms as P

STALE_S = 3 * 86400
LIVE_STAGES = ("submitted", "approved", "live")


def health(store, business_id, econ_cfg, now):
    problems = []

    def add(kind, product, text, action):
        problems.append({"kind": kind, "product_id": product["id"] if product else None, "title": product["title"] if product else None,
                         "text": text, "action": action})
    products = store.find("catalog", business_id=business_id, limit=5000)
    by_spu, by_sku = {}, {}
    for p in products:
        d = p["data"]
        sup = d.get("supplier") or {}
        if sup.get("spu"):
            by_spu.setdefault(sup["spu"].upper(), []).append(p)
        for v in d["variants"]:
            if v.get("sku"):
                by_sku.setdefault(v["sku"].upper(), []).append(p)
        if not sup and d.get("supplier_error"):
            add("supplier_lookup_failed", p, d["supplier_error"] + ".",
                "Danny: check the product in CJ (it may have been removed) and correct the SKUs.")
            continue
        if not sup and d.get("supplier_pending"):
            add("pending_check", p, f"Supplier check queued for CJ product {d['supplier_pending']} (CJ allows one request a second, "
                                    "so ~10 products are checked per run).", "None: it's checked on a coming run.")
            continue
        if not sup:
            add("unverified_supplier", p, "No supplier linked: cost and stock can't be checked.", "Link the product's SKUs to its CJ product.")
            continue
        if not sup.get("verified_at") or now - sup["verified_at"] > STALE_S:
            add("stale", p, "Supplier data is over 3 days old.", "Refreshed automatically on the next run.")
        if p["status"] == "live":
            empty = [v["sku"] for v in d["variants"] if v.get("stock") == 0]
            if empty:
                add("oversell_risk", p, f"Live on the store but the supplier shows 0 stock for {', '.join(empty[:4])}.",
                    "Danny: mark those variants unavailable in Shopify (Jarvis won't change a live listing without approval).")
            ship = (sup.get("shipping") or {}).get("usd")
            for v in d["variants"]:
                if v.get("supplier_cost_usd") is not None and ship is not None:
                    e = P.channel_economics("shopify", float(v["price"]), v["supplier_cost_usd"] * econ_cfg["usd_to_business"],
                                            ship * econ_cfg["usd_to_business"], econ_cfg)
                    if e["contribution"] < 0:
                        add("price_below_cost", p, f"{v['sku']} at {v['price']:.2f} loses about {-e['contribution']:.2f} per order (estimate).",
                            "Danny: review the price (price changes need your approval).")
        unmapped = [v["sku"] or "(none)" for v in d["variants"] if not v.get("supplier_vid")]
        if unmapped:
            add("sku_mapping", p, f"Variants not mapped to a supplier variant: {', '.join(unmapped[:5])}.",
                "Danny: correct the SKUs so orders reach the right CJ variant.")
    for spu, ps in by_spu.items():
        if len(ps) > 1:
            add("duplicate", ps[0], f"Supplier product {spu} appears in {len(ps)} records: {', '.join(x['title'][:30] for x in ps)}.",
                "Danny: keep one listing; duplicates split reviews and risk double-selling stock.")
    for sku, ps in by_sku.items():
        if len({x["id"] for x in ps}) > 1:
            add("duplicate_sku", ps[0], f"SKU {sku} is used by more than one product.", "Danny: give each variant a unique SKU.")
    for cl in store.find("channel_listings", business_id=business_id, limit=20000):
        if cl["stage"] in LIVE_STAGES:
            pf = P.PLATFORMS[cl["platform"]]
            prod = store.get("catalog", cl["product_id"])
            if not pf["sync"]["reliable"]:
                add("no_sync", prod, f"Listed on {pf['name']} without reliable stock/price/order sync.", "Pause it there until an integration is connected.")
            if pf["dropshipping"] in (P.CONDITIONAL, P.UNCLEAR) and pf.get("cj_compatible") is not True:
                add("policy", prod, f"{pf['name']}: dropshipping conditions not confirmed.", "Danny: confirm the platform's rules.")
    return {"ok": not problems, "problems": problems, "products": len(products), "checked_at": now,
            "summary": "No stock, price, duplicate or mapping problems found." if not problems else f"{len(problems)} problem(s) need attention."}


def route_order(store, business_id, order, platform):
    """What to do with one order. Returns {"decision": "forward"|"hold", "reasons": [...]} and never buys anything."""
    reasons = []
    pf = P.PLATFORMS.get(platform)
    if not pf or not pf["sync"]["reliable"]:
        reasons.append(f"No authorised order integration for {platform}.")
    products = store.find("catalog", business_id=business_id, limit=5000)
    sku_map = {v["sku"].upper(): (p, v) for p in products for v in p["data"]["variants"] if v.get("sku")}
    for line in order.get("lines") or []:
        hit = sku_map.get(str(line.get("sku") or "").upper())
        if not hit:
            reasons.append(f"SKU {line.get('sku')} isn't in the catalogue.")
            continue
        p, v = hit
        if not v.get("supplier_vid"):
            reasons.append(f"SKU {line['sku']} isn't mapped to a supplier variant.")
        if v.get("stock") is not None and v["stock"] < (line.get("qty") or 1):
            reasons.append(f"Supplier stock for {line['sku']} is {v['stock']}, the order needs {line.get('qty') or 1}.")
    if reasons:
        return {"decision": "hold", "reasons": reasons, "action": "Danny reviews: contact the customer, cancel/refund (your approval) or find stock."}
    return {"decision": "forward", "reasons": ["Mapped and in stock: CJ's Shopify app forwards it once the product is connected in CJ."],
            "action": "none (Jarvis never places supplier orders itself)"}
