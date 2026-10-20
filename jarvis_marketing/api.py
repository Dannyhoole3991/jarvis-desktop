"""
The Marketing dashboard's API, shared by the Jarvis window (jarvis_v2/server.py, token auth) and the
laptop server (remote.py, password login). Read routes return records as stored; nothing is
recomputed or rounded differently on the way out, so the dashboard always matches the records.

Who may do what:
  * reading: any authenticated caller
  * emergency STOP: any authenticated caller (stopping is always safe)
  * everything else (approve/reject, mode, resume, run a cycle, schedule, stop an experiment,
    reject a product): only a HumanPrincipal, i.e. Danny pressing a button in the window or on
    the laptop. Jarvis's brain and the agents never get one.
"""

import os
import re
import threading
import time

from jarvis_marketing import config, controls as C
from jarvis_marketing.agents import testing
from jarvis_marketing.director import Busy, Director, Scheduler


class Department:
    """Every business's director, plus the background work started from the dashboard."""

    def __init__(self, businesses=None, directors=None):
        self.directors = directors or {b["slug"]: Director(b) for b in (businesses or config.load_businesses())}
        self.schedulers = []
        self.background = {}           # slug -> {"what", "started", "status", "result"}
        self.lock = threading.Lock()

    def director(self, slug):
        if slug not in self.directors:
            raise ValueError("No such business.")
        return self.directors[slug]

    def start(self, notify=None, schedule=True):
        """Recover interrupted work, then start each business's scheduler (which does nothing until Danny switches it on)."""
        for d in self.directors.values():
            d.notify_hook = notify
            try:
                d.recover()
            except Exception as error:
                d.store.notify(f"Recovery after restart had a problem: {str(error)[:200]}", "urgent", d.bid)
            if schedule:
                self.schedulers.append(Scheduler(d).start())
        return self

    def stop(self):
        for s in self.schedulers:
            s.stop()

    def run_in_background(self, slug, what, fn):
        with self.lock:
            job = self.background.get(slug)
            if job and job["status"] == "running":
                raise Busy(f"Already busy: {job['what']}.")
            job = {"what": what, "started": time.time(), "status": "running", "result": None}
            self.background[slug] = job

        def work():
            try:
                job["result"] = fn()
                job["status"] = "done"
            except Exception as error:
                job["result"] = str(error)[:300]
                job["status"] = "failed"
        threading.Thread(target=work, daemon=True, name=f"jarvis-marketing-{slug}").start()
        return job


# ------------------------------------------------------------------ views
def _ago(store, t):
    return None if not t else round(store.clock() - t)


