"""
Channels agent: where should each product be sold, and is it ready there?

  1. refresh the central product records (catalog.refresh): live store products + Jarvis's drafts,
     linked back to the supplier and re-checked when stale;
  2. assess every product on every platform (readiness.assess): fixes, mandatory checks, profit after
     that platform's fees, READY / NEEDS_FIXES / REJECTED, and a recommendation with reasons;
  3. prepare the Google feed rows for READY products (written to a file for review; NOT submitted);
  4. run the stock/price/duplicate/order safeguards (sync.health).

Nothing is published or submitted here. A platform listing only moves past "assessed" through Danny's
approval and an authorised integration; none of the non-Shopify platforms is connected yet, and their
statuses say so instead of guessing.
"""

import csv
import os
import time

from jarvis_marketing import catalog, config, platforms as P, readiness as R, sync

INPUT = {}
OUTPUT = {"products": int, "assessed": int, "ready": [str], "needs_fixes": int, "rejected": int, "feed_rows": int,
          "sync_problems": int, "notes": [str]}


def google_statuses(adapters):
    mc = getattr(adapters, "merchant", None)
    sc = getattr(adapters, "search_console", None)
    why_mc = getattr(mc, "unavailable_reason", None) or "Merchant Center not connected"
    why_sc = getattr(sc, "unavailable_reason", None) or "Search Console not connected"
    return {"submission": {"value": None, "note": f"unknown: {why_mc}"}, "approval": {"value": None, "note": f"unknown: {why_mc}"},
            "indexing": {"value": None, "note": f"unknown: {why_sc}"}, "performance": {"value": None, "note": f"unknown: {why_sc}"},
            "label": "unavailable (not invented)"}


def run(ctx, inp):
    made, notes = catalog.refresh(ctx)
    now, counts, ready, rows = ctx.now(), {"assessed": 0, "needs": 0, "rejected": 0}, [], []
    for pid in made:
        ctx.check()
        product = ctx.store.get("catalog", pid)
        for platform in P.PLATFORMS:
            res = R.assess(product, platform, ctx.economics, ctx.thresholds, now)
            ok, reasons = R.recommend(platform, res, ctx.thresholds)
            statuses = google_statuses(ctx.adapters) if platform == "google" else \
                {"listing": {"value": "live" if (platform == "shopify" and product["status"] == "live") else None,
                             "note": "from our store" if platform == "shopify" else f"not connected: {P.PLATFORMS[platform]['integration']['name']}"}}
            existing = ctx.store.one("channel_listings", product_id=pid, platform=platform)
            stage = existing["stage"] if existing and existing["stage"] not in ("assessed",) else "assessed"
            if platform == "shopify":
                stage = "live" if product["status"] == "live" else "draft"
            fields = dict(readiness=res["readiness"], score=res["score"], stage=stage, checks_json=res["checks"], fixes_json=res["fixes"],
                          escalations_json=res["escalations"], payload_json=res["payload"], economics_json=res["economics"],
                          statuses_json=statuses, recommended=ok, reasons_json=reasons, checked_at=now)
            if existing:
                ctx.store.update("channel_listings", existing["id"], **fields)
            else:
                ctx.store.insert("channel_listings", business_id=ctx.business_id, product_id=pid, platform=platform, simulated=ctx.simulated, **fields)
            counts["assessed"] += 1
            if res["readiness"] == R.READY:
                ready.append(f"{product['title'][:40]} @ {platform}")
                if platform == "google":
                    rows += R.google_feed_rows(product, res["payload"])
            elif res["readiness"] == R.NEEDS_FIXES:
                counts["needs"] += 1
            else:
                counts["rejected"] += 1
    path = os.path.join(config.data_dir(ctx.business["profile"]), "google_feed_prepared.tsv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        cols = ["id", "item_group_id", "title", "description", "link", "image_link", "additional_image_link", "availability", "price",
                "condition", "brand", "gtin", "identifier_exists", "google_product_category", "color", "size", "shipping"]
        w = csv.DictWriter(f, fieldnames=cols, delimiter="\t")
        w.writeheader()
        for r in rows:
            w.writerow({k: str(v).replace("\t", " ").replace("\n", " ") for k, v in r.items()})
    problems = R.validate_feed_rows(rows)
    if problems:
        notes.append(f"{len(problems)} Google feed problems remain in READY rows (should be none): {problems[:3]}")
    h = sync.health(ctx.store, ctx.business_id, ctx.economics, now)
    period = time.strftime("%Y-%m-%d", time.gmtime(now))
    old = ctx.store.one("reports", business_id=ctx.business_id, kind="sync", period=period)
    if old:
        ctx.store.update("reports", old["id"], text=h["summary"], data_json=h, created_at=now)
    else:
        ctx.store.insert("reports", business_id=ctx.business_id, kind="sync", period=period, text=h["summary"], data_json=h, created_at=now)
    if h["problems"]:
        ctx.store.notify(f"Multichannel check: {h['summary']}", "needs_you" if any(p["kind"] in ("oversell_risk", "price_below_cost", "duplicate")
                                                                                 for p in h["problems"]) else "info", ctx.business_id)
    return {"products": len(made), "assessed": counts["assessed"], "ready": ready, "needs_fixes": counts["needs"], "rejected": counts["rejected"],
            "feed_rows": len(rows), "sync_problems": len(h["problems"]), "notes": notes}
