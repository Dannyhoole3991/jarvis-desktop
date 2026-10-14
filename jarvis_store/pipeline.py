"""
Research a product on CJ, then create it in Shopify as a DRAFT with honest
copy, real-cost pricing and a TikTok video, for Danny to review.

    add_product(keyword=None, kind="everyday", dry_run=False)

Nothing here publishes anything. The finished draft is listed in Jarvis's
"waiting on Danny" queue and gets a REVIEW.md next to its video.
"""

import json
import os
import re
import urllib.request
from difflib import SequenceMatcher

from . import config, copywriter, pricing
from .cj import CJ, CJError
from .shopify import Shopify, ShopError, audit

# Everyday product ideas Jarvis rotates through when not given a keyword.
SEED_KEYWORDS = [
    "cable organizer", "drawer organizer", "kitchen gadget", "vegetable peeler", "jar opener", "cord holder",
    "phone stand", "lint remover", "shoe brush", "travel bottle", "cutlery organizer", "magnetic hook",
    "fridge organizer", "sink caddy", "spice rack", "herb scissors", "egg separator", "bag clip",
    "desk organizer", "makeup brush cleaner", "plant mister", "key holder", "under shelf basket", "soap dispenser",
]

# Christmas decoration ideas for kind="christmas" (classy home decor, matching the existing collection).
CHRISTMAS_SEED_KEYWORDS = [
    "christmas tree topper", "christmas garland", "christmas table runner", "christmas cushion cover",
    "christmas stocking", "christmas candle holder", "christmas gnome", "christmas wreath", "christmas ornament set",
    "christmas snow globe", "christmas napkin ring", "christmas window decoration",
]

# Things Jarvis must never pick: UK-plug problems, regulated/safety-critical goods, liquids, adult items.
_DENY_BASE = (
    r"eu plug|us plug|uk plug|au plug|110v|220v|240v|mains|adapter|charger plug|baby|infant|toddler|kid|kids|"
    r"child|children|toy|knife|knives|blade|razor|weapon|gun|pepper spray|vape|cigarette|e-cig|sex|adult|lingerie|"
    r"supplement|capsule|medicine|medical|serum|cream|lotion|perfume|liquid|oil|e-liquid|lithium battery|"
    r"food|edible|drink|contact lens|helmet|car seat|airbag|tattoo|piercing|whitening|hair removal|laser|"
    r"halloween|easter|valentine")
DENY = re.compile(r"(?i)\b(" + _DENY_BASE + r"|christmas|xmas)\b")      # everyday: no seasonal stock
DENY_CHRISTMAS = re.compile(r"(?i)\b(" + _DENY_BASE + r")\b")          # christmas: only the safety list


def _deny(kind):
    return DENY_CHRISTMAS if kind == "christmas" else DENY


class PipelineError(Exception):
    pass


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50]


def _split_options(option_names, variants):
    """CJ gives 'Color-Size' and keys like 'Black-XL'; turn them into Shopify options/variants."""
    names = [n.strip() for n in option_names.split("-") if n.strip()] if option_names else []
    names = ["Colour" if n.lower() == "color" else n.capitalize() for n in names]
    if len(variants) <= 1 or not names:
        return [], [dict(v, options={}) for v in variants[:1]]
    out = []
    for v in variants:
        parts = [p.strip() for p in (v["key"] or v["name"]).split("-")]
        if len(parts) != len(names):
            names, parts = ["Style"], [(v["key"] or v["name"]).strip()]
        out.append(dict(v, options=dict(zip(names, parts))))
    if len(names) == 1 and len({tuple(v["options"].items()) for v in out}) != len(out):
        return [], out[:1]
    options = [{"name": n, "values": list(dict.fromkeys(v["options"][n] for v in out))} for n in names]
    return options, out


def _too_similar(title, existing):
    t = title.lower()
    return any(SequenceMatcher(None, t, e.lower()).ratio() > 0.7 for e in existing)


def evaluate(cj, pid, kind="everyday"):
    """Full check of one CJ product. Returns a plan dict, or raises PipelineError with the reason it was rejected."""
    d = cj.details(pid)
    bad = _deny(kind).search(d["name"] + " " + d["category"])
    if bad:
        raise PipelineError(f"not suitable ({bad.group(0)})")
    if len(d["images"]) < config.MIN_IMAGES:
        raise PipelineError("too few photos")
    variants = [v for v in d["variants"] if v["vid"] and v["price_usd"] > 0][:12]
    if not variants:
        raise PipelineError("no buyable variants")
    if max(v["price_usd"] for v in variants) > config.MAX_PRODUCT_USD:
        raise PipelineError("too expensive from CJ")
    heaviest = max(variants, key=lambda v: v["weight_g"])
    options_ship = [o for o in cj.uk_shipping(heaviest["vid"]) if o["max_days"] <= config.MAX_DELIVERY_DAYS]
    if not options_ship:
        raise PipelineError(f"no UK shipping within {config.MAX_DELIVERY_DAYS} days")
    ship = options_ship[0]
    options, variants = _split_options(d["option_names"], variants)
    priced = []
    for v in variants:
        p = pricing.price_for(v["price_usd"], ship["usd"], kind)
        if p["price_gbp"] > config.MAX_SELL_PRICE_GBP:
            raise PipelineError(f"would sell for £{p['price_gbp']}, above the £{config.MAX_SELL_PRICE_GBP:.0f} cap")
        priced.append(dict(v, **p))
    title = copywriter.clean_title(d["name"])
    facts = copywriter.facts_from_description(d["description"])
    if d["material"] and "material" not in facts:
        facts["material"] = d["material"]
    return {
        "kind": kind, "cj": {"pid": d["pid"], "spu": d["spu"], "name": d["name"]},
        "title": title, "options": options, "variants": priced, "shipping": ship,
        "description_html": copywriter.description_html(title, facts, options, (ship["min_days"], ship["max_days"])),
        "images": d["images"][:8], "video_lines": copywriter.video_lines(title, options),
        "caption": copywriter.tiktok_caption(title, min(v["price_gbp"] for v in priced)),
    }


