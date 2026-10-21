"""
Platform readiness: is ONE product ready for ONE platform? READY / NEEDS_FIXES / REJECTED.

For each product x platform:
  1. build that platform's listing from the central record (catalog.py), never changing what the
     product is: only wording length, format, attributes and which photos are sent;
  2. apply SAFE, verifiable fixes (shorten an over-long title at a word boundary, remove promotional
     words from a title, HTML -> plain text, drop non-https images, set identifier_exists=no when there's
     no barcode, derive availability from supplier stock) and record every one;
  3. run the mandatory checks again on the fixed listing.

A product is READY only if EVERY mandatory check passes; the score never overrides that. A check that
can't be passed by a safe fix is ESCALATED to Danny with what's needed. A platform that forbids the
business model, or a loss-making price, is REJECTED. READY never means "publish": publishing is a
separate, approved step, and only through an authorised integration.
"""

import re

from jarvis_marketing import platforms as P

READY, NEEDS_FIXES, REJECTED = "READY", "NEEDS_FIXES", "REJECTED"
SPECS = {    # listing limits per platform; Google's are from its own spec page, the rest are widely documented (unconfirmed)
    "shopify": {"title_max": 255, "desc_max": 65000, "images_min": 1, "images_max": 250, "plain": False, "gtin_required": False},
    "google": {"title_max": 150, "desc_max": 5000, "images_min": 1, "images_max": 11, "plain": True, "gtin_required": False, "feed": True},
    "meta_shops": {"title_max": 200, "desc_max": 9999, "images_min": 1, "images_max": 20, "plain": True, "gtin_required": False, "feed": True},
    "pinterest": {"title_max": 500, "desc_max": 10000, "images_min": 1, "images_max": 10, "plain": True, "gtin_required": False, "feed": True},
    "ebay_uk": {"title_max": 80, "desc_max": 500000, "images_min": 1, "images_max": 24, "plain": False, "gtin_required": False},
    "amazon_uk": {"title_max": 200, "desc_max": 2000, "images_min": 1, "images_max": 9, "plain": True, "gtin_required": True},
    "tiktok_shop_uk": {"title_max": 255, "desc_max": 10000, "images_min": 1, "images_max": 9, "plain": True, "gtin_required": False},
    "onbuy": {"title_max": 150, "desc_max": 10000, "images_min": 1, "images_max": 10, "plain": True, "gtin_required": True},
    "etsy": {"title_max": 140, "desc_max": 10000, "images_min": 1, "images_max": 10, "plain": True, "gtin_required": False},
}
PROMO = re.compile(r"(?i)\s*[-|,]?\s*\b(free (uk )?(delivery|shipping)|on sale|sale|best ?seller|cheapest|\d+% off|limited offer|hot deal)\b!*|!{2,}")
GOOGLE_CATEGORY = [  # suggestion only (by product words); the final category is a human check
    (r"cable|cord|wire", "Home & Garden > Household Supplies > Storage & Organization"),
    (r"organi[sz]er|storage|caddy|rack|holder|hook|basket", "Home & Garden > Household Supplies > Storage & Organization"),
    (r"chopper|peeler|kitchen|oil sprayer|utensil", "Home & Garden > Kitchen & Dining > Kitchen Tools & Utensils"),
    (r"hair ?brush|comb", "Health & Beauty > Personal Care > Hair Care > Combs & Brushes"),
    (r"pet hair|lint", "Home & Garden > Household Supplies > Household Cleaning Supplies"),
    (r"light|lamp|led", "Home & Garden > Lighting"),
]


def _cut(text, n):
    if len(text) <= n:
        return text
    cut = text[:n + 1].rsplit(" ", 1)[0].rstrip(" -,|/")
    return cut if cut else text[:n]


def _category(title):
    for pattern, path in GOOGLE_CATEGORY:
        if re.search(pattern, title, re.I):
            return path
    return None


