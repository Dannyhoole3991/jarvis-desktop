"""
Campaign Testing & Optimisation agent.

  design    an experiment per product from QA-passed creatives with DIFFERENT concepts: hypothesis,
            variants, audience, placement, budget (money 0: organic only), duration, success criteria,
            stopping rules, attribution window. Each post then waits for Danny's approval.
  publish   only posts Danny approved, each through controls.require (which consumes that approval,
            so one approval can never cover two posts or changed content).
  collect   platform numbers per post and real orders. Anything not reported is stored as
            unavailable (NULL), never 0. Orders are attributed only by the post's UTM, inside the window.
  judge     stats.evaluate on sessions, orders and contribution profit; never on views or likes.

Organic TikTok posts can't carry clickable links: attribution relies on the link in bio carrying
the post's UTM, which Danny sets when a post goes live. That limits what organic tests can show,
so most organic verdicts will honestly be "inconclusive"; this is recorded on every experiment.
"""

import hashlib
import os
import re
import urllib.parse

from jarvis_marketing import controls as C, evidence as E, stats
from jarvis_marketing.adapters import Unavailable

DAY = 86400
METRICS = ("views", "likes", "comments", "shares", "avg_watch_seconds", "completion_rate", "link_clicks", "sessions")
DIAGNOSTIC = ("views", "likes", "comments", "shares", "avg_watch_seconds", "completion_rate")
ATTRIBUTION_NOTE = ("TikTok organic posts can't hold clickable links and a profile has one bio link, so each post's UTM link "
                    "goes in the bio while that post is live. Variants run one after another, so time effects aren't controlled: "
                    "treat organic results as directional.")


def utm_link(destination, experiment_id, post_id):
    q = urllib.parse.urlencode({"utm_source": "tiktok", "utm_medium": "organic", "utm_campaign": f"exp-{experiment_id}",
                                "utm_content": f"post-{post_id}"})
    return f"{destination}{'&' if '?' in destination else '?'}{q}"


def _file_sha(path):
    if not path or not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def post_content(post_fields, creative):
    """Exactly what Danny approves. Change any of it and the approval no longer matches."""
    files = creative["files"] or {}
    return {"platform": post_fields["platform"], "caption": post_fields["caption"], "link": post_fields["link"],
            "creative_uid": creative["creative_uid"], "file_sha256": _file_sha(files.get("video") or files.get("manifest"))}


