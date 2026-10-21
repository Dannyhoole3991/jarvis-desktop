"""
Ad Research agent: finds existing adverts for the products being researched and reverse-engineers them.

For each keyword it asks every ad source (sources.REGISTRY kind "ads") for adverts, stores them with
their provenance, and analyses each one into the same structure:

    hook / first seconds / problem or desire / first shot / product reveal / scenes and pacing /
    demonstration / benefits / objections / emotional triggers / text overlays / captions / audio /
    editing style / offer / price presentation / call to action / customer and angle /
    performance evidence and its limits / elements worth testing

Every element says whether it was OBSERVED (read from the ad's text, dates or a measured video
file) or is NOT OBSERVABLE from that source; "why it might work" is always a HYPOTHESIS. Nothing is
inferred from an ad that the source didn't show: no visuals without a video file, no sales ever.

The principles (`principles()`) summarise what the ads for a keyword have in common, weighted
towards ads that kept running. They steer which ORIGINAL concepts the creative agent writes; ad
copy is never reused (see `too_close()`), and music from other adverts is never used.
"""

import datetime as dt
import re
import statistics
from difflib import SequenceMatcher

from jarvis_marketing import evidence as E, sources

INPUT = {"keywords?": [str]}
OUTPUT = {"keywords": [{"keyword": str, "ads": int, "analysed": int, "sources_ok": [str], "sources_failed": [str],
                        "principles": int}], "notes": [str]}
ANALYSER = "jarvis-ad-analyser/1 (rules)"
LONG_RUNNING_DAYS = 30
AD_SOURCES = [name for name, s in sources.REGISTRY.items() if s["kind"] == "ads"]

HOOK_TYPES = {      # first matching cue (by position in the hook) decides the type
    "problem": r"tired of|sick of|struggl|hate |mess|clutter|tangl|annoying|never again|no more|stop |ruin|fed up|can't find|chaos|"
               r"keep losing|always los",
    "curiosity": r"didn't expect|did not expect|secret|won't believe|nobody|no one tells|why |wait|you need to see|\?$",
    "demo": r"watch|see how|look at|in seconds|here's how|test(ed|ing)? |try this|before and after",
    "benefit": r"easiest|easy|saves?|instantly|effortless|keeps? |perfect for|upgrade|boost|finally|best self|productiv|"
               r"somewhere (proper|to live)|all (zipped|tucked|packed|stored) away|takes the stress|declutter|makes? .{0,20}easier",
    "objection": r"same\b|worth it|difference|\bvs\b|compared|actually works|honest|cheap",
    "social_proof": r"everyone|viral|sold out|reviews?|customers|made me buy|best.?seller|people are",
    "offer": r"% off|\bsale\b|\bfree\b|[£$€]\s?\d|buy \d|\bdeal\b|discount",
}
# Our concept for each hook type. Social proof needs real reviews we don't have, so it's never copied.
CONCEPT_FOR = {"problem": "problem_solution", "demo": "demo_closeup", "curiosity": "curiosity_open", "benefit": "benefit_led",
               "objection": "objection_comparison", "offer": "price_reveal", "social_proof": None}
WHY = {
    "problem": "Naming a frustration viewers already have makes them stop and recognise themselves.",
    "curiosity": "An open question or surprise makes viewers watch on to find out the answer.",
    "demo": "Seeing the product work answers 'does it actually do that?' before it's asked.",
    "benefit": "Leading with the outcome tells the viewer what's in it for them straight away.",
    "objection": "Answering a doubt up front removes the reason not to buy.",
    "social_proof": "Popularity reassures, but we can only say it with real reviews, so it isn't used.",
    "offer": "A clear price or deal filters for people ready to buy.",
}
BENEFIT = (r"(no more [^.!?]+|saves? [^.!?]+|keeps? [^.!?]+|in seconds|no tools[^.!?]*|fits [^.!?]+|easy to [^.!?]+|made (from|of) [^.!?]+|"
           r"featuring [^.!?]+|takes the stress out[^.!?]*|one [a-z]+ each|all (zipped|tucked|packed|stored) away|"
           r"nothing(?:'s| is) loose|nothing gets [a-z]+|boost your [^.!?,]+|with wireless [a-z]+)")
OBJECTION = r"(no tools|free (uk )?(delivery|shipping|returns)|money.?back|guarantee|easy returns|fits any [^.!?]+|works on any [^.!?]+|washable|reusable)"
TRIGGERS = {"relief": r"no more|finally|sorted|stress", "urgency": r"limited|today only|ends|last chance|selling fast|while stocks",
            "curiosity": r"secret|didn't expect|won't believe|\?", "belonging/social proof": r"everyone|viral|people are|customers",
            "pride/aesthetic": r"beautiful|aesthetic|clean look|tidy|stylish", "value": r"save|deal|% off|cheap|only [£$€]"}