def overview(dept, slug):
    d = dept.director(slug)
    s, bid = d.store, d.bid
    cands = s.find("candidates", business_id=bid, order="score DESC", limit=200)
    briefs = s.find("briefs", business_id=bid, limit=500)
    creatives = s.find("creatives", business_id=bid, order="id DESC", limit=200)
    reviews = {}
    for r in s.find("qa_reviews", business_id=bid, limit=2000):
        reviews[r["creative_id"]] = r                      # the latest review per creative
    posts = s.find("posts", business_id=bid, order="id DESC", limit=200)
    approvals = s.find("approvals", business_id=bid, order="id DESC", limit=200)
    orders = s.find("orders", business_id=bid, order="created_at DESC", limit=500)
    experiments = s.find("experiments", business_id=bid, order="id DESC", limit=50)
    actual_rev = round(sum(o["total"] for o in orders), 2)
    attributed = [o for o in orders if o["post_id"]]
    sessions = s.sql("SELECT post_id, value FROM metrics m WHERE business_id=? AND metric='sessions' AND available=1 AND "
                     "collected_at=(SELECT MAX(collected_at) FROM metrics WHERE post_id=m.post_id AND metric='sessions')", (bid,))
    total_sessions = sum(r["value"] for r in sessions) if sessions else None
    unit = {e["id"]: (e["data"] or {}).get("unit_contribution_estimate") for e in experiments}
    est_contribution = round(sum((unit.get(s.get("posts", o["post_id"])["experiment_id"]) or 0) for o in attributed), 2)
    pending = [a for a in approvals if a["status"] == "pending"]
    post_by_approval = {p["approval_id"]: p for p in posts if p["approval_id"]}
    return {
        "business": {k: d.business[k] for k in ("slug", "name", "public_url", "currency", "profile")},
        "simulated": d.adapters.simulated,
        "controls": d.controls.summary(bid),
        "adapters": d.adapters.describe(),
        "background": dept.background.get(slug),
        "cycles": s.find("cycles", business_id=bid, order="id DESC", limit=10),
        "tasks": [{k: t[k] for k in ("id", "cycle_id", "agent", "kind", "status", "error", "attempts", "max_attempts",
                                     "created_at", "started_at", "finished_at")} for t in s.find("tasks", business_id=bid, order="id DESC", limit=40)],
        "notifications": s.find("notifications", business_id=bid, order="id DESC", limit=40),
        "opportunities": s.find("opportunities", business_id=bid, order="rank", limit=50),
        "candidates": [{"id": c["id"], "title": c["title"], "status": c["status"], "score": c["score"], "reasons": c["reasons"],
                        "keyword": (c["data"] or {}).get("keyword"), "simulated": bool(c["simulated"]),
                        "card": (c["data"] or {}).get("card"), "listing": (c["data"] or {}).get("listing"),
                        "risks": (c["data"] or {}).get("risks"), "gaps": (c["data"] or {}).get("gaps")} for c in cands],
        "briefs": [{"id": b["id"], "candidate_id": b["candidate_id"], "concept": b["concept"], "status": b["status"],
                    "label": b["label"], "hook": b["data"]["hook"], "hypothesis": b["data"]["hypothesis"],
                    "scenes": b["data"]["scenes"], "caption": b["data"]["caption"], "destination": b["data"]["destination"],
                    "test_variable": b["data"]["test_variable"], "duration_s": b["data"]["duration_s"]} for b in briefs],
        "creatives": [{"id": c["id"], "brief_id": c["brief_id"], "candidate_id": c["candidate_id"], "status": c["status"],
                       "revision": c["revision"], "provider": c["provider"], "creative_uid": c["creative_uid"],
                       "simulated": bool(c["simulated"]), "has_video": bool((c["files"] or {}).get("video")),
                       "technical": (c["checks"] or {}).get("technical"), "log": c["log"],
                       "qa": ({"passed": bool(reviews[c["id"]]["passed"]), "overall": reviews[c["id"]]["overall"],
                               "scores": reviews[c["id"]]["scores"], "fixes": reviews[c["id"]]["fixes"],
                               "label": reviews[c["id"]]["label"]} if c["id"] in reviews else None)} for c in creatives],
        "approvals_pending": [dict(a, post=post_by_approval.get(a["id"])) for a in pending],
        "approvals_recent": [a for a in approvals if a["status"] != "pending"][:30],
        "posts": posts,
        "experiments": experiments,
        "actual": {"label": "actual (from records)", "orders": len(orders), "revenue": actual_rev,
                   "attributed_orders": len(attributed), "attributed_revenue": round(sum(o["total"] for o in attributed), 2),
                   "spend": 0.0, "sessions": total_sessions,
                   "cvr": round(len(attributed) / total_sessions, 4) if total_sessions else None,
                   "cac": None, "cac_note": "No paid spend, so there is no acquisition cost to report."},
        "estimated": {"label": "estimate", "contribution_from_attributed_orders": est_contribution,
                      "basis": "attributed orders x each experiment's estimated unit contribution"},
        "verdicts": {v: sum(1 for e in experiments if e["verdict"] == v) for v in ("winner", "no_winner", "inconclusive", "insufficient_data")},
        "lessons": s.find("lessons", business_id=bid, order="id DESC", limit=50),
        "reports": s.find("reports", business_id=bid, order="id DESC", limit=10),
        "errors": [t for t in s.find("tasks", business_id=bid, status=["failed", "timed_out"], order="id DESC", limit=20)],
        "audit": s.find("audit", business_id=bid, order="id DESC", limit=40),
        "reconcile_problems": testing.reconcile(s, bid),
        "now": s.clock(),
    }


