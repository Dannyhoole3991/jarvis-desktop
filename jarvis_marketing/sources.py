"""
Research sources: a configurable set of independent providers, each with its own adapter, access
requirements, rate limit, reliability check and provenance. Adding a provider is one entry here plus
an adapter (see adapters/); nothing else in the department changes.

Every call goes through `call()`, which paces requests per source, records the run (ok / failed /
unavailable, how many items, how long) in `source_runs`, and turns a failure into None so research
carries on with the sources that do work. `coverage()` is what the dashboard shows: which sources
answered recently, which are failing and why, and which need Danny to set something up.

What each source can and can't show is written down here, so nobody mistakes reach for sales:
  * Meta Ad Library: commercial ads only if they reached the EU (searched by EU country). It gives
    the ad text, start/stop dates, platforms and EU reach; no spend, no clicks, no sales, no video file.
  * TikTok Creative Center (Top Ads / Top Products): no official API. Ads Danny saves by hand are
    imported with their URL; TikTok's own "CTR"/"likes" labels there are TikTok's ranking signals.
  * TikTok Commercial Content API: needs TikTok's approval; unavailable until granted.
  * Supplier (CJ), competitor stores (public products.json), our own store and orders: as before.
"""

import time

from jarvis_marketing.adapters import Disabled, Unavailable

# name -> what it is, what it needs, how fast it may be called, what it can't show.
REGISTRY = {
    "meta_ad_library": {
        "kind": "ads", "adapter": "ad_library",
        "access": "A Meta developer app with identity confirmation and a long-lived access token "
                  "(saved with: py -3 -m jarvis_store.setup_credentials).",
        "min_interval_s": 2.0,
        "limits": "Non-political ads are only returned if they reached the EU; no spend, clicks or sales; "
                  "run length and reach are not proof of profit."},
    "manual_ads": {
        "kind": "ads", "adapter": "ad_imports",
        "access": "Ads Danny adds from the dashboard (e.g. from TikTok Creative Center Top Ads), with their URL.",
        "min_interval_s": 0.0,
        "limits": "Only what Danny copied; TikTok's CTR/like labels are TikTok's ranking signals, not our sales."},
    "tiktok_commercial_content": {
        "kind": "ads", "adapter": "tiktok_ads",
        "access": "TikTok Commercial Content API approval (apply on developers.tiktok.com).",
        "min_interval_s": 1.0, "limits": "EU ads only; no performance numbers."},
    "supplier": {
        "kind": "supplier", "adapter": "supplier", "access": "CJ API key (already stored).", "min_interval_s": 1.2,
        "limits": "'Listed by N stores' is a popularity signal, not sales; stock may change."},
    "competitor_stores": {
        "kind": "competitors", "adapter": "competitors", "access": "Store URLs listed in businesses.json.",
        "min_interval_s": 1.0, "limits": "Prices only; no sales numbers."},
    "search_demand": {
        "kind": "demand", "adapter": "trends", "access": "Google Trends API (invite-only alpha).", "min_interval_s": 1.0,
        "limits": "Interest index, not purchases."},
    "short_form": {
        "kind": "social", "adapter": "shortform", "access": "TikTok Research API (academic only).", "min_interval_s": 1.0,
        "limits": "Views are attention, not sales."},
    "reviews": {
        "kind": "reviews", "adapter": "reviews", "access": "No supported review source yet.", "min_interval_s": 1.0,
        "limits": "Small samples."},
    "google_merchant_center": {
        "kind": "google", "adapter": "merchant",
        "access": "Shopify 'Google & YouTube' channel for products; Merchant API + Danny's Google sign-in (OAuth) for statuses.",
        "min_interval_s": 1.0, "limits": "Approval/disapproval and free-listing clicks for OUR products only."},
    "google_search_console": {
        "kind": "google", "adapter": "search_console", "access": "Search Console API + Danny's Google sign-in (OAuth), site verified.",
        "min_interval_s": 1.0, "limits": "Queries and indexing for OUR site only; never a ranking guarantee."},
    "google_shopping": {
        "kind": "google", "adapter": "google_shopping", "access": "No official API for Shopping results.",
        "min_interval_s": 1.0, "limits": "Competitor prices come from listed competitor stores instead."},
    "own_store": {
        "kind": "own", "adapter": "shop", "access": "Public store pages (no credentials).", "min_interval_s": 0.5,
        "limits": "What customers see."},
    "own_orders": {
        "kind": "own_sales", "adapter": "orders", "access": "Shopify admin (browser) or read_orders API scope.",
        "min_interval_s": 1.0, "limits": "Attribution only where the order carries our UTM."},
}

_last_call = {}


def call(ctx, source, method, *args, **kwargs):
    """Calls adapters.<source adapter>.<method>(*args). Returns the result, or None if the source is
    unavailable or failed (the run is recorded either way and research carries on)."""
    spec = REGISTRY[source]
    adapter = getattr(ctx.adapters, spec["adapter"], None)
    started = ctx.now()
    simulated = bool(getattr(adapter, "simulated", False))

    def done(ok, items=None, error=None):
        ctx.store.insert("source_runs", business_id=ctx.business_id, source=source, call=method, ok=ok, items=items,
                         error=(str(error)[:300] if error else None), started_at=started, finished_at=ctx.now(),
                         simulated=simulated)
    if adapter is None:
        done(False, error="not configured for this business")
        return None
    wait = spec["min_interval_s"] - (time.monotonic() - _last_call.get((ctx.business_id, source), 0.0))
    if wait > 0 and not simulated:
        time.sleep(min(wait, spec["min_interval_s"]))
    _last_call[(ctx.business_id, source)] = time.monotonic()
    try:
        out = getattr(adapter, method)(*args, **kwargs)
    except (Unavailable, Disabled) as why:
        done(False, error=f"unavailable: {why}")
        return None
    except Exception as error:          # noqa: BLE001 - one source failing never stops research
        done(False, error=f"{type(error).__name__}: {error}")
        ctx.log(f"{source}.{method} failed: {str(error)[:200]}")
        return None
    items = out[0] if isinstance(out, tuple) else out
    done(True, items=len(items) if isinstance(items, (list, tuple)) else None)
    return out


def coverage(store, business_id, adapters=None, since_s=14 * 86400):
    """Per source: configured?, last ok/failure, recent success rate, last error and what it needs."""
    now = store.clock()
    rows = store.sql("SELECT source, ok, items, error, finished_at, simulated FROM source_runs WHERE business_id=? AND started_at>=? "
                     "ORDER BY id", (business_id, now - since_s))
    out = []
    for name, spec in REGISTRY.items():
        runs = [r for r in rows if r["source"] == name]
        adapter = getattr(adapters, spec["adapter"], None) if adapters else None
        note = getattr(adapter, "unavailable_reason", None)
        last_ok = max((r["finished_at"] for r in runs if r["ok"]), default=None)
        failures = [r for r in runs if not r["ok"]]
        state = ("unavailable" if note or (runs and all(not r["ok"] and str(r["error"] or "").startswith("unavailable") for r in runs))
                 else "never run" if not runs else "ok" if runs[-1]["ok"] else "failing")
        out.append({"source": name, "kind": spec["kind"], "state": state, "runs": len(runs),
                    "success_rate": round(sum(1 for r in runs if r["ok"]) / len(runs), 2) if runs else None,
                    "last_ok": last_ok, "last_error": failures[-1]["error"] if failures else note,
                    "items": sum(r["items"] or 0 for r in runs if r["ok"]),
                    "simulated": bool(getattr(adapter, "simulated", False)) if adapter else None,
                    "access": spec["access"], "limits": spec["limits"]})
    return out
