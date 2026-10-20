"""
Creative QA agent: an independent check of every render. It re-reads the brief, the product's facts
and the live product page itself, and never trusts production's own report.

Nine checks, each scored 1-5 with the reason. Product accuracy, offer/CTA, landing page, ad policy and
technical quality are CRITICAL: any of them under 3 rejects the creative. Scores are PREDICTIONS of
quality, never results. A reject comes with specific fixes; mechanical ones go back to production.
"""

import hashlib
import json
import os

from jarvis_marketing import claims, evidence as E
from jarvis_marketing.adapters import Unavailable

INPUT = {"creative_ids?": [int]}
OUTPUT = {"reviews": [{"creative_id": int, "passed": bool, "overall": float, "fixes": [str]}],
          "revise": [{"creative_id": int, "fixes": [str]}]}
CRITICAL = ("product_accuracy", "offer_cta", "captions", "landing_page", "policy", "technical")
CAPTION_MAX_CHARS = 32
MAX_DURATION_S = 60
PASS_OVERALL = 3.5


def technical(probe, spec):
    """Objective checks on a render's probe."""
    w, h = probe.get("width") or 0, probe.get("height") or 0
    dur, want = probe.get("duration_s") or 0, spec["duration_s"]
    return {"aspect_9_16": {"ok": bool(h) and abs(w / h - 9 / 16) < 0.01, "detail": f"{w}x{h}"},
            "resolution": {"ok": w >= 720 and h >= 1280, "detail": f"{w}x{h} (need at least 720x1280)"},
            "duration": {"ok": 0 < dur <= MAX_DURATION_S and abs(dur - want) <= max(2.0, 0.25 * want),
                         "detail": f"{dur}s for a {want}s brief"},
            "audio": {"ok": bool(probe.get("has_audio")), "detail": "audio track present" if probe.get("has_audio") else "no audio"},
            "export_quality": {"ok": (probe.get("bitrate_kbps") or 0) >= 1500, "detail": f"{probe.get('bitrate_kbps')} kbps"}}


def _score(ok, good=5, bad=1):
    return good if ok else bad