def find_candidate(cj, shop, keyword=None, kind="everyday", log=print):
    existing_spus = shop.existing_cj_spus() if shop else set()
    existing_titles = shop.existing_titles() if shop else []
    keywords = [keyword] if keyword else (CHRISTMAS_SEED_KEYWORDS if kind == "christmas" else SEED_KEYWORDS)
    for kw in keywords:
        results = [r for r in cj.search(kw) if r["listed"] >= config.MIN_LISTED and r["price_usd"] <= config.MAX_PRODUCT_USD]
        for r in results[:8]:
            spu = (r["sku"] or "")[:11].upper()
            if spu and spu in existing_spus:
                continue
            if _deny(kind).search(r["name"]) or _too_similar(copywriter.clean_title(r["name"]), existing_titles):
                continue
            try:
                plan = evaluate(cj, r["id"], kind)
            except (PipelineError, CJError, ValueError) as reason:
                log(f"  skipped '{r['name'][:50]}': {reason}")
                continue
            log(f"  picked '{plan['title']}' (listed by {r['listed']} stores)")
            return plan
        if keyword:
            break
    raise PipelineError("No suitable product found that passes every check.")


def _download(url, path):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(path, "wb") as f:
        f.write(r.read())


def make_assets(plan, folder, video=True):
    os.makedirs(folder, exist_ok=True)
    local = []
    for i, url in enumerate(plan["images"][:6], 1):
        path = os.path.join(folder, f"p{i:02d}.jpg")
        try:
            _download(url, path)
            local.append(path)
        except Exception:
            continue
    spec = {"images": local, "lines": plan["video_lines"], "brand": config.BRAND,
            "link": config.SHOP_PUBLIC_URL, "output": os.path.join(folder, "tiktok.mp4")}
    with open(os.path.join(folder, "spec.json"), "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=1)
    if video and len(local) >= 2:
        import jarvis_video
        jarvis_video.make_video(spec["images"], spec["lines"], spec["brand"], spec["link"], spec["output"])
    with open(os.path.join(folder, "caption.txt"), "w", encoding="utf-8") as f:
        f.write(plan["caption"] + "\n")
    return spec


def write_review(plan, folder, created=None):
    lines = [f"# Review: {plan['title']}", "",
             f"Status: **DRAFT** (not visible to customers)" + (f" | [Open in Shopify]({created['admin_url']})" if created else ""),
             f"CJ product: {plan['cj']['name']} (pid {plan['cj']['pid']}, SPU {plan['cj']['spu']})",
             f"Shipping: {plan['shipping']['name']}, {plan['shipping']['min_days']}-{plan['shipping']['max_days']} days, "
             f"${plan['shipping']['usd']:.2f}", "",
             "| Variant | CJ cost + post (£) | Fees (£) | Price (£) | Profit (£) |", "|---|---|---|---|---|"]
    for v in plan["variants"]:
        name = " / ".join(v["options"].values()) or "Single"
        lines.append(f"| {name} | {v['cost_gbp']:.2f} | {v['fee_gbp']:.2f} | {v['price_gbp']:.2f} | {v['profit_gbp']:.2f} |")
    lines += ["", "## Before publishing", "- Check the photos and description match the product.",
              "- In CJ, connect this product (Products > Connection) so orders go to CJ automatically "
              "(the variant SKUs already match CJ's).",
              "- Then set it Active in Shopify.", "", "## TikTok caption", "```", plan["caption"], "```"]
    with open(os.path.join(folder, "REVIEW.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def add_product(keyword=None, kind="everyday", dry_run=False, video=True, cj=None, shop=None, log=print):
    """Research one product and add it to Shopify as a DRAFT. Returns a summary dict."""
    cj = cj or CJ()
    shop = shop if shop is not None else (None if dry_run else Shopify())
    log("Researching on CJ...")
    plan = find_candidate(cj, shop, keyword, kind, log)
    folder = os.path.join(config.DRAFTS_DIR, _slug(plan["title"]))
    created = None
    if not dry_run:
        tags = [f"cj:{plan['cj']['spu']}"] + (["christmas"] if kind == "christmas" else [])
        created = shop.create_draft_product(
            plan["title"], plan["description_html"], "Christmas Decorations" if kind == "christmas" else "Home & Living",
            tags, plan["options"],
            [{"options": v["options"], "price": f"{v['price_gbp']:.2f}", "sku": v["sku"]} for v in plan["variants"]],
            [{"url": u, "alt": plan["title"]} for u in plan["images"]])
        log(f"Draft created in Shopify: {created['admin_url']}")
    log("Making the video...")
    make_assets(plan, folder, video=video)
    write_review(plan, folder, created)
    if not dry_run:
        try:
            import jarvis_business as jbiz
            jbiz.queue_human_action("review_store_draft",
                                    f"Review Jarvis's draft product '{plan['title']}' (folder {folder}) and publish it if happy.")
        except Exception:
            pass
    audit("pipeline_done", title=plan["title"], dry_run=dry_run, folder=folder,
          product_id=created["id"] if created else None)
    return {"title": plan["title"], "draft": created, "folder": folder, "dry_run": dry_run,
            "prices": [v["price_gbp"] for v in plan["variants"]], "shipping_days": plan["shipping"]["max_days"]}