# ------------------------------------------------------------------ design
def design(ctx, inp):
    if not ctx.controls.allowed("design_experiment"):
        return {"experiments": [], "notes": [f"Experiments can't be designed in {ctx.controls.mode()} mode."]}
    used = {p["creative_id"] for p in ctx.store.find("posts", business_id=ctx.business_id, limit=100000)}
    passed = [c for c in ctx.store.find("creatives", business_id=ctx.business_id, status="qa_passed") if c["id"] not in used]
    by_cand = {}
    for c in passed:
        concept = ctx.store.get("briefs", c["brief_id"])["concept"]
        by_cand.setdefault(c["candidate_id"], {}).setdefault(concept, c)        # one creative per concept
    made, notes = [], []
    active = len(ctx.store.find("experiments", business_id=ctx.business_id, status=["designed", "awaiting_approval", "running", "ended"]))
    for cand_id, concepts in sorted(by_cand.items()):
        ctx.check()
        if active >= ctx.testing["max_active_experiments"]:
            notes.append(f"{active} experiments already open (limit {ctx.testing['max_active_experiments']}); the rest wait")
            break
        if ids := inp.get("candidate_ids"):
            if cand_id not in ids:
                continue
        variants = list(concepts.values())
        if len(variants) < 2:
            notes.append(f"candidate {cand_id}: only one QA-passed concept, need two to compare")
            continue
        key = "exp:" + ",".join(str(v["id"]) for v in sorted(variants, key=lambda v: v["id"]))
        if ctx.store.one("experiments", idem_key=key):
            continue
        cand = ctx.store.get("candidates", cand_id)
        econ = ((cand["data"].get("card") or {}).get("economics")) or {}
        t = ctx.testing
        data = {"hypothesis": f"For {cand['title']}, the concepts {', '.join(concepts)} differ in how many viewers buy.",
                "variants": [{"creative_id": v["id"], "concept": k, "brief_id": v["brief_id"]} for k, v in concepts.items()],
                "audience": "UK TikTok users (organic For You feed)", "placement": "TikTok organic posts",
                "budget": {"money": 0.0, "currency": ctx.business["currency"], "posts_per_variant": 1,
                           "note": "Organic only. Paid spend is disabled in code in this milestone."},
                "duration_days": t["experiment_days"],
                "success_criteria": {"primary": "contribution profit from attributed orders > 0, and a significantly higher "
                                                "conversion rate (sessions to orders) than every other variant",
                                     "min_sessions_per_variant": t["min_sessions_per_variant"], "significance": t["significance"],
                                     "never": "views, likes or comments decide nothing"},
                "stopping_rules": [f"stop a variant after {t['stop_sessions_without_order']} sessions with no order",
                                   "stop everything on emergency stop", f"end after {t['experiment_days']} days",
                                   "no verdict on stale or missing data"],
                "attribution": {"model": "last click by UTM (utm_content=post-<id>)", "window_days": t["attribution_window_days"],
                                "data_delay_hours": t["data_delay_hours"], "limitation": ATTRIBUTION_NOTE},
                "unit_contribution_estimate": econ.get("contribution"), "price": econ.get("price"),
                "break_even_cac": econ.get("break_even_cac")}
        eid = ctx.store.insert("experiments", business_id=ctx.business_id, candidate_id=cand_id, kind="organic", status="designed",
                               idem_key=key, data_json=data, created_at=ctx.now(), simulated=ctx.simulated)
        posts = []
        for v in data["variants"]:
            creative = ctx.store.get("creatives", v["creative_id"])
            brief = ctx.store.get("briefs", v["brief_id"])
            pid = ctx.store.insert("posts", business_id=ctx.business_id, experiment_id=eid, creative_id=creative["id"],
                                   platform="tiktok", content_hash=f"pending-{eid}-{creative['id']}", caption="", status="draft",
                                   created_at=ctx.now(), simulated=ctx.simulated)
            caption = brief["data"]["caption"] + "\n" + " ".join(brief["data"].get("hashtags") or [])
            fields = {"platform": "tiktok", "caption": caption.strip(), "link": utm_link(brief["data"]["destination"], eid, pid)}
            content = post_content(fields, creative)
            ctx.store.update("posts", pid, caption=fields["caption"], link=fields["link"], content_hash=C.payload_hash(content),
                             status="pending_approval")
            if ctx.controls.allowed("request_approval"):
                aid = C.Approvals(ctx.store).request(ctx.business_id, "publish_organic", "post", pid, content,
                                                     f"Post the '{v['concept'].replace('_', ' ')}' video for {cand['title']} to TikTok",
                                                     "testing-agent")
                ctx.store.update("posts", pid, approval_id=aid)
            posts.append(pid)
        ctx.store.update("experiments", eid, status="awaiting_approval")
        ctx.store.update("candidates", cand_id, status="testing", updated_at=ctx.now())
        made.append({"experiment_id": eid, "candidate_id": cand_id, "post_ids": posts})
        active += 1
    return {"experiments": made, "notes": notes}


# ------------------------------------------------------------------ paid (Facebook / Instagram), PREPARED only
PAID_IN = {"candidate_ids?": [int]}
PAID_OUT = {"prepared": [{"experiment_id": int, "candidate_id": int, "ready": bool, "problems": [str]}], "notes": [str]}
PAID_OFF = ("Launching is disabled in code (paid ads are off). To run it, Danny must authorise this campaign and its "
            "budget: switch paid ads on in jarvis_marketing/controls.py, set spend caps, and approve the campaign.")


def paid_link(destination, experiment_id, creative_id):
    q = urllib.parse.urlencode({"utm_source": "meta", "utm_medium": "paid", "utm_campaign": f"exp-{experiment_id}",
                                "utm_content": f"creative-{creative_id}"})
    return f"{destination}{'&' if '?' in destination else '?'}{q}"