def build_payload(product, platform):
    """The platform's listing from the central record (before fixes)."""
    d = product["data"]
    base = {"title": d["title"], "description": d["description_html"], "images": list(d["images"]), "condition": "new",
            "link": d.get("url"), "brand": d["identifiers"].get("brand"), "category": _category(d["title"]),
            "variants": [{"sku": v["sku"], "price": v["price"], "options": v["options"], "gtin": v.get("barcode"),
                          "supplier_vid": v.get("supplier_vid"), "stock": v.get("stock")} for v in d["variants"]]}
    if SPECS[platform].get("feed"):
        base["identifier_exists"] = None
        base["availability"] = None
        base["item_group_id"] = (d.get("supplier") or {}).get("spu") if len(d["variants"]) > 1 else None
        base["shipping"] = "GB:::0.00 GBP"
    return base


def apply_fixes(payload, platform, product):
    """Safe, verifiable corrections only. Returns (payload, fixes)."""
    spec, fixes = SPECS[platform], []

    def fix(rule, field, before, after):
        fixes.append({"rule": rule, "field": field, "before": str(before)[:200], "after": str(after)[:200]})
    if platform != "shopify":
        clean = PROMO.sub("", payload["title"]).strip(" -|,")
        if clean != payload["title"]:
            fix("promotional words aren't allowed in titles", "title", payload["title"], clean)
            payload["title"] = clean
    if len(payload["title"]) > spec["title_max"]:
        new = _cut(payload["title"], spec["title_max"])
        fix(f"title over {spec['title_max']} characters, shortened at a word boundary", "title", payload["title"], new)
        payload["title"] = new
    if spec["plain"] and "<" in payload["description"]:
        from jarvis_marketing.catalog import text_of
        new = text_of(payload["description"])
        fix("this platform takes plain text", "description", "(HTML)", new[:80] + "...")
        payload["description"] = new
    if len(payload["description"]) > spec["desc_max"]:
        new = _cut(payload["description"], spec["desc_max"])
        fix(f"description over {spec['desc_max']} characters, shortened", "description", f"{len(payload['description'])} chars", f"{len(new)} chars")
        payload["description"] = new
    ok_prefix = ("https://", "mock://") if product.get("simulated") else ("https://",)     # SIMULATED products keep their mock images
    bad = [u for u in payload["images"] if not str(u).startswith(ok_prefix)]
    if bad:
        fix("images must be https links", "images", bad, "removed")
        payload["images"] = [u for u in payload["images"] if u not in bad]
    if len(payload["images"]) > spec["images_max"]:
        fix(f"at most {spec['images_max']} images", "images", len(payload["images"]), spec["images_max"])
        payload["images"] = payload["images"][: spec["images_max"]]
    if "identifier_exists" in payload and not any(v.get("gtin") for v in payload["variants"]):
        if payload["identifier_exists"] is not False:
            fix("no barcode (GTIN) and no MPN on record: declare identifier_exists = no rather than invent one", "identifier_exists",
                payload["identifier_exists"], "no")
            payload["identifier_exists"] = False
    if "availability" in payload:
        known = [v["stock"] for v in payload["variants"] if v.get("stock") is not None]
        avail = None if not known else ("in_stock" if max(known) > 0 else "out_of_stock")
        if avail and payload["availability"] != avail:
            fix("availability follows the supplier's stock", "availability", payload["availability"], avail)
            payload["availability"] = avail
    return payload, fixes


