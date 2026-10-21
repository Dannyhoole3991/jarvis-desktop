"""
Listing agent: verify the product, prepare the Shopify listing, create it as a DRAFT, check the draft,
and publish it only after Danny approves that exact listing.

  list     for each shortlisted product not yet in the store:
             1. re-read the supplier now (identity, variants, UK shipping, stock): research can be stale;
             2. build the listing with jarvis_store's own rules (photos, delivery cap, price cap, honest
                copy, real-cost pricing) and check every number in the copy against the supplier data;
             3. work out profit per order AFTER the planned ad cost; reject if it fails the thresholds;
             4. create the DRAFT (hidden from customers), read it back and compare field by field;
             5. ask Danny to approve publishing that exact listing.
  go_live  for an approved listing: re-check the draft is unchanged, consume the approval, publish,
           then read the PUBLIC page: only a page that loads, is in stock and shows the planned price
           counts as live. Until then no advert may point at it.
"""

import hashlib
import json
import re

from jarvis_marketing import claims, controls as C, evidence as E, scoring

INPUT = {"candidate_ids?": [int]}
OUTPUT = {"listings": [{"candidate_id": int, "listing_id": int, "status": str}], "rejected": [{"candidate_id": int, "reasons": [str]}],
          "notes": [str]}
LIVE_IN = {"listing_ids?": [int]}
LIVE_OUT = {"live": [{"listing_id": int, "status": str, "url": str}], "refused": [{"listing_id": int, "why": str}]}
OPEN = ("draft_verified", "awaiting_approval", "approved", "publishing", "live_verified", "live_unverified")