def paid_plan(ctx, inp):
    """A complete, PAUSED Facebook/Instagram test plan per product: nothing is sent to Meta."""
    ctx.external("prepare_paid")
    pt, made, notes = ctx.business["paid_test"], [], []
    ids = inp.get("candidate_ids")
    for a in ctx.store.sql("SELECT * FROM assessments WHERE business_id=? AND id IN (SELECT MAX(id) FROM assessments "
                           "WHERE business_id=? GROUP BY candidate_id)", (ctx.business_id, ctx.business_id)):
        ctx.check()
        if ids and a["candidate_id"] not in ids:
            continue
        cand = ctx.store.get("candidates", a["candidate_id"])
        top = a["data"]["test_first"][: pt["max_variants"]]
        if len(top) < 2:
            notes.append(f"{cand['title']}: fewer than two passed creatives, nothing to compare")
            continue
        key = "paid:" + ",".join(map(str, sorted(top)))
        if ctx.store.one("experiments", idem_key=key):
            continue
        listing = cand["data"].get("listing") or {}
        page = None
        if listing.get("handle"):
            page, _ = ctx.adapters.shop.product_page(listing["handle"])
        live = bool(page and page.get("available") and page.get("url") == listing.get("url"))
        econ = (cand["data"].get("card") or {}).get("economics") or {}
        eid = ctx.store.insert("experiments", business_id=ctx.business_id, candidate_id=cand["id"], kind="paid_meta",
                               status="preparing", idem_key=key, data_json={}, created_at=ctx.now(), simulated=ctx.simulated)
        ads = []
        for cid in top:
            c = ctx.store.get("creatives", cid)
            b = ctx.store.get("briefs", c["brief_id"])
            ads.append({"creative_id": cid, "concept": b["concept"], "video_file": (c["files"] or {}).get("video"),
                        "primary_text": b["data"]["caption"], "headline": cand["title"][:40], "cta_button": "SHOP_NOW",
                        "link": paid_link(listing.get("url") or "", eid, cid), "format": "9:16 video"})
        n = len(ads)
        budget = {"money": round(pt["daily_per_ad_set"] * pt["days"] * n, 2), "daily_per_ad_set": pt["daily_per_ad_set"],
                  "days": pt["days"], "currency": ctx.business["currency"], "status": "PROPOSED (not authorised)",
                  "note": "Proposed only. Nothing can be spent: paid ads are off in code."}
        plan = {"platform": "Meta (Facebook + Instagram)", "objective": "OUTCOME_SALES", "status_on_create": "PAUSED",
                "hypothesis": f"For {cand['title']}, the top-ranked concepts ({', '.join(x['concept'] for x in ads)}) differ in cost per purchase.",
                "variants": [{"creative_id": x["creative_id"], "concept": x["concept"]} for x in ads],
                "ad_sets": [{"name": f"{x['concept']} (creative {x['creative_id']})", "daily_budget": pt["daily_per_ad_set"],
                             "audience": pt["audience"], "placements": pt["placements"], "optimisation": pt["optimisation"]} for x in ads],
                "ads": ads, "audience": f"{', '.join(pt['audience']['countries'])}, ages {pt['audience']['age_min']}-{pt['audience']['age_max']}, "
                                        f"{pt['audience']['targeting']}", "audiences_approved": False,
                "placement": ", ".join(pt["placements"]), "budget": budget, "duration_days": pt["days"],
                "destination": listing.get("url"), "destination_live": live,
                "tracking": {"meta_pixel_id": ctx.business["meta"].get("pixel_id") or None, "utm": "utm_source=meta, utm_content=creative-<id>",
                             "record_of_truth": "Shopify orders by UTM; Meta's attributed purchases are kept separately"},
                "success_criteria": {"primary": "lowest cost per purchase below the break-even CPA, with contribution profit after ad spend > 0",
                                     "break_even_cpa": econ.get("break_even_cac"), "min_purchases_per_variant": 5,
                                     "never": "views, clicks or CTR alone decide nothing"},
                "stopping_rules": [f"stop a variant once its spend reaches {econ.get('break_even_cac')} x 2 with no purchase"
                                   if econ.get("break_even_cac") else "stop a variant at 2x break-even CPA with no purchase",
                                   "stop everything on emergency stop or at the spend cap", f"end after {pt['days']} days",
                                   "no verdict on stale or missing data"],
                "attribution": {"model": pt["attribution"], "window_days": 7, "data_delay_hours": ctx.testing["data_delay_hours"],
                                "limitation": "Meta's attributed purchases include view-through and modelled conversions; "
                                              "Shopify UTM orders are counted separately and are the record of truth."},
                "unit_contribution_estimate": econ.get("contribution"), "price": econ.get("price"),
                "break_even_cac": econ.get("break_even_cac"), "assessment_id": a["id"], "launch": PAID_OFF}
        problems = ctx.adapters.paid.validate(plan) if hasattr(ctx.adapters.paid, "validate") else ["No paid-ads adapter."]
        plan["problems"] = problems
        ctx.store.update("experiments", eid, data_json=plan, status="prepared")
        made.append({"experiment_id": eid, "candidate_id": cand["id"], "ready": not problems, "problems": problems})
    if not made and not notes:
        notes.append("No ranked creatives yet (they need QA and the pre-launch ranking first).")
    return {"prepared": made, "notes": notes}