def check(product, platform, payload, econ_cfg, thresholds, now):
    """The mandatory checks on the (fixed) listing. Returns (checks, economics)."""
    d, pf, spec = product["data"], P.PLATFORMS[platform], SPECS[platform]
    sup = d.get("supplier") or {}
    out = []

    def add(key, label, ok, detail, mandatory=True, blocking=False, needs=None):
        out.append({"key": key, "label": label, "ok": bool(ok), "mandatory": mandatory, "blocking": bool(blocking and not ok),
                    "detail": detail, "needs": None if ok else needs})
    # 1. eligibility + dropshipping
    ds = pf["dropshipping"]
    if ds == P.NOT_ALLOWED:
        add("eligibility", "Platform allows this business model", False, f"{pf['name']}: " + pf["dropshipping_facts"][0]["text"], blocking=True,
            needs="Not fixable: the platform's rules exclude these products.")
    elif ds == P.UNCLEAR:
        add("eligibility", "Platform allows this business model", False, "No reliable rule found: " + pf["dropshipping_facts"][0]["text"],
            needs=f"Danny: confirm in {pf['name']}'s seller terms whether supplier-fulfilled (CJ) orders are allowed in the UK.")
    elif ds == P.CONDITIONAL and pf.get("cj_compatible") is not True:
        add("eligibility", "Platform allows this business model", False,
            "Allowed with conditions: " + "; ".join(f["text"] for f in pf["dropshipping_facts"]), needs="Danny: " + pf.get("cj_note", ""))
    else:
        add("eligibility", "Platform allows this business model", True, "; ".join(f["text"] for f in pf["dropshipping_facts"]))
    # 2. policy / compliance
    comp = d.get("compliance") or {}
    marketplace = pf["kind"] == "marketplace"
    terms = list(comp.get("restricted_terms") or []) + (list(comp.get("marketplace_terms") or []) if marketplace else [])
    add("policy", "Product policy and claims", not terms and comp.get("claims_ok", False),
        ("words needing a policy check: " + ", ".join(terms) + "; " if terms else "") + comp.get("claims_note", ""),
        needs="Danny: check the item against the platform's restricted-products rules.")
    if marketplace and comp.get("electrical"):
        add("safety_docs", "Electrical safety information", False, "Electrical item: marketplaces ask for UK safety marking and manufacturer details.",
            needs="Danny: get UKCA/CE and manufacturer details from CJ before listing.")
    # 3. supplier
    fresh = sup.get("verified_at") and now - sup["verified_at"] <= 3 * 86400
    add("supplier", "Supplier verified recently", bool(sup) and fresh,
        f"{sup.get('name')} {sup.get('spu')} checked {round((now - sup['verified_at']) / 3600)}h ago" if sup.get("verified_at") else "no supplier on record",
        needs="Re-read the supplier (automatic on the next run) or link the product to its supplier.")
    unmapped = [v["sku"] or "(no SKU)" for v in d["variants"] if not v.get("supplier_vid")]
    add("sku_mapping", "Every variant maps to a supplier variant", not unmapped and bool(d["variants"]),
        "all variants mapped" if not unmapped else f"unmapped: {', '.join(unmapped[:5])}", needs="Danny: fix the SKUs so each matches a CJ variant.")
    known = [v["stock"] for v in d["variants"] if v.get("stock") is not None]
    add("stock", "Supplier has stock", bool(known) and max(known) >= thresholds["min_stock"],
        f"stock per variant: {[v.get('stock') for v in d['variants']][:8]}" if known else "supplier didn't report stock",
        needs="Check stock in CJ.")
    # 4. profit on this platform (weakest variant)
    fx, ship = econ_cfg["usd_to_business"], sup.get("shipping") or {}
    econs = []
    for v in d["variants"]:
        if v.get("supplier_cost_usd") is not None and ship.get("usd") is not None:
            econs.append(P.channel_economics(platform, float(v["price"]), v["supplier_cost_usd"] * fx, ship["usd"] * fx, econ_cfg))
    worst = min(econs, key=lambda e: e["contribution"]) if econs else None
    if worst is None:
        add("profit", f"Profitable after {pf['name']} fees", False, "costs unknown (no supplier cost or shipping)", needs="Link the product to its supplier.")
    else:
        add("profit", f"Profitable after {pf['name']} fees", worst["contribution"] >= thresholds["min_contribution"],
            f"weakest variant {worst['price']:.2f}: contribution {worst['contribution']:.2f} after {worst['platform_fees']:.2f} platform fees "
            f"(estimate; fee rates {worst['fee_status']})", blocking=worst["contribution"] < 0,
            needs="A higher price would be needed; price changes are Danny's decision.")
    # 5. data completeness
    missing = [k for k in ("title", "description", "link") if not payload.get(k)] + (["price"] if not all(v["price"] for v in payload["variants"]) else [])
    add("data", "Listing data complete", not missing and len(payload["description"]) >= 50,
        "complete" if not missing else f"missing: {', '.join(missing)}", needs="The product page must be live (published) first." if "link" in missing else "Fill the missing fields.")
    if spec["gtin_required"]:
        add("identifiers", "Barcodes (GTIN)", any(v.get("gtin") for v in payload["variants"]), "no barcodes on record",
            needs="Danny: buy GS1 barcodes or apply for the platform's GTIN exemption.")
    if platform == "google":
        add("brand", "Brand attribute", bool(payload.get("brand")), d["identifiers"].get("brand_note", "no brand on record"),
            needs="Danny: decide the brand. Google requires a real brand for new products; Jarvis won't present our store name as the "
                  "manufacturer. If you sell it as your own brand ('Collective'), say so and it will be used.")
        add("category", "Google product category", bool(payload.get("category")), payload.get("category") or "no category suggested",
            mandatory=False, needs="Pick a Google product category.")
    # 6. images
    add("images", "Images", len(payload["images"]) >= spec["images_min"],
        f"{len(payload['images'])} images" + (" (SIMULATED)" if product.get("simulated") else " (https)"),
        needs="Add genuine product photos.")
    # 7. sync, orders, tracking
    sync = pf["sync"]
    add("sync", "Stock and price stay in sync", sync["reliable"], f"stock: {sync['stock']}; price: {sync['price']}",
        needs=f"Connect {pf['integration']['name']} first: without it stock and prices would drift and orders could be oversold.")
    add("orders", "Orders and tracking flow back", sync["reliable"], f"orders: {sync['orders']}; tracking: {sync['tracking']}",
        needs="Not until an authorised integration is connected.")
    # 8. delivery and returns
    exp = (pf.get("delivery") or {}).get("max_days_expected")
    mx = ship.get("max_days")
    add("delivery", "Delivery promise achievable", exp is None or (mx is not None and mx <= exp),
        f"supplier {ship.get('min_days')}-{mx} days" + (f" vs platform expectation ~{exp} days (unconfirmed)" if exp else ""),
        needs="Slower than the platform expects: a UK warehouse or faster shipping option would be needed.")
    # 9. demand evidence
    dem = d.get("demand") or {}
    signals = [s for s in (f"{dem['supplier_listed']} stores list it at the supplier" if dem.get("supplier_listed") else None,
                           f"{dem['ads_analysed']} adverts analysed" if dem.get("ads_analysed") else None,
                           f"{dem['own_orders']} of our orders matched by title (estimate)" if dem.get("own_orders") else None) if s]
    add("demand", "Evidence of demand", bool(signals), "; ".join(signals) or "no demand evidence recorded for this product",
        needs="Run research for this product (supplier popularity, adverts, own sales).")
    return out, {"variants": econs, "weakest": worst}