def _numbers(obj):
    return {round(float(n), 2) for n in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", json.dumps(obj, default=str).replace(",", " "))}


def _text(html):
    return " ".join(re.sub(r"<[^>]+>", " ", html or "").split())


def content_for(listing_data, product_ref):
    """Exactly what Danny approves. Any change to it needs a new approval."""
    return {"product_ref": product_ref, "title": listing_data["title"],
            "prices": sorted(round(float(v["price"]), 2) for v in listing_data["variants"]),
            "skus": sorted(v["sku"] for v in listing_data["variants"]), "images": list(listing_data["images"]),
            "description_sha256": hashlib.sha256(listing_data["description_html"].encode("utf-8")).hexdigest()}


def compare(listing, snap):
    """Field-by-field check of the draft Shopify holds against the listing that was meant. [] = matches."""
    if not snap:
        return ["Shopify has no such product."]
    problems = []
    if snap["status"] != "DRAFT":
        problems.append(f"status is {snap['status']}, expected DRAFT")
    if snap["title"] != listing["title"]:
        problems.append(f"title is '{snap['title']}'")
    want = sorted((v["sku"], f"{float(v['price']):.2f}") for v in listing["variants"])
    got = sorted((v["sku"], f"{float(v['price']):.2f}") for v in snap["variants"])
    if want != got:
        problems.append(f"variants/prices differ: {got} vs {want}")
    if snap["images"] < len(listing["images"]):
        problems.append(f"{snap['images']} images, expected {len(listing['images'])}")
    if "jarvis" not in snap["tags"]:
        problems.append("missing the jarvis tag")
    if _text(snap["description_html"])[:200] != _text(listing["description_html"])[:200]:
        problems.append("description differs")
    return problems


def verify_and_plan(ctx, cand):
    """Fresh supplier check + listing plan + economics. Returns (plan, checks, reasons_to_reject)."""
    sup, bid, key = ctx.adapters.supplier, ctx.business_id, f"{cand['supplier']}:{cand['supplier_product_id']}"
    reasons, checks = [], {}
    d, dfid = sup.details(cand["supplier_product_id"])
    checks["identity"] = {"ok": (d.get("pid") in (None, cand["supplier_product_id"])) and bool(d.get("name")),
                          "detail": f"supplier still lists '{d.get('name')}'"}
    try:
        plan, pfid = sup.listing_plan(cand["supplier_product_id"])
    except Exception as error:          # jarvis_store's own rules said no (photos, delivery, price cap ...)
        return None, checks, [f"Listing check failed: {error}"]
    old_imgs = set(cand["data"].get("images") or [])
    same_photos = set(plan["images"]) <= (old_imgs | set(d.get("images") or []))
    checks["photos"] = {"ok": same_photos and len(plan["images"]) >= ctx.thresholds["min_images"],
                        "detail": f"{len(plan['images'])} supplier photos" + ("" if same_photos else " (not the researched set)")}
    if not checks["photos"]["ok"]:
        reasons.append("Photos don't match the researched product or are too few.")
    stock_vals = []
    for v in plan["variants"][:5]:
        try:
            s, sfid = sup.stock(v["vid"])
            stock_vals.append(s["stock"])
        except Exception as error:
            ctx.log(f"stock read failed for {v['vid']}: {error}")
            stock_vals.append(None)
    known = [s for s in stock_vals if s is not None]
    checks["stock"] = {"ok": bool(known) and min(known) >= ctx.thresholds["min_stock"],
                       "detail": f"supplier stock per variant: {stock_vals}" if known else "supplier didn't report stock",
                       "unknown": not known}
    if known and min(known) < ctx.thresholds["min_stock"]:
        reasons.append("Supplier shows a variant out of stock.")
    ship = plan["shipping"]
    checks["delivery"] = {"ok": ship["max_days"] <= ctx.thresholds["max_delivery_days"],
                          "detail": f"{ship['name']}: {ship['min_days']}-{ship['max_days']} days, ${ship['usd']}"}
    if not checks["delivery"]["ok"]:
        reasons.append(f"Delivery up to {ship['max_days']} days is too slow.")
    allowed = _numbers(d) | _numbers(ship) | _numbers(plan.get("facts") or {}) | {round(float(v["price"]), 2) for v in plan["variants"]}
    try:
        claims.check(_text(plan["description_html"]), allowed)
        checks["copy"] = {"ok": True, "detail": "every number in the description is in the supplier's data; no banned claims"}
    except claims.UnsupportedClaim as why:
        checks["copy"] = {"ok": False, "detail": str(why)}
        reasons.append(f"Description: {why}")
    fx, econ_cfg = ctx.economics["usd_to_business"], ctx.economics
    fees = {k: econ_cfg[k] for k in ("payment_fee_pct", "payment_fee_fixed", "return_rate", "other_variable_cost")}
    worst = None
    for v in plan["variants"]:          # the weakest variant decides
        e = scoring.unit_economics(float(v["price"]), round(v["cost_usd"] * fx, 2), round(ship["usd"] * fx, 2), **fees)
        if worst is None or e["contribution"] < worst["contribution"]:
            worst = e
    cpa = econ_cfg["planned_cpa"]
    after = round(worst["contribution"] - cpa, 2)
    econ = dict(worst, planned_cpa=cpa, profit_after_ads=after, planned_cpa_label=E.ESTIMATE,
                basis=f"weakest variant; supplier USD x {fx}; planned ad cost {cpa:.2f} per order is an ESTIMATE until a real CPA exists")
    checks["profit"] = {"ok": worst["contribution"] >= ctx.thresholds["min_contribution"] and after >= ctx.thresholds["min_profit_after_ads"],
                        "detail": f"contribution {worst['contribution']:.2f}, after planned ad cost {after:.2f}"}
    if worst["contribution"] < ctx.thresholds["min_contribution"]:
        reasons.append(f"Contribution {worst['contribution']:.2f} is below {ctx.thresholds['min_contribution']:.2f}.")
    if after < ctx.thresholds["min_profit_after_ads"]:
        reasons.append(f"After a planned ad cost of {cpa:.2f}, profit {after:.2f} is below {ctx.thresholds['min_profit_after_ads']:.2f}.")
    E.add(ctx.store, bid, "candidate", key, E.ESTIMATE, "profit_after_ads", f"Profit after planned ad cost about {after:.2f} per order",
          value=after, basis=econ["basis"], confidence=0.4)
    plan = dict(plan, economics=econ, supplier_fetch_id=dfid, plan_fetch_id=pfid)
    return plan, checks, reasons


def run(ctx, inp):
    ids = inp.get("candidate_ids")
    if ids:
        cands = [ctx.store.get("candidates", i) for i in ids]
    else:          # products not in the store yet, best first, a few per cycle (Danny approves each one anyway)
        pool = [c for c in ctx.store.find("candidates", business_id=ctx.business_id, status=["shortlisted", "reserve"], order="score DESC")
                if not (c["data"].get("listing") or {}).get("url") and not ctx.store.one("listings", candidate_id=c["id"])]
        cands = pool[: ctx.limits["listings_per_cycle"]]
    made, rejected, notes = [], [], []
    for cand in cands:
        ctx.check()
        if (cand["data"].get("listing") or {}).get("url"):
            continue                                   # already in the store
        existing = ctx.store.one("listings", candidate_id=cand["id"])
        if existing and existing["status"] in OPEN:
            made.append({"candidate_id": cand["id"], "listing_id": existing["id"], "status": existing["status"]})
            continue
        plan, checks, reasons = verify_and_plan(ctx, cand)
        now = ctx.now()
        if reasons:
            data = plan or {}
            if existing:
                ctx.store.update("listings", existing["id"], status="rejected", data_json=data, checks_json=checks,
                                 error="; ".join(reasons)[:500], updated_at=now)
            else:
                ctx.store.insert("listings", business_id=ctx.business_id, candidate_id=cand["id"], status="rejected", data_json=data,
                                 checks_json=checks, error="; ".join(reasons)[:500], created_at=now, updated_at=now, simulated=ctx.simulated)
            ctx.store.update("candidates", cand["id"], status="rejected", reasons_json=(cand["reasons"] or []) + reasons, updated_at=now)
            rejected.append({"candidate_id": cand["id"], "reasons": reasons})
            continue
        ctx.external("create_draft")
        created = ctx.adapters.shop_admin.create_draft(plan)
        snap, _ = ctx.adapters.shop_admin.snapshot(created["product_ref"])
        problems = compare(plan, snap)
        checks["draft_matches"] = {"ok": not problems, "detail": "; ".join(problems) or "Shopify's draft matches the listing"}
        status = "draft_verified" if not problems else "draft_mismatch"
        fields = dict(status=status, data_json=dict(plan, admin_url=created["admin_url"]), checks_json=checks,
                      product_ref=created["product_ref"], handle=created["handle"], error="; ".join(problems) or None, updated_at=now)
        if existing:
            ctx.store.update("listings", existing["id"], **fields)
            lid = existing["id"]
        else:
            lid = ctx.store.insert("listings", business_id=ctx.business_id, candidate_id=cand["id"], created_at=now,
                                   simulated=ctx.simulated, **fields)
        ctx.store.audit("listing-agent", "create_draft", status, subject=f"listing:{lid}", business_id=ctx.business_id,
                        product_ref=created["product_ref"], simulated=ctx.simulated)
        if status == "draft_verified" and ctx.controls.allowed("request_approval"):
            content = content_for(plan, created["product_ref"])
            e = plan["economics"]
            stock_note = "" if not checks["stock"].get("unknown") else " Supplier didn't report stock: check it in CJ."
            aid = C.Approvals(ctx.store).request(
                ctx.business_id, "publish_product", "listing", lid, content,
                f"Publish '{plan['title']}' ({len(plan['variants'])} variant(s), {min(content['prices']):.2f}-{max(content['prices']):.2f}). "
                f"Weakest variant ({e['price']:.2f}) makes about {e['contribution']:.2f}/order, about {e['profit_after_ads']:.2f} after the "
                f"planned ad cost (estimates).{stock_note}", "listing-agent")
            ctx.store.update("listings", lid, status="awaiting_approval", approval_id=aid, content_hash=C.payload_hash(content))
            status = "awaiting_approval"
        elif problems:
            notes.append(f"{cand['title']}: the draft doesn't match ({'; '.join(problems)}); not offered for publishing")
        made.append({"candidate_id": cand["id"], "listing_id": lid, "status": status})
    return {"listings": made, "rejected": rejected, "notes": notes}


def go_live(ctx, inp):
    ids = inp.get("listing_ids")
    if not ids:
        recheck(ctx, inp)
    rows = [ctx.store.get("listings", i) for i in ids] if ids else ctx.store.find("listings", business_id=ctx.business_id, status="approved")
    live, refused = [], []
    for row in rows:
        ctx.check()
        if row["status"] != "approved":
            refused.append({"listing_id": row["id"], "why": f"listing is {row['status']}"})
            continue
        snap, _ = ctx.adapters.shop_admin.snapshot(row["product_ref"])
        problems = compare(row["data"], snap)
        content = content_for(row["data"], row["product_ref"])
        if problems or C.payload_hash(content) != row["content_hash"]:
            ctx.store.update("listings", row["id"], status="failed", error="Changed after approval: " + ("; ".join(problems) or "content"))
            refused.append({"listing_id": row["id"], "why": "the draft changed after approval; it needs a new approval"})
            continue
        try:
            ctx.external("publish_product", approval_id=row["approval_id"], content=content)
        except C.PermissionDenied as why:
            ctx.store.update("listings", row["id"], error=str(why)[:300])
            refused.append({"listing_id": row["id"], "why": str(why)})
            continue
        if not ctx.store.update("listings", row["id"], where={"status": "approved"}, status="publishing"):
            refused.append({"listing_id": row["id"], "why": "already being published"})
            continue
        try:
            out = ctx.adapters.shop_admin.publish(row["product_ref"], "Danny (approved in the dashboard)")
        except Exception as error:
            ctx.store.update("listings", row["id"], status="failed", error=f"publish failed: {str(error)[:250]}")
            refused.append({"listing_id": row["id"], "why": f"publish failed: {str(error)[:200]}"})
            continue
        page, pfid = ctx.adapters.shop.product_page(out.get("handle") or row["handle"])
        want = min(content["prices"])
        ok = bool(page and page.get("available") and page.get("price_min") is not None and abs(page["price_min"] - want) < 0.005)
        cand = ctx.store.get("candidates", row["candidate_id"])
        checks = dict(row["checks"] or {}, public_page={"ok": ok, "detail": (f"{page['url']} loads, in stock, {page['price_min']}" if ok else
                                                                             f"public page not right yet: {page or 'not found'}; channel: {out.get('channel')}")})
        if ok:
            key = f"{cand['supplier']}:{cand['supplier_product_id']}"
            E.add(ctx.store, ctx.business_id, "candidate", key, E.FACT, "store_listing", f"Listed in our store at {page['price_min']}",
                  value=page["price_min"], fetch_id=pfid, quote=E.item_quote(page), confidence=0.95)
            ctx.store.update("candidates", cand["id"], data_json=dict(cand["data"], listing=page), updated_at=ctx.now(),
                             **({"status": "shortlisted"} if cand["status"] in ("reserve", "shortlisted") else {}))
            ctx.store.update("listings", row["id"], status="live_verified", url=page["url"], checks_json=checks, error=None, updated_at=ctx.now())
            ctx.store.notify(f"'{row['data']['title']}' is live and checked: {page['url']}", "info", ctx.business_id)
            live.append({"listing_id": row["id"], "status": "live_verified", "url": page["url"]})
        else:
            ctx.store.update("listings", row["id"], status="live_unverified", checks_json=checks,
                             error="Published, but the public page didn't check out; no advert may use it yet.", updated_at=ctx.now())
            ctx.store.notify(f"'{row['data']['title']}' was set live but its public page didn't check out "
                             f"(channel: {out.get('channel')}). Please open it in Shopify and tick the Online Store channel.",
                             "needs_you", ctx.business_id)
            live.append({"listing_id": row["id"], "status": "live_unverified", "url": ""})
        ctx.store.audit("listing-agent", "publish_product", "ok" if ok else "unverified", subject=f"listing:{row['id']}",
                        business_id=ctx.business_id, simulated=ctx.simulated)
    return {"live": live, "refused": refused}


def recheck(ctx, inp):
    """Re-reads published-but-unverified pages (e.g. after Danny ticked the sales channel)."""
    for row in ctx.store.find("listings", business_id=ctx.business_id, status="live_unverified"):
        page, pfid = ctx.adapters.shop.product_page(row["handle"])
        if page and page.get("available"):
            cand = ctx.store.get("candidates", row["candidate_id"])
            E.add(ctx.store, ctx.business_id, "candidate", f"{cand['supplier']}:{cand['supplier_product_id']}", E.FACT, "store_listing",
                  f"Listed in our store at {page['price_min']}", value=page["price_min"], fetch_id=pfid, quote=E.item_quote(page), confidence=0.95)
            ctx.store.update("candidates", cand["id"], data_json=dict(cand["data"], listing=page), updated_at=ctx.now(),
                             **({"status": "shortlisted"} if cand["status"] in ("reserve", "shortlisted") else {}))
            ctx.store.update("listings", row["id"], status="live_verified", url=page["url"], error=None, updated_at=ctx.now())