# ------------------------------------------------------------------ publish
def _post_view(ctx, post):
    creative = ctx.store.get("creatives", post["creative_id"])
    brief = ctx.store.get("briefs", creative["brief_id"])
    return dict(post, concept=brief["concept"], supplier_product_id=brief["data"]["product_identity"]["supplier_product_id"],
                title=brief["data"]["product_identity"]["title"], price=brief["data"].get("price_stated"))


def publish(ctx, inp):
    ids = inp.get("post_ids")
    posts = [ctx.store.get("posts", i) for i in ids] if ids else ctx.store.find("posts", business_id=ctx.business_id, status="approved")
    done, refused = [], []
    for post in posts:
        ctx.check()
        if post["status"] != "approved":
            refused.append({"post_id": post["id"], "why": f"post is {post['status']}"})
            continue
        creative = ctx.store.get("creatives", post["creative_id"])
        twin = [p for p in ctx.store.find("posts", creative_id=post["creative_id"], status=["publishing", "published", "check"])
                if p["id"] != post["id"]]
        if twin:
            ctx.store.update("posts", post["id"], status="failed", error=f"Duplicate: this video already went out as post {twin[0]['id']}.")
            refused.append({"post_id": post["id"], "why": "duplicate of an already published post"})
            continue
        content = post_content({"platform": post["platform"], "caption": post["caption"], "link": post["link"]}, creative)
        if C.payload_hash(content) != post["content_hash"]:
            ctx.store.update("posts", post["id"], status="failed", error="The content changed after approval; it needs a new approval.")
            refused.append({"post_id": post["id"], "why": "content changed after approval"})
            continue
        try:
            ctx.external("publish_organic", approval_id=post["approval_id"], content=content)
        except C.PermissionDenied as why:
            ctx.store.update("posts", post["id"], error=str(why)[:300])
            refused.append({"post_id": post["id"], "why": str(why)})
            continue
        # The approval is used up. Mark "publishing" BEFORE calling out, so a crash mid-post can never repost.
        if not ctx.store.update("posts", post["id"], where={"status": "approved"}, status="publishing"):
            refused.append({"post_id": post["id"], "why": "already being published"})
            continue
        files = creative["files"] or {}
        try:
            result = ctx.adapters.publisher.publish(_post_view(ctx, post), files.get("video") or files.get("manifest"))
            status = "published" if result["status"] == "posted" else "check"
            ctx.store.update("posts", post["id"], status=status, external_ref=result["external_ref"], published_at=ctx.now(),
                             error=result.get("note"))
            ctx.store.audit("testing-agent", "publish_organic", status, subject=f"post:{post['id']}", business_id=ctx.business_id,
                            external_ref=result["external_ref"], simulated=ctx.simulated)
            exp = ctx.store.get("experiments", post["experiment_id"])
            if exp["status"] in ("designed", "awaiting_approval"):
                ctx.store.update("experiments", exp["id"], status="running", started_at=ctx.now())
            ctx.store.notify(f"Posted to TikTok{' (SIMULATED)' if ctx.simulated else ''}. Set your TikTok bio link to: {post['link']}",
                             "needs_you", ctx.business_id)
            done.append({"post_id": post["id"], "status": status, "external_ref": result["external_ref"]})
        except Exception as error:
            ctx.store.update("posts", post["id"], status="failed", error=str(error)[:300])
            ctx.store.audit("testing-agent", "publish_organic", "failed", subject=f"post:{post['id']}", business_id=ctx.business_id,
                            error=str(error)[:300])
            refused.append({"post_id": post["id"], "why": f"publish failed: {str(error)[:200]}"})
    return {"published": done, "refused": refused}