def assess(product, platform, econ_cfg, thresholds, now):
    payload = build_payload(product, platform)
    payload, fixes = apply_fixes(payload, platform, product)
    checks, econ = check(product, platform, payload, econ_cfg, thresholds, now)
    payload, more = apply_fixes(payload, platform, product)        # re-run: fixes are idempotent, nothing new expected
    fixes += more
    mandatory = [c for c in checks if c["mandatory"]]
    if any(c["blocking"] for c in checks):
        status = REJECTED
    elif all(c["ok"] for c in mandatory):
        status = READY
    else:
        status = NEEDS_FIXES
    score = round(100 * sum(1 for c in checks if c["ok"]) / len(checks), 1)
    escalations = [{"check": c["key"], "label": c["label"], "needs": c["needs"], "detail": c["detail"]} for c in checks if not c["ok"] and c["needs"]]
    return {"readiness": status, "score": score, "checks": checks, "fixes": fixes, "escalations": escalations, "payload": payload,
            "economics": econ}


def recommend(platform, result, thresholds):
    """Should this product be offered on this platform? (A recommendation, never an action.)"""
    pf = P.PLATFORMS[platform]
    w = result["economics"]["weakest"]
    reasons = []
    if result["readiness"] == REJECTED:
        return False, [c["detail"] for c in result["checks"] if c["blocking"]][:2]
    if pf["kind"] in ("search_listing", "social_catalog", "own_store"):
        reasons.append("links to our own store, so no extra selling fee and stock/prices sync through Shopify")
    if w:
        reasons.append(f"about {w['contribution']:.2f} per order after this platform's fees (estimate)")
    blockers = [c["label"] for c in result["checks"] if c["mandatory"] and not c["ok"]]
    ok = bool(w and w["contribution"] >= thresholds["min_contribution"]) and pf["dropshipping"] != P.NOT_ALLOWED and \
        not any(c["key"] in ("eligibility", "sync") and not c["ok"] for c in result["checks"])
    if blockers:
        reasons.append("still needs: " + ", ".join(blockers[:4]))
    return ok, reasons


