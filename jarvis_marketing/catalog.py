"""
The central product record: ONE verified record per product, from which every platform's listing is
prepared. Platform listings may reword, shorten or reformat it; they may never change what the product is.

Built from two sources:
    shopify_live   our ACTIVE Shopify products (read-only), linked back to the supplier by the CJ
                   product code at the start of each SKU and re-checked there
    jarvis_draft   products the listing agent prepared (already supplier-verified)

Each section records when it was last verified and from which recorded read (fetch id), so a stale
or unproven field is visible, never assumed. Missing facts stay missing: no GTIN is invented, no
brand is made up, no spec is guessed.
"""

import re

from jarvis_marketing import claims, evidence as E

STALE_SUPPLIER_S = 3 * 86400          # supplier data older than this is re-read before any listing is prepared
ELECTRICAL = re.compile(r"(?i)\b(usb|led|rechargeable|charging|charger|battery|batteries|electric|wireless|bluetooth|plug|mains)\b")
NEGATED = re.compile(r"(?i)\b(no|without|not|non|doesn't need|does not need|zero)\W+(\w+\W+)?$")
# Words that need a human look. "own" = a problem even on our own store/Google/Meta/Pinterest; "marketplace" = marketplaces'
# stricter category rules (blades, food-contact, cosmetics, children's products, lithium batteries).
RESTRICTED = {
    "own": re.compile(r"(?i)\b(weapon|pepper spray|vape|e-cig|supplement|medical|medicine|cure|treat(s|ment)?)\b"),
    "marketplace": re.compile(r"(?i)\b(knife|knives|blade|razor|cosmetic|serum|lotion|food|edible|baby|infant|toy|children|kids|lithium)\b"),
}


def _mentions(pattern, text):
    """Matches that aren't negated ('no batteries needed' is not an electrical item)."""
    return sorted({m.group(0).lower() for m in pattern.finditer(text) if not NEGATED.search(text[max(0, m.start() - 24):m.start()])})


def spu_from_sku(sku):
    sku = (sku or "").strip()
    up = sku.upper()
    m = re.match(r"^(CJ[A-Z]+\d{7})", up)                  # classic CJ code: letters + 7 digits, then the variant suffix
    if m:
        return m.group(1)
    m = re.match(r"^(CJ[A-Z]+\d+)(?:-|$)", up)             # newer style (e.g. CJJZSNSN00770-Ash): the code ends at the first dash
    if m:
        return m.group(1)
    if up.startswith("MOCK-"):                             # simulated SKUs: <product id>-<colour>
        return sku.rsplit("-", 1)[0]
    return sku.split("-", 1)[0] or None


def text_of(html):
    t = re.sub(r"</(p|li|h\d|div)>", "\n", html or "", flags=re.I)
    return re.sub(r"[ \t]+", " ", re.sub(r"<[^>]+>", " ", t)).strip()


def specs_from(text):
    """Only label: value pairs the copy actually states (e.g. 'Material: ABS')."""
    out = {}
    for m in re.finditer(r"(?im)^\s*([A-Za-z][A-Za-z ]{2,24}):\s*([^\n]{1,120})$", text):
        out[m.group(1).strip().lower()] = m.group(2).strip()
    return out