def review(ctx, creative):
    brief = ctx.store.get("briefs", creative["brief_id"])
    cand = ctx.store.get("candidates", creative["candidate_id"])
    b, spec = brief["data"], creative["spec"]
    scores, fixes, revise = {}, [], []

    def put(name, score, why):
        scores[name] = {"score": score, "why": why}

    # 1. product accuracy: same product, same photos, at every step
    images = cand["data"].get("images") or []
    expected = {"supplier": cand["supplier"], "supplier_product_id": cand["supplier_product_id"],
                "images_hash": hashlib.sha256(json.dumps(images).encode()).hexdigest()[:16]}
    ident = spec.get("product_identity") or {}
    same = all(ident.get(k) == v and b["product_identity"].get(k) == v for k, v in expected.items())
    only_own = set(spec["images"]) <= set(images) and bool(spec["images"])
    put("product_accuracy", _score(same and only_own), "same product and only its own photos" if same and only_own
        else "the render doesn't match the product's identity or uses other photos")
    if not (same and only_own):
        fixes.append("Rebuild from this product's own photos.")
    # 2. hook
    first = spec["scenes"][0]
    hook_ok = first["seconds"] <= 3.0 and len(b["hook"].split()) <= 10
    put("hook", 5 if hook_ok else 2, f"first shot {first['seconds']}s, hook {len(b['hook'].split())} words")
    if first["seconds"] > 3.0:
        fixes.append("Get to the hook within 3 seconds.")
        revise.append("hook_slow")
    # 3. pacing
    slow = [s["n"] for s in spec["scenes"] if s["seconds"] > 5.0]
    put("pacing", 5 if not slow else 3, "cuts every 5s or faster" if not slow else f"scenes {slow} hold too long")
    if slow or spec["duration_s"] > 30:
        revise.append("too_long")
    # 4. benefits grounded in facts
    f = E.facts(ctx.store, "candidate", f"{cand['supplier']}:{cand['supplier_product_id']}")
    grounded = [x for x in (cand["title"], f.get("material"), f.get("packing_list")) if x and any(str(x).lower() in v.lower() for v in spec["lines"])]
    put("benefits", 4 if grounded else 2, "names the product and its real details" if grounded else "no concrete product detail")
    # 5. offer and CTA: the price shown is the store's price
    listing_price = f.get("store_listing")
    offer_ok = bool(b.get("cta")) and b.get("price_stated") == listing_price and bool(b.get("destination"))
    put("offer_cta", _score(offer_ok), "price matches the store and there's a call to action with a link" if offer_ok
        else f"price {b.get('price_stated')} vs store {listing_price}, link {b.get('destination')}")
    if not offer_ok:
        fixes.append("Use the store's current price and product link.")
    # 6. captions / accessibility
    long_caps = [s["n"] for s in spec["scenes"] if len(s["on_screen_text"]) > CAPTION_MAX_CHARS]
    missing = [s["n"] for s in spec["scenes"] if not s["on_screen_text"].strip()]
    put("captions", 5 if not (long_caps or missing) else 2,
        "every scene has short on-screen text" if not (long_caps or missing) else f"too long: {long_caps}, missing: {missing}")
    if long_caps:
        fixes.append(f"Shorten on-screen text to {CAPTION_MAX_CHARS} characters (scenes {long_caps}).")
        revise.append("captions_too_long")
    # 7. landing page, read fresh now
    landing_ok, why = False, "product page not checked"
    handle = (cand["data"].get("listing") or {}).get("handle")
    if handle:
        try:
            page, _ = ctx.adapters.shop.product_page(handle)
            if page and page.get("available") and page.get("price_min") == b.get("price_stated") and page.get("url") == b.get("destination"):
                landing_ok, why = True, f"live page shows {page['price_min']}, in stock"
            else:
                why = "live page differs: " + (f"price {page.get('price_min')}, available {page.get('available')}" if page else "not found")
        except Unavailable as error:
            why = f"couldn't read the page ({error})"
    put("landing_page", _score(landing_ok), why)
    if not landing_ok:
        fixes.append("The product page must be live at the price in the video before this can go out.")
    # 8. policy and claims
    try:
        claims.check_all([b["hook"], b["caption"], *spec["lines"], *spec["on_screen"]], b["allowed_numbers"])
        put("policy", 5, "no banned or unsupported claims" + ("; AI-generated label will be switched on" if ctx.business.get("ai_label") else ""))
    except claims.UnsupportedClaim as error:
        put("policy", 1, str(error))
        fixes.append(f"Remove: {error}")
    # 9. technical, re-measured where there's a real file
    probe = (creative["checks"] or {}).get("probe") or {}
    path = (creative["files"] or {}).get("video")
    if path and os.path.exists(path):
        try:
            from jarvis_marketing.adapters.video import probe as ffprobe
            probe = ffprobe(path)
        except Exception as error:
            probe = {"error": str(error)}
    tech = technical(probe, spec) if probe and "error" not in probe else {"probe": {"ok": False, "detail": "couldn't measure the file"}}
    bad = [k for k, v in tech.items() if not v["ok"]]
    put("technical", 5 if not bad else 1, "all technical checks pass" + (" (SIMULATED render)" if probe.get("simulated") else "")
        if not bad else "failed: " + ", ".join(f"{k} ({tech[k]['detail']})" for k in bad))
    if "duration" in bad:
        revise.append("too_long")
    if bad:
        fixes.append("Fix the render: " + ", ".join(bad))
    overall = round(sum(s["score"] for s in scores.values()) / len(scores), 2)
    passed = all(scores[c]["score"] >= 3 for c in CRITICAL) and overall >= PASS_OVERALL
    mechanical_only = not passed and set(revise) and all(scores[c]["score"] >= 3 for c in CRITICAL if c not in ("technical", "captions"))
    return scores, overall, passed, fixes, sorted(set(revise)) if mechanical_only else []


def run(ctx, inp):
    ids = inp.get("creative_ids")
    creatives = [ctx.store.get("creatives", i) for i in ids] if ids else \
        ctx.store.find("creatives", business_id=ctx.business_id, status=["rendered", "render_failed"])
    reviews, revise = [], []
    for c in creatives:
        ctx.check()
        scores, overall, passed, fixes, mech = review(ctx, c)
        ctx.store.insert("qa_reviews", business_id=ctx.business_id, creative_id=c["id"], passed=passed, overall=overall,
                         scores_json=scores, fixes_json=fixes, label=E.PREDICTION, created_at=ctx.now(),
                         model_version=ctx.model_version, simulated=ctx.simulated)
        status = "qa_passed" if passed else ("qa_revise" if mech else "rejected")
        ctx.store.update("creatives", c["id"], status=status, updated_at=ctx.now())
        reviews.append({"creative_id": c["id"], "passed": passed, "overall": float(overall), "fixes": fixes})
        if mech:
            revise.append({"creative_id": c["id"], "fixes": mech})
    return {"reviews": reviews, "revise": revise}