def google_feed_rows(product, payload):
    """Merchant Center rows (one per variant) from a Google payload. Field names follow Google's product data spec."""
    d, rows = product["data"], []
    for v in payload["variants"]:
        opts = v["options"] or {}
        rows.append({"id": v["sku"][:50], "item_group_id": payload.get("item_group_id") or "", "title": payload["title"],
                     "description": payload["description"], "link": payload["link"] or "", "image_link": (payload["images"] or [""])[0],
                     "additional_image_link": ",".join(payload["images"][1:11]), "availability": "in_stock" if (v.get("stock") or 0) > 0 else
                     ("out_of_stock" if v.get("stock") == 0 else payload.get("availability") or ""),
                     "price": f"{float(v['price']):.2f} GBP", "condition": "new", "brand": payload.get("brand") or "",
                     "gtin": v.get("gtin") or "", "identifier_exists": "no" if payload.get("identifier_exists") is False else "",
                     "google_product_category": payload.get("category") or "", "color": opts.get("Colour") or opts.get("Color") or "",
                     "size": opts.get("Size") or "", "shipping": payload.get("shipping") or ""})
    return rows


def validate_feed_rows(rows):
    """Google's documented rules for each row. Returns [(id, problem)] (empty = valid)."""
    spec, out = SPECS["google"], []
    for r in rows:
        if not r["id"] or len(r["id"]) > 50:
            out.append((r["id"], "id missing or over 50 characters"))
        if not r["title"] or len(r["title"]) > spec["title_max"]:
            out.append((r["id"], "title missing or over 150 characters"))
        if not r["description"] or len(r["description"]) > spec["desc_max"]:
            out.append((r["id"], "description missing or over 5000 characters"))
        if not r["link"].startswith("https://"):
            out.append((r["id"], "link must be the live https product page"))
        if not r["image_link"].startswith("https://"):
            out.append((r["id"], "image_link must be an https image"))
        if r["availability"] not in ("in_stock", "out_of_stock", "preorder", "backorder"):
            out.append((r["id"], "availability unknown"))
        if not re.fullmatch(r"\d+\.\d{2} [A-Z]{3}", r["price"]):
            out.append((r["id"], "price must look like '9.99 GBP'"))
        if not r["brand"] and r["identifier_exists"] != "no":
            out.append((r["id"], "brand missing (or declare identifier_exists = no)"))
        if not r["brand"]:
            out.append((r["id"], "brand missing: required for new products"))
        if PROMO.search(r["title"]):
            out.append((r["id"], "promotional text in the title"))
    return out