def _supplier(ctx, spu):
    """Fresh supplier facts for a product code: details, cheapest UK shipping, stock per variant (first 10)."""
    sup = ctx.adapters.supplier
    d, dfid = sup.details_by_spu(spu)
    variants = d.get("variants") or []
    listed, lfid = None, None
    if d.get("name"):                       # popularity at the supplier: the same "listed by N stores" figure research uses
        try:
            items, lfid = sup.search(d["name"][:60])
            hit = next((i for i in items if str(i["id"]) == str(d.get("pid"))), None)
            listed = hit["listed"] if hit else None
        except Exception:
            listed = None
    ship, sfid = None, None
    if variants:
        opts, sfid = sup.shipping(variants[0]["vid"])
        ship = min(opts, key=lambda o: o["usd"]) if opts else None
    stocks = {}
    for v in variants[:10]:
        try:
            s, _ = sup.stock(v["vid"])
            stocks[v["vid"]] = s["stock"]
        except Exception:                              # unknown stays unknown
            stocks[v["vid"]] = None
    return {"name": sup.name, "pid": d.get("pid"), "spu": d.get("spu") or spu, "title": d.get("name"), "material": d.get("material"),
            "images": d.get("images") or [], "shipping": ship, "verified_at": ctx.now(), "fetch_ids": [x for x in (dfid, sfid, lfid) if x],
            "listed": listed,
            "variants": [{"vid": v["vid"], "sku": v.get("sku") or "", "name": v.get("name"), "cost_usd": v.get("price_usd"),
                          "stock": stocks.get(v["vid"])} for v in variants]}


def _match_variant(sku, supplier):
    for v in supplier["variants"]:
        if v["sku"] and v["sku"].upper() == (sku or "").upper():
            return v
    return None


def _compliance(title, text):
    blob = f"{title} {text_of(text)}"
    found = _mentions(RESTRICTED["own"], blob)
    marketplace = _mentions(RESTRICTED["marketplace"], blob)
    electrical = bool(_mentions(ELECTRICAL, blob))
    try:
        claims.check(text_of(text), allowed_numbers=[float(n) for n in re.findall(r"\d+(?:\.\d+)?", blob)])
        claims_ok, claims_note = True, "no banned or unsupported claims in the copy"
    except claims.UnsupportedClaim as why:
        claims_ok, claims_note = False, str(why)
    return {"restricted_terms": found, "marketplace_terms": marketplace, "electrical": electrical, "claims_ok": claims_ok, "claims_note": claims_note,
            "notes": (["Electrical item: marketplaces ask for UK safety marking (UKCA/CE) and the responsible manufacturer's details."]
                      if electrical else [])}


def record_from_shopify(ctx, p, supplier, product_fid):
    text = text_of(p["description_html"])
    variants = []
    for v in p["variants"]:
        sv = _match_variant(v["sku"], supplier) if supplier else None
        variants.append({"sku": v["sku"], "price": v["price"], "options": v["options"], "barcode": v["barcode"] or None,
                         "supplier_vid": sv["vid"] if sv else None, "supplier_cost_usd": sv["cost_usd"] if sv else None,
                         "stock": sv["stock"] if sv else None})
    specs = specs_from(text)
    claims_ok = [x for x in ([f"Material: {supplier['material']}"] if supplier and supplier.get("material") else [])]
    if supplier and supplier.get("shipping"):
        s = supplier["shipping"]
        claims_ok.append(f"UK delivery usually {s['min_days']}-{s['max_days']} days")
    gtins = [v["barcode"] for v in variants if v["barcode"]]
    return {"title": p["title"], "description_html": p["description_html"], "description_text": text, "specs": specs,
            "images": p["images"], "videos": [], "handle": p["handle"], "url": p["url"], "shopify_ref": p["ref"],
            "shopify_status": p["status"], "product_type": p["product_type"], "vendor": p["vendor"], "tags": p["tags"],
            "supplier": supplier, "variants": variants,
            "identifiers": {"gtin": gtins or None, "mpn": None, "brand": None,
                            "brand_note": f"Shopify vendor is '{p['vendor']}' (our store name), not a manufacturer brand",
                            "identifier_exists": bool(gtins)},
            "compliance": _compliance(p["title"], p["description_html"]), "approved_claims": claims_ok,
            "evidence": {"shopify_fetch_id": product_fid, "supplier_fetch_ids": (supplier or {}).get("fetch_ids", []),
                         "last_verified": {"shopify": ctx.now(), "supplier": (supplier or {}).get("verified_at")}}}