CTA = r"(shop now|buy now|order now|get yours|tap the link|link in bio|learn more|grab one|shop the)"
PRICE = r"([£$€]\s?\d+(?:[.,]\d{2})?|\d+\s?% off|buy \d+[^.!?]*|free (uk )?(delivery|shipping))"


def _first_sentence(text):
    text = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text)[:200]


def hook_type(hook):
    best, at = "other", None
    low = (hook or "").lower()
    for kind, pattern in HOOK_TYPES.items():
        m = re.search(pattern, low)
        if m and (at is None or m.start() < at):
            best, at = kind, m.start()
    return best


def _found(pattern, text):
    return sorted({m.group(0).strip() for m in re.finditer(pattern, text, re.I)})


def _days_between(start, stop, today):
    try:
        a = dt.date.fromisoformat(start)
        b = dt.date.fromisoformat(stop) if stop else today
        return max(0, (b - a).days)
    except (TypeError, ValueError):
        return None


def analyse(ad, today):
    """One advert -> the structured reverse-engineering. Pure (no I/O), so it is easy to test."""
    text = " ".join(ad.get("bodies") or []).strip()
    titles = " ".join((ad.get("titles") or []) + (ad.get("descriptions") or []))
    full = (text + " " + titles).strip()
    hook = _first_sentence(text) if text else _first_sentence(titles)
    kind = hook_type(hook)
    video = ad.get("video") if isinstance(ad.get("video"), dict) and "error" not in (ad.get("video") or {}) else None
    no_text = {"value": None, "observed": False, "note": "the source gave no ad text"}

    def obs(value, src, why=None, test=False):
        return {"value": value, "observed": True, "from": src, "why_it_might_work": why, "label_why": E.HYPOTHESIS if why else None,
                "worth_testing": test}

    def unseen(why):
        return {"value": None, "observed": False, "note": why}
    no_video = "not observable: no video file from this source (someone has to watch it at the ad's URL)"
    el = {}
    el["hook"] = obs(hook, "first line of the ad text", WHY.get(kind), test=CONCEPT_FOR.get(kind) is not None) if hook else no_text
    el["hook_type"] = obs(kind, "keyword cues in the hook (rules)") if hook else no_text
    if video:
        first = video.get("first_shot_s")
        if first is None and video.get("duration_s"):
            first = (video.get("cuts") or [video["duration_s"]])[0]
        el["first_seconds"] = obs({"first_shot_s": first, "cuts_in_first_3s": sum(1 for c in video.get("cuts") or [] if c <= 3.0)},
                                  "ffmpeg scene detection on the video file",
                                  "A cut inside the first 2 seconds keeps the opening moving." if first is not None and first <= 2.0 else None,
                                  test=first is not None and first <= 2.0)
        el["first_shot"] = obs(f"{first}s before the first cut", "ffmpeg scene detection")
        dur, cuts = video.get("duration_s") or 0, video.get("cuts") or []
        shots = [b - a for a, b in zip([0.0] + cuts, cuts + [dur])] if dur else []
        avg = round(statistics.mean(shots), 2) if shots else None
        el["scenes_pacing"] = obs({"duration_s": dur, "cuts": len(cuts), "avg_shot_s": avg,
                                   "cuts_per_10s": round(10 * len(cuts) / dur, 2) if dur else None}, "ffmpeg scene detection")
        style = None if avg is None else "fast-cut" if avg < 1.5 else "moderate" if avg < 3.5 else "slow / long takes"
        el["editing_style"] = obs(style, "average shot length", "Fast cuts hold attention on short-form feeds." if style == "fast-cut" else None,
                                  test=style == "fast-cut")
        el["audio"] = obs("has an audio track" if video.get("has_audio") else "silent", "ffprobe",
                          None) if "has_audio" in video else unseen(no_video)
        if video.get("simulated"):
            for k in ("first_seconds", "first_shot", "scenes_pacing", "editing_style", "audio"):
                el[k]["simulated"] = True
    else:
        for k in ("first_seconds", "first_shot", "scenes_pacing", "editing_style", "audio"):
            el[k] = unseen(no_video)
    el["product_reveal"] = unseen("not observable automatically: when the product first appears needs a person watching the video")
    el["text_overlays"] = unseen("not observable: the API gives the post text, not the words shown on screen")
    el["captions"] = unseen("not observable: subtitles are part of the video picture")
    pd = _found(HOOK_TYPES["problem"], full)
    desire = _found(r"(easy|easier|tidy|clean|organis|organiz|space|time|beautiful|gift)\w*", full)
    el["problem_or_desire"] = obs({"problem_words": pd, "desire_words": desire}, "ad text",
                                  "Speaks to a pain the customer already feels." if pd else "Sells an outcome the customer wants." if desire else None,
                                  test=bool(pd)) if full else no_text
    demo = _found(HOOK_TYPES["demo"], full)
    el["demonstration"] = obs(demo or "no demonstration words in the text", "ad text",
                              WHY["demo"] if demo else None, test=bool(demo)) if full else no_text
    el["benefits"] = obs(_found(BENEFIT, full), "ad text") if full else no_text
    objections = _found(OBJECTION, full)
    el["objections_answered"] = obs(objections, "ad text", WHY["objection"] if objections else None, test=bool(objections)) if full else no_text
    trig = {k: _found(p, full) for k, p in TRIGGERS.items()}
    el["emotional_triggers"] = obs({k: v for k, v in trig.items() if v}, "ad text (word cues)") if full else no_text
    prices = _found(PRICE, full)
    el["offer"] = obs(prices or "no offer in the text", "ad text", WHY["offer"] if prices else None, test=bool(prices)) if full else no_text
    el["price_presentation"] = obs("price stated" if any(re.search(r"[£$€]", p) for p in prices) else "no price shown in text", "ad text") if full else no_text
    cta = ad.get("cta") or (_found(CTA, full) or [None])[0]
    el["call_to_action"] = obs(cta, "the ad's button/CTA field" if ad.get("cta") else "ad text") if cta else unseen("no call to action in what the source showed")
    angle = (f"{kind} angle" + (f" for people bothered by {', '.join(pd[:2])}" if pd else "")) if hook else None
    el["customer_and_angle"] = {"value": angle, "observed": False, "label": E.HYPOTHESIS,
                                "note": "inferred from the words used; who the ad was really targeted at isn't shown"}
    run_days = _days_between(ad.get("start"), ad.get("stop"), today)
    perf = {"start": ad.get("start"), "stop": ad.get("stop"), "run_days": run_days,
            "run_days_label": E.ESTIMATE if run_days is not None else None,
            "run_days_basis": "stop (or today, if no stop date) minus start date, from the source" if run_days is not None else None,
            "long_running": run_days is not None and run_days >= LONG_RUNNING_DAYS, "reach": ad.get("reach"),
            "platforms": ad.get("platforms") or [], "observed_signals": ad.get("observed_signals") or {}}
    limits = ["No sales, spend or conversion data: a long-running or widely-seen ad may still lose money."]
    if ad.get("reach") is not None:
        limits.append("Reach is people who saw it in the EU, not buyers.")
    if ad.get("observed_signals"):
        limits.append("Likes/CTR labels copied from the source are that platform's own signals, not our sales.")
    if not ad.get("stop") and ad.get("start"):
        limits.append("No stop date reported: it may still be running, or the source may not say.")
    test = [k for k, v in el.items() if isinstance(v, dict) and v.get("worth_testing")]
    return {"hook": hook, "hook_type": kind if hook else None, "concept": CONCEPT_FOR.get(kind) if hook else None,
            "elements": el, "performance": perf, "limitations": limits, "worth_testing": test,
            "observed_count": sum(1 for v in el.values() if isinstance(v, dict) and v.get("observed")),
            "not_observable": sorted(k for k, v in el.items() if isinstance(v, dict) and not v.get("observed") and v.get("label") != E.HYPOTHESIS),
            "simulated_video": bool(video and video.get("simulated"))}


