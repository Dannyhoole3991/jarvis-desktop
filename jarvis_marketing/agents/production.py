"""
Video Production agent: renders each brief with the configured provider, using ONLY the product's
own photos (from the brief's product identity), and checks the result technically.

If an accurate video can't be made (no real photos, provider unavailable), the creative is marked
needs_asset and Danny is asked for one. Another product's images are never substituted.
Revisions apply QA's fixes (shorter captions, faster hook, shorter cut), capped by max_revisions.
"""

import hashlib
import json
import os

from jarvis_marketing import config
from jarvis_marketing.adapters import Unavailable
from jarvis_marketing.agents import qa

INPUT = {"brief_ids?": [int], "revise?": [{"creative_id": int, "fixes": [str]}]}
OUTPUT = {"creatives": [{"creative_id": int, "brief_id": int, "status": str, "revision": int}], "needs_asset": [int],
          "errors": [str]}
CAPTION_MAX = qa.CAPTION_MAX_CHARS


def spec_for(ctx, brief, scenes=None):
    b = brief["data"]
    scenes = scenes or b["scenes"]
    return {"images": list(b["product_identity"]["images"]), "lines": [s["vo"] for s in scenes],
            "on_screen": [s["on_screen_text"] for s in scenes], "scenes": scenes,
            "duration_s": round(sum(s["seconds"] for s in scenes), 1), "brand": "Collective",
            "link_text": ctx.business.get("public_url"), "voice": None,
            "product_identity": b["product_identity"]}


def _uid(brief_id, revision, spec):
    raw = json.dumps({"b": brief_id, "r": revision, "s": spec}, sort_keys=True)
    return "CR-" + hashlib.sha256(raw.encode()).hexdigest()[:20]


def apply_fixes(scenes, fixes):
    """Mechanical fixes only. Returns (new scenes, fixes applied)."""
    scenes = [dict(s) for s in scenes]
    done = []
    if "captions_too_long" in fixes:
        for s in scenes:
            words, out = s["on_screen_text"].split(), []
            for w in words:
                if len(" ".join(out + [w])) > CAPTION_MAX:
                    break
                out.append(w)
            s["on_screen_text"] = " ".join(out) or s["on_screen_text"][:CAPTION_MAX]
        done.append("captions_too_long")
    if "hook_slow" in fixes and scenes:
        scenes[0]["seconds"] = min(scenes[0]["seconds"], 2.0)
        done.append("hook_slow")
    if "too_long" in fixes and len(scenes) > 3:
        scenes.pop(len(scenes) // 2)
        done.append("too_long")
    for i, s in enumerate(scenes):
        s["n"] = i + 1
    return scenes, done


def render(ctx, brief, revision=0, scenes=None, fixes_applied=()):
    spec = spec_for(ctx, brief, scenes)
    ident = brief["data"]["product_identity"]
    out_dir = os.path.join(config.assets_dir(ctx.business["profile"]), f"brief-{brief['id']}")
    now = ctx.now()
    base = {"business_id": ctx.business_id, "brief_id": brief["id"], "candidate_id": brief["candidate_id"],
            "provider": ctx.adapters.video.name, "revision": revision, "created_at": now, "updated_at": now,
            "simulated": ctx.simulated}
    spec["creative_uid"] = _uid(brief["id"], revision, {k: v for k, v in spec.items() if k != "creative_uid"})
    existing = ctx.store.one("creatives", creative_uid=spec["creative_uid"])
    if existing:
        return existing["id"], existing["status"]
    if not spec["images"]:
        cid = ctx.store.insert("creatives", status="needs_asset", creative_uid=spec["creative_uid"], spec_json=spec, files_json={},
                               checks_json={}, log="No real product photos for this product: Danny, please add some.", **base)
        ctx.store.notify(f"Video for '{ident['title']}' needs product photos: none are on file, and I won't use another product's.",
                         "needs_you", ctx.business_id)
        return cid, "needs_asset"
    try:
        result = ctx.adapters.video.render(spec, out_dir)
    except Unavailable as why:
        cid = ctx.store.insert("creatives", status="needs_asset", creative_uid=spec["creative_uid"], spec_json=spec, files_json={},
                               checks_json={}, log=f"Couldn't render accurately: {why}", **base)
        ctx.store.notify(f"Video for '{ident['title']}' couldn't be made accurately ({why}). Please send a usable asset.",
                         "needs_you", ctx.business_id)
        return cid, "needs_asset"
    checks = qa.technical(result["probe"], spec)
    status = "rendered" if all(c["ok"] for c in checks.values()) else "render_failed"
    log = result.get("log", "") + (f"\nFixes applied: {', '.join(fixes_applied)}" if fixes_applied else "")
    cid = ctx.store.insert("creatives", status=status, creative_uid=spec["creative_uid"], spec_json=spec,
                           files_json=result["files"], checks_json={"probe": result["probe"], "technical": checks}, log=log[-4000:], **base)
    return cid, status


def run(ctx, inp):
    made, needs_asset, errors = [], [], []
    for item in inp.get("revise") or []:
        ctx.check()
        old = ctx.store.get("creatives", item["creative_id"])
        if old["revision"] >= ctx.testing["max_revisions"]:
            ctx.store.update("creatives", old["id"], status="rejected", updated_at=ctx.now())
            errors.append(f"creative {old['id']}: revision limit reached, rejected")
            continue
        scenes, done = apply_fixes(old["spec"]["scenes"], item["fixes"])
        if not done:
            ctx.store.update("creatives", old["id"], status="rejected", updated_at=ctx.now())
            errors.append(f"creative {old['id']}: QA's fixes need a new concept, not a re-render; rejected")
            continue
        brief = ctx.store.get("briefs", old["brief_id"])
        ctx.store.update("creatives", old["id"], status="superseded", updated_at=ctx.now())
        cid, status = render(ctx, brief, old["revision"] + 1, scenes, done)
        made.append({"creative_id": cid, "brief_id": brief["id"], "status": status, "revision": old["revision"] + 1})
    ids = inp.get("brief_ids")
    briefs = [ctx.store.get("briefs", i) for i in ids] if ids else ctx.store.find("briefs", business_id=ctx.business_id, status="ready")
    for brief in briefs:
        ctx.check()
        try:
            cid, status = render(ctx, brief)
        except Exception as error:
            errors.append(f"brief {brief['id']}: {str(error)[:200]}")
            continue
        ctx.store.update("briefs", brief["id"], status="produced")
        if status == "needs_asset":
            needs_asset.append(brief["id"])
        made.append({"creative_id": cid, "brief_id": brief["id"], "status": status, "revision": 0})
    return {"creatives": made, "needs_asset": needs_asset, "errors": errors}