def record_from_listing(ctx, row, cand):
    plan = row["data"]
    sup_vars = {v["vid"]: v for v in plan.get("variants") or []}
    stock_detail = ((row["checks"] or {}).get("stock") or {}).get("detail", "")
    supplier = {"name": cand["supplier"], "pid": cand["supplier_product_id"], "spu": plan.get("spu"), "title": cand["title"],
                "material": (plan.get("facts") or {}).get("material") or cand["data"].get("material"), "images": plan.get("images") or [],
                "shipping": plan.get("shipping"), "verified_at": row["updated_at"],
                "fetch_ids": [x for x in (plan.get("supplier_fetch_id"), plan.get("plan_fetch_id")) if x],
                "variants": [{"vid": v["vid"], "sku": v["sku"], "name": " / ".join(v["options"].values()), "cost_usd": v["cost_usd"],
                              "stock": None} for v in sup_vars.values()], "stock_note": stock_detail}
    text = text_of(plan["description_html"])
    claims_ok = []
    if supplier["material"]:
        claims_ok.append(f"Material: {supplier['material']}")
    if supplier["shipping"]:
        claims_ok.append(f"UK delivery usually {supplier['shipping']['min_days']}-{supplier['shipping']['max_days']} days")
    m = re.search(r"\[(.*)\]", stock_detail)
    stocks = [int(x) if x.strip().isdigit() else None for x in m.group(1).split(",")] if m else []
    variants = []
    for i, v in enumerate(plan.get("variants") or []):
        variants.append({"sku": v["sku"], "price": float(v["price"]), "options": v["options"], "barcode": None, "supplier_vid": v["vid"],
                         "supplier_cost_usd": v["cost_usd"], "stock": stocks[i] if i < len(stocks) else None})
    return {"title": plan["title"], "description_html": plan["description_html"], "description_text": text, "specs": specs_from(text),
            "images": plan.get("images") or [], "videos": [], "handle": row["handle"], "url": row["url"],
            "shopify_ref": row["product_ref"], "shopify_status": "ACTIVE" if row["status"] == "live_verified" else "DRAFT",
            "product_type": plan.get("product_type"), "vendor": "Collective", "tags": plan.get("tags") or [],
            "supplier": supplier, "variants": variants,
            "identifiers": {"gtin": None, "mpn": None, "brand": None, "brand_note": "generic supplier product: no manufacturer brand recorded",
                            "identifier_exists": False},
            "compliance": _compliance(plan["title"], plan["description_html"]), "approved_claims": claims_ok,
            "evidence": {"listing_id": row["id"], "candidate_id": cand["id"], "supplier_fetch_ids": supplier["fetch_ids"],
                         "last_verified": {"supplier": row["updated_at"], "shopify": row["updated_at"]}}}


def _videos(ctx, cand_id):
    """Only QA-passed renders with a real video file are approved video assets."""
    if not cand_id:
        return []
    return [{"creative_id": c["id"], "file": (c["files"] or {}).get("video"), "creative_uid": c["creative_uid"]}
            for c in ctx.store.find("creatives", business_id=ctx.business_id, candidate_id=cand_id, status="qa_passed")
            if (c["files"] or {}).get("video")]


def _demand(ctx, cand_id, spu, title, supplier=None):
    out = {"supplier_listed": (supplier or {}).get("listed"), "ads_analysed": 0, "own_orders": None, "label": "fact where present"}
    if cand_id:
        cand = ctx.store.get("candidates", cand_id)
        f = E.facts(ctx.store, "candidate", f"{cand['supplier']}:{cand['supplier_product_id']}")
        out["supplier_listed"] = f.get("supplier_listed") or out["supplier_listed"]
        out["ads_analysed"] = len(ctx.store.find("ads", business_id=ctx.business_id, keyword=cand["data"].get("keyword") or "", limit=500))
    orders = ctx.store.find("orders", business_id=ctx.business_id, limit=5000)
    if orders:
        out["own_orders"] = sum(1 for o in orders if any(title.lower()[:25] in str(i.get("title", "")).lower() for i in o["items"] or []))
    return out