def describe(dept, slug=None):
    """A spoken summary for Jarvis's brain (read-only)."""
    out = []
    for d in ([dept.director(slug)] if slug else dept.directors.values()):
        v = overview(dept, d.business["slug"])
        sim = "SIMULATED data. " if v["simulated"] else ""
        c = v["controls"]
        pend = len(v["approvals_pending"])
        out.append(f"{d.business['name']} marketing: {sim}mode {c['mode']}"
                   + (", EMERGENCY STOP ON" if c["emergency_stop"]["on"] else "")
                   + f". {sum(1 for x in v['candidates'] if x['status'] in ('shortlisted', 'in_creative', 'testing'))} products in play, "
                   f"{pend} post(s) waiting for Danny's approval, {v['actual']['attributed_orders']} orders attributed to posts. "
                   "Paid ads, purchases and price changes are off.")
    return " ".join(out)


def media_path(dept, slug, creative_id):
    """The rendered video for a creative, only from the department's own assets folder."""
    d = dept.director(slug)
    c = d.store.get("creatives", creative_id)
    if not c or c["business_id"] != d.bid:
        raise ValueError("No such creative.")
    path = (c["files"] or {}).get("video")
    if not path:
        raise ValueError("This creative has no video file (it may be a SIMULATED render).")
    root = os.path.realpath(config.assets_dir(d.business["profile"]))
    real = os.path.realpath(path)
    if not real.startswith(root + os.sep) or not real.lower().endswith(".mp4") or not os.path.isfile(real):
        raise ValueError("That file isn't a marketing render.")
    return real


# ------------------------------------------------------------------ routes
def handle_get(dept, path, query):
    parts = path.strip("/").split("/")             # api, marketing, ...
    if parts[:2] != ["api", "marketing"]:
        return None
    if len(parts) == 2:
        return {"businesses": [{"slug": s, "name": d.business["name"], "profile": d.business["profile"],
                                "pending": [a["summary"] for a in d.approvals.pending(d.bid)],
                                "stopped": d.controls.stopped()["on"]} for s, d in dept.directors.items()],
                "describe": describe(dept)}
    slug = parts[2]
    if len(parts) == 3:
        return overview(dept, slug)
    d = dept.director(slug)
    if parts[3] == "candidates" and len(parts) == 5:
        c = d.store.get("candidates", int(parts[4]))
        if not c or c["business_id"] != d.bid:
            raise ValueError("No such product.")
        key = f"{c['supplier']}:{c['supplier_product_id']}"
        opp = d.store.get("opportunities", c["opportunity_id"]) if c["opportunity_id"] else None
        ev = d.store.find("evidence", subject_type="candidate", subject_key=key)
        if opp:
            ev += d.store.find("evidence", subject_type="opportunity", subject_key=opp["keyword"])
        return {"candidate": c, "evidence": ev}
    if parts[3] == "experiments" and len(parts) == 5:
        e = d.store.get("experiments", int(parts[4]))
        if not e or e["business_id"] != d.bid:
            raise ValueError("No such experiment.")
        return {"experiment": e, "posts": d.store.find("posts", experiment_id=e["id"]),
                "variants": testing.variant_numbers(_ReadCtx(d), e)}
    if parts[3] == "describe":
        return {"text": describe(dept, slug)}
    return None


class _ReadCtx:
    """Just enough of an agent Context for read-only helpers."""

    def __init__(self, d):
        self.store, self.business_id, self.testing = d.store, d.bid, d.business["testing"]

    def now(self):
        return self.store.clock()


def _human(principal):
    if not isinstance(principal, C.HumanPrincipal):
        raise PermissionError("Only Danny can do that, from the Marketing dashboard.")
    return principal