# ------------------------------------------------------------------ collect
def collect(ctx, inp):
    now = ctx.now()
    posts = ctx.store.find("posts", business_id=ctx.business_id, status=["published", "check"])
    stored, unavailable = 0, []
    for post in posts:
        ctx.check()
        view = _post_view(ctx, post)
        try:
            values, fid = ctx.adapters.analytics.post_metrics(view, now)
        except Unavailable as why:
            unavailable.append(f"post {post['id']}: {why}")
            continue
        observed = values.pop("observed_at", None) or now
        for m in METRICS:
            v = values.get(m)
            ctx.store.insert("metrics", business_id=ctx.business_id, experiment_id=post["experiment_id"], post_id=post["id"],
                             source=ctx.adapters.analytics.name, metric=m, value=v, available=v is not None, collected_at=now,
                             observed_at=observed, simulated=ctx.simulated)
            stored += 1
    orders_added = 0
    if posts:
        orders_adapter = ctx.adapters.orders
        if hasattr(orders_adapter, "posts_source"):          # the simulated shop needs to know what was posted
            orders_adapter.posts_source = lambda: [_post_view(ctx, p) for p in posts]
            orders_adapter.price_for = lambda p: p.get("price") or 0.0
        since = min(p["published_at"] for p in posts if p["published_at"]) - 28 * DAY
        try:
            orders, fid = orders_adapter.orders(since, now)
        except Exception as error:
            orders, fid = [], None
            unavailable.append(f"orders: {str(error)[:200]}")
        window = ctx.testing["attribution_window_days"] * DAY
        for o in orders:
            post_id = None
            content = ((o.get("attribution") or {}).get("utm_content")) or ""
            m = re.fullmatch(r"post-(\d+)", content)
            if m:
                p = ctx.store.get("posts", int(m.group(1)))
                if p and p["business_id"] == ctx.business_id and p["published_at"] and \
                        p["published_at"] <= o["created_at"] <= p["published_at"] + window:
                    post_id = p["id"]
            if ctx.store.one("orders", business_id=ctx.business_id, order_ref=o["order_ref"]):
                continue
            ctx.store.insert("orders", business_id=ctx.business_id, order_ref=o["order_ref"], created_at=o["created_at"],
                             total=o["total"], currency=o["currency"], items_json=o.get("items"), attribution_json=o.get("attribution"),
                             post_id=post_id, source=orders_adapter.name, collected_at=now, simulated=ctx.simulated)
            orders_added += 1
    return {"metrics_stored": stored, "orders_added": orders_added, "unavailable": unavailable}


# ------------------------------------------------------------------ judge
def latest_metrics(store, post_id):
    rows = store.find("metrics", post_id=post_id, order="collected_at DESC", limit=len(METRICS) * 3)
    if not rows:
        return {}, None
    at = rows[0]["collected_at"]
    return {r["metric"]: (r["value"] if r["available"] else None) for r in rows if r["collected_at"] == at}, at


def variant_numbers(ctx, exp):
    now, ttl = ctx.now(), ctx.testing["analytics_ttl_hours"] * 3600
    unit = exp["data"].get("unit_contribution_estimate")
    out = []
    for post in ctx.store.find("posts", experiment_id=exp["id"]):
        m, at = latest_metrics(ctx.store, post["id"])
        orders = ctx.store.find("orders", post_id=post["id"])
        published = post["status"] in ("published", "check")
        row = {"id": post["id"], "concept": ctx.store.get("briefs", ctx.store.get("creatives", post["creative_id"])["brief_id"])["concept"],
               "status": post["status"], "sessions": m.get("sessions") if published else None,
               "orders": len(orders) if published else None, "revenue": round(sum(o["total"] for o in orders), 2),
               "contribution": round(len(orders) * unit, 2) if unit is not None else None, "spend": 0.0,
               "stale": bool(published and (at is None or now - at > ttl)),
               "missing": [k for k in ("sessions",) if published and m.get(k) is None],
               "diagnostics": {k: m.get(k) for k in DIAGNOSTIC}, "collected_at": at, "order_ids": [o["id"] for o in orders]}
        out.append(row)
    return out