def principles(store, business_id, keyword):
    """What the analysed ads for a keyword have in common. Ads that kept running count double, but this
    is a HYPOTHESIS to test, never proof: run length isn't profit."""
    rows = store.find("ad_analyses", business_id=business_id, keyword=keyword, limit=500)
    if not rows:
        return []
    counts, examples = {}, {}
    for r in rows:
        a = r["data"]
        kind = a.get("hook_type")
        if not kind or kind == "other":
            continue
        w = 2 if a["performance"].get("long_running") else 1
        counts[kind] = counts.get(kind, 0) + w
        examples.setdefault(kind, []).append(r["ad_id"])
    total = sum(counts.values()) or 1
    out = []
    for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        long_n = sum(1 for r in rows if r["data"].get("hook_type") == kind and r["data"]["performance"].get("long_running"))
        out.append({"principle": f"{kind.replace('_', ' ')} openings", "hook_type": kind, "concept": CONCEPT_FOR.get(kind),
                    "why": WHY.get(kind), "ads": len(examples[kind]), "long_running_ads": long_n, "weight": round(n / total, 2),
                    "example_ad_ids": examples[kind][:5], "label": E.HYPOTHESIS,
                    "usable": CONCEPT_FOR.get(kind) is not None})
    paced = [r["data"]["elements"]["scenes_pacing"]["value"]["avg_shot_s"] for r in rows
             if r["data"]["elements"]["scenes_pacing"].get("observed") and r["data"]["elements"]["scenes_pacing"]["value"].get("avg_shot_s")]
    if paced:
        out.append({"principle": f"median shot length {statistics.median(paced):.1f}s across {len(paced)} measured videos",
                    "hook_type": None, "concept": None, "why": "Our renders should cut at least this often.", "ads": len(paced),
                    "long_running_ads": None, "weight": None, "example_ad_ids": [], "label": E.HYPOTHESIS, "usable": True,
                    "median_shot_s": round(statistics.median(paced), 2)})
    return out