def _upsert(ctx, key, source, title, status, data, verified_at):
    row = ctx.store.one("catalog", business_id=ctx.business_id, key=key)
    if row:
        ctx.store.update("catalog", row["id"], title=title, status=status, data_json=data, verified_at=verified_at, updated_at=ctx.now())
        return row["id"]
    return ctx.store.insert("catalog", business_id=ctx.business_id, key=key, source=source, title=title, status=status, data_json=data,
                            verified_at=verified_at, created_at=ctx.now(), updated_at=ctx.now(), simulated=ctx.simulated)


def refresh(ctx, max_supplier_checks=10):
    """Builds/updates the central records. Supplier data is re-read when older than STALE_SUPPLIER_S, a few per run."""
    notes, made, checked = [], [], 0
    for row in ctx.store.find("listings", business_id=ctx.business_id, status=["awaiting_approval", "approved", "live_verified", "live_unverified"]):
        cand = ctx.store.get("candidates", row["candidate_id"])
        data = record_from_listing(ctx, row, cand)
        data["videos"] = _videos(ctx, cand["id"])
        data["demand"] = _demand(ctx, cand["id"], data["supplier"]["spu"], data["title"])
        if ctx.business.get("brand"):
            data["identifiers"].update(brand=ctx.business["brand"], brand_note=f"'{ctx.business['brand']}' set as our own brand by Danny")
        key = f"shopify:{row['product_ref']}"
        made.append(_upsert(ctx, key, "jarvis_draft", data["title"], "live" if row["status"] == "live_verified" else "draft", data,
                            data["supplier"]["verified_at"]))
    try:
        products, pfid = ctx.adapters.shop_admin.active_products()
    except Exception as error:
        notes.append(f"Couldn't read the store's live products: {str(error)[:200]}")
        products, pfid = [], None
    for p in products:
        ctx.check()
        key = f"shopify:{p['ref']}"
        existing = ctx.store.one("catalog", business_id=ctx.business_id, key=key)
        if existing and existing["source"] == "jarvis_draft":
            continue
        spu = next((spu_from_sku(v["sku"]) for v in p["variants"] if v["sku"]), None)
        supplier = (existing or {}).get("data", {}).get("supplier") if existing else None
        stale = not supplier or not supplier.get("verified_at") or ctx.now() - supplier["verified_at"] > STALE_SUPPLIER_S \
            or "listed" not in supplier
        lookup_error = None
        if spu and stale and checked < max_supplier_checks:
            checked += 1
            try:
                supplier = _supplier(ctx, spu)
            except Exception as error:
                lookup_error = f"supplier code {spu} couldn't be read ({str(error)[:120]})"
                notes.append(f"{p['title'][:40]}: {lookup_error}")
                supplier = None
        elif not spu:
            notes.append(f"{p['title'][:40]}: no supplier SKU, so cost/stock can't be verified")
        data = record_from_shopify(ctx, p, supplier, pfid)
        if not supplier and lookup_error:
            data["supplier_error"] = lookup_error  # tried and failed: reported as a problem, not as "queued"
        elif not supplier and spu:
            data["supplier_pending"] = spu         # queued: CJ allows one request a second, so a few products per run
        cand = ctx.store.one("candidates", business_id=ctx.business_id, supplier_product_id=(supplier or {}).get("pid") or "")
        data["videos"] = _videos(ctx, cand["id"] if cand else None)
        data["demand"] = _demand(ctx, cand["id"] if cand else None, spu, p["title"], supplier)
        brand = ctx.business.get("brand")
        if brand:
            data["identifiers"].update(brand=brand, brand_note=f"'{brand}' set as our own brand by Danny (businesses.json)")
        made.append(_upsert(ctx, key, "shopify_live", p["title"], "live", data, (supplier or {}).get("verified_at")))
    return made, notes