def judge(ctx, inp):
    out = []
    for exp in ctx.store.find("experiments", business_id=ctx.business_id, status=["running", "ended"]):
        ctx.check()
        variants = variant_numbers(ctx, exp)
        ends = (exp["started_at"] or ctx.now()) + exp["data"]["duration_days"] * DAY
        ended = ctx.now() >= ends
        price = exp["data"].get("price")
        crit = dict(ctx.testing, break_even_cvr=None)
        result = stats.evaluate([{k: v[k] for k in ("id", "sessions", "orders", "revenue", "contribution", "spend", "stale", "missing")}
                                 for v in variants], crit, ctx.now(), ends if ended else None)
        baseline = baseline_orders(ctx, exp)
        attributed_rev = round(sum(v["revenue"] for v in variants), 2)
        result.update(attributed_revenue=attributed_rev, attributed_orders=sum(v["orders"] or 0 for v in variants),
                      incremental={"label": E.ESTIMATE, **baseline},
                      contribution_label=E.ESTIMATE + " (real orders x estimated unit contribution)",
                      diagnostics={v["id"]: v["diagnostics"] for v in variants}, price=price,
                      recommendation=recommend(result), simulated=bool(exp["simulated"]))
        status = exp["status"]
        if ended and status == "running":
            status = "ended"
        if result["data_final"] and result["verdict"] in ("winner", "no_winner", "inconclusive"):
            status = "concluded"
        ctx.store.update("experiments", exp["id"], verdict=result["verdict"], result_json=result, status=status,
                         ended_at=ends if ended else None)
        out.append({"experiment_id": exp["id"], "verdict": result["verdict"], "status": status})
    return {"judged": out}


def recommend(result):
    stops = [v["id"] for v in result["variants"] if v["stop"]]
    if result["verdict"] == "winner":
        return (f"Variant {result['winner']} won on profit. Scaling would need paid spend, which is disabled; "
                "next step: Danny decides whether to set limits.")
    if result["verdict"] == "no_winner":
        return "Stop: no variant made a profit."
    if result["verdict"] == "inconclusive":
        return "Inconclusive: not enough evidence to pick a winner. Don't scale; rerun only with more traffic."
    return "Keep collecting." + (f" Recommend stopping posts {stops} already." if stops else "")


def baseline_orders(ctx, exp):
    """Pre/post comparison of ALL orders (an estimate of incremental orders, never a measured fact)."""
    if not exp["started_at"]:
        return {"incremental_orders": None, "basis": "experiment hasn't started"}
    start, days = exp["started_at"], exp["data"]["duration_days"]
    before = ctx.store.sql("SELECT COUNT(*) AS n FROM orders WHERE business_id=? AND created_at>=? AND created_at<?",
                           (ctx.business_id, start - 28 * DAY, start))[0]["n"]
    during = ctx.store.sql("SELECT COUNT(*) AS n FROM orders WHERE business_id=? AND created_at>=? AND created_at<?",
                           (ctx.business_id, start, start + days * DAY))[0]["n"]
    expected = before / 28 * days
    return {"incremental_orders": round(during - expected, 1), "orders_during": during, "expected_from_baseline": round(expected, 1),
            "basis": f"orders in the {days} test days minus the daily average of the 28 days before x {days}; "
                     "not a controlled measurement"}


def reconcile(store, business_id):
    """Do the dashboard's numbers match the records? Returns a list of problems (empty = all reconciled)."""
    problems = []
    for exp in store.find("experiments", business_id=business_id):
        v = exp.get("verdict") and exp["verdict"]
        if not v:
            continue
        vj = exp["result"]
        posts = [p["id"] for p in store.find("posts", experiment_id=exp["id"])]
        rows = store.find("orders", post_id=posts) if posts else []
        if round(sum(o["total"] for o in rows), 2) != vj["attributed_revenue"]:
            problems.append(f"experiment {exp['id']}: attributed revenue {vj['attributed_revenue']} != orders {sum(o['total'] for o in rows):.2f}")
        if len(rows) != vj["attributed_orders"]:
            problems.append(f"experiment {exp['id']}: {vj['attributed_orders']} attributed orders != {len(rows)} in records")
        if vj["verdict"] == "winner" and vj.get("winner") is None:
            problems.append(f"experiment {exp['id']}: winner without a variant")
    for o in store.find("orders", business_id=business_id):
        if o["post_id"]:
            p = store.get("posts", o["post_id"])
            if not p or not p["published_at"] or o["created_at"] < p["published_at"]:
                problems.append(f"order {o['order_ref']}: attributed to a post that wasn't live yet")
    return problems