def too_close(text, ad_texts, max_ratio=0.6, max_shared_words=5):
    """True if `text` copies an analysed ad (near-identical, or shares a run of more than 5 words)."""
    t = " ".join(str(text).lower().split())
    words = t.split()
    grams = {" ".join(words[i:i + max_shared_words + 1]) for i in range(len(words) - max_shared_words)}
    for other in ad_texts:
        o = " ".join(str(other).lower().split())
        if not o:
            continue
        if SequenceMatcher(None, t, o).ratio() > max_ratio:
            return True
        if any(g in o for g in grams):
            return True
    return False


def ad_texts(store, business_id, keyword):
    return [" ".join(a["data"].get("bodies") or []) for a in store.find("ads", business_id=business_id, keyword=keyword, limit=500)]


def _store_ad(ctx, keyword, ad, fid):
    now, bid = ctx.now(), ctx.business_id
    row = ctx.store.one("ads", business_id=bid, source=ad["source"], external_id=ad["external_id"])
    if row:
        ctx.store.update("ads", row["id"], data_json=ad, fetch_id=fid, last_seen=now, keyword=keyword)
        aid = row["id"]
    else:
        aid = ctx.store.insert("ads", business_id=bid, source=ad["source"], external_id=ad["external_id"], keyword=keyword,
                               advertiser=ad.get("advertiser"), data_json=ad, fetch_id=fid, first_seen=now, last_seen=now,
                               simulated=ctx.simulated or str(ad["source"]).startswith("mock"))
    if fid:
        E.add(ctx.store, bid, "ad", aid, E.FACT, "ad_seen",
              f"{ad.get('advertiser') or 'An advertiser'} ad on {ad['source']}" + (f", running since {ad['start']}" if ad.get("start") else ""),
              value={"start": ad.get("start"), "stop": ad.get("stop"), "reach": ad.get("reach")}, fetch_id=fid,
              quote=E.item_quote(ad), confidence=0.8)
    return aid


def run(ctx, inp):
    kws = inp.get("keywords")
    if not kws:
        opps = ctx.store.find("opportunities", business_id=ctx.business_id, order="rank", limit=ctx.limits["keywords_per_cycle"])
        kws = [o["keyword"] for o in opps]
    today = dt.date.fromtimestamp(ctx.now())
    out, notes = [], []
    for kw in kws:
        ctx.check()
        ok, failed, n_ads, n_an = [], [], 0, 0
        for src in AD_SOURCES:
            got = sources.call(ctx, src, "ads", kw, limit=ctx.limits["ads_per_keyword"])
            if got is None:
                failed.append(src)
                continue
            ok.append(src)
            ads, fid = got
            for ad in ads:
                ctx.check()
                aid = _store_ad(ctx, kw, ad, fid)
                n_ads += 1
                if ctx.store.one("ad_analyses", ad_id=aid, model_version=ANALYSER):
                    ctx.store.update("ad_analyses", ctx.store.one("ad_analyses", ad_id=aid, model_version=ANALYSER)["id"],
                                     data_json=analyse(ad, today), created_at=ctx.now())
                else:
                    ctx.store.insert("ad_analyses", business_id=ctx.business_id, ad_id=aid, keyword=kw, data_json=analyse(ad, today),
                                     created_at=ctx.now(), model_version=ANALYSER, simulated=ctx.simulated)
                n_an += 1
        p = principles(ctx.store, ctx.business_id, kw)
        if not ok:
            notes.append(f"{kw}: no ad source answered ({', '.join(failed)}); see Sources for what each needs")
        out.append({"keyword": kw, "ads": n_ads, "analysed": n_an, "sources_ok": ok, "sources_failed": failed, "principles": len(p)})
    return {"keywords": out, "notes": notes}