def handle_post(dept, path, body, principal=None):
    """principal: a HumanPrincipal when Danny pressed a button (window UI token or laptop login), else None."""
    parts = path.strip("/").split("/")
    if parts[:2] != ["api", "marketing"]:
        return None
    if parts[2:] == ["stop"]:
        by = str(principal) if principal else "jarvis"
        for d in dept.directors.values():
            d.controls.emergency_stop(by, str(body.get("reason") or "Stop pressed"), d.bid)
            break                                    # one global switch, recorded once
        return {"text": "Emergency stop is ON. Nothing external will run until Danny resumes."}
    if parts[2:] == ["resume"]:
        p = _human(principal)
        next(iter(dept.directors.values())).controls.resume(p, str(body.get("reason") or ""))
        return {"text": "Resumed. Modes and approvals are exactly as they were."}
    if len(parts) < 4:
        return None
    slug, d = parts[2], dept.director(parts[2])
    p = _human(principal)
    try:
        m = re.fullmatch(r"approvals/(\d+)/(approve|reject)", "/".join(parts[3:]))
        if m:
            a, post = d.decide_post(p, int(m.group(1)), m.group(2), str(body.get("note") or ""))
            if a["status"] == "approved" and post:
                if not d.controls.allowed(d.bid, "publish_organic"):
                    return {"text": f"Approved. It will post once the mode is TEST (now {d.controls.mode(d.bid)}). "
                                    "The approval lasts 24 hours.", "approval": a}
                dept.run_in_background(slug, f"posting post {post['id']}", lambda: d.publish_approved([post["id"]])["output"])
                return {"text": "Approved: posting it now. It will show as published when it's live.", "approval": a}
            return {"text": "Rejected: it won't be posted.", "approval": a}
        if parts[3] == "mode":
            mode = d.controls.set_mode(p, d.bid, str(body.get("mode", "")), str(body.get("reason") or ""))
            return {"text": f"Mode is now {mode}."}
        if parts[3] == "cycle":
            stages = body.get("stages") or None
            dept.run_in_background(slug, "a marketing cycle", lambda: d.run_cycle(stages))
            return {"text": "Running a marketing cycle now."}
        if parts[3] == "schedule":
            d.controls.set_schedule(p, d.bid, bool(body.get("enabled")), body.get("every_hours") or 24)
            return {"text": "Schedule " + ("on." if body.get("enabled") else "off (paused).")}
        m = re.fullmatch(r"experiments/(\d+)/stop", "/".join(parts[3:]))
        if m:
            e = d.store.get("experiments", int(m.group(1)))
            if not e or e["business_id"] != d.bid:
                raise ValueError("No such experiment.")
            for post in d.store.find("posts", experiment_id=e["id"], status="pending_approval"):
                d.decide_post(p, post["approval_id"], "reject", "experiment stopped")
            d.store.update("experiments", e["id"], status="stopped", ended_at=d.store.clock())
            d.store.audit(p, "stop_experiment", "ok", subject=f"experiment:{e['id']}", business_id=d.bid)
            return {"text": "Experiment stopped. Nothing more will be posted for it."}
        m = re.fullmatch(r"candidates/(\d+)/reject", "/".join(parts[3:]))
        if m:
            c = d.store.get("candidates", int(m.group(1)))
            if not c or c["business_id"] != d.bid:
                raise ValueError("No such product.")
            d.store.update("candidates", c["id"], status="rejected", reasons_json=(c["reasons"] or []) + [f"Rejected by Danny: {body.get('note') or 'no reason given'}"])
            d.store.audit(p, "reject_candidate", "ok", subject=f"candidate:{c['id']}", business_id=d.bid)
            return {"text": "Rejected. It won't be shortlisted again unless you change that."}
        if parts[3] == "notifications" and parts[4:] == ["seen"]:
            with d.store.lock, d.store.conn() as conn:
                conn.execute("UPDATE notifications SET seen=1 WHERE business_id=?", (d.bid,))
            return {"ok": True}
    except C.PermissionDenied as error:
        raise PermissionError(str(error))
    except Busy as error:
        raise ValueError(str(error))
    return None
