"""The agents and the director, end to end on SIMULATED adapters."""

import inspect
import os
import threading
import time
import unittest

from jarvis_marketing import controls as C, evidence as E, scoring
from jarvis_marketing.adapters import Disabled, Unavailable
from jarvis_marketing.adapters.mock import MockSupplier, Unsupported
from jarvis_marketing.agents import base, testing
from jarvis_marketing.director import STAGES, Busy, Director, Scheduler
from jarvis_marketing.tests.helpers import Clock, business, temp_store

DANNY = C.HumanPrincipal("Danny", "test")
DAY = 86400


def make(clock=None, **over):
    clock = clock or Clock()
    store, folder = temp_store(clock)
    os.environ["JARVIS_MARKETING_DATA"] = folder        # rendered (simulated) assets go to the temp folder
    from jarvis_marketing import config
    config.DATA_ROOT = folder
    b = business(**over)
    return Director(b, store=store), clock


def to_mode(d, mode):
    order = [C.RESEARCH, C.REVIEW, C.TEST]
    cur = d.controls.mode(d.bid)
    for m in order[order.index(cur) + 1: order.index(mode) + 1]:
        d.controls.set_mode(DANNY, d.bid, m)


def prepared(**over):
    """A director that has researched, evaluated, briefed, rendered, QA'd and designed experiments (REVIEW mode)."""
    d, clock = make(**over)
    to_mode(d, C.REVIEW)
    r = d.run_cycle(["research", "evaluate", "brief", "produce", "qa", "design"])
    return d, clock, r


class CycleTests(unittest.TestCase):
    def test_research_mode_runs_research_and_blocks_the_rest_explicitly(self):
        d, _ = make()
        r = d.run_cycle()
        by = {t["stage"]: t for t in r["tasks"]}
        for s in ("research", "evaluate", "collect", "judge", "learn"):
            self.assertEqual(by[s]["status"], "done", s)
        for s in ("brief", "produce", "qa", "design", "publish"):
            self.assertEqual(by[s]["status"], "blocked", s)
            self.assertIn("RESEARCH", by[s]["error"])

    def test_structured_exchange_every_task_output_matches_its_schema(self):
        d, _, r = prepared()
        for t in r["tasks"]:
            task = d.store.get("tasks", t["task_id"])
            self.assertEqual(task["status"], "done", (t, task["error"]))
            out = dict(task["output"])
            out.pop("log", None)
            base.validate(out, STAGES[task["kind"]][4])

    def test_full_loop_with_approvals_reaches_lessons_and_reconciles(self):
        d, clock, _ = prepared()
        to_mode(d, C.TEST)
        for a in d.approvals.pending(d.bid):
            d.decide_post(DANNY, a["id"], "approve")
        out = d.publish_approved()["output"]
        self.assertTrue(out["published"])
        for _ in range(18):
            clock.advance(DAY)
            d.run_cycle(["collect", "judge", "learn"])
        exps = d.store.find("experiments")
        self.assertTrue(exps)
        for e in exps:
            self.assertEqual(e["status"], "learned")
            self.assertIn(e["verdict"], ("winner", "no_winner", "inconclusive"))
            if e["verdict"] != "winner":
                self.assertIsNone(e["result"]["winner"])
        lessons = d.store.find("lessons")
        self.assertTrue(lessons)
        self.assertTrue(all(l["simulated"] for l in lessons))
        self.assertTrue(all(l["label"] != E.VERIFIED for l in lessons))       # simulated data can never be "verified"
        exp_lessons = [l for l in lessons if l["experiment_id"]]
        self.assertTrue(all(l["evidence"]["post_ids"] and "metric_ids" in l["evidence"] for l in exp_lessons))
        self.assertEqual(testing.reconcile(d.store, d.bid), [])
        self.assertTrue(d.store.find("reports"))

    def test_cycle_task_cap_and_busy_guard(self):
        d, _ = make(limits=dict(business()["limits"], max_tasks_per_cycle=3))
        r = d.run_cycle()
        self.assertEqual(r["status"], "capped")
        self.assertEqual(len(r["tasks"]), 3)
        d.cycle_lock.acquire()
        try:
            with self.assertRaises(Busy):
                d.run_cycle()
        finally:
            d.cycle_lock.release()

    def test_emergency_stop_refuses_cycles_and_blocks_tasks(self):
        d, _ = make()
        d.controls.emergency_stop("danny", "test")
        with self.assertRaises(C.PermissionDenied):
            d.run_cycle()
        task = d.run_stage("research")
        self.assertEqual(task["status"], "blocked")


class ResearchTests(unittest.TestCase):
    def test_every_fact_traces_to_a_recorded_read_and_is_marked_simulated(self):
        d, _ = make()
        d.run_stage("research", {"keywords": ["phone stand"]})
        rows = d.store.find("evidence", label=["fact", "verified"])
        self.assertTrue(rows)
        for r in rows:
            f = d.store.get("fetches", r["fetch_id"])
            self.assertEqual(f["url"], r["source_url"])
            self.assertIn(" ".join(r["quote"].split()), " ".join(f["snapshot"].split()))
            self.assertTrue(r["simulated"])
            self.assertTrue(r["source_url"].startswith("mock://"))

    def test_unavailable_sources_are_gaps_never_numbers(self):
        d, _ = make()
        d.adapters.trends = Unsupported("trends", "no official API")
        d.adapters.shortform = Unsupported("shortform", "academic only")
        out = d.run_stage("research", {"keywords": ["phone stand"]})["output"]
        gaps = out["opportunities"][0]["gaps"]
        self.assertTrue(any("search demand" in g for g in gaps))
        self.assertTrue(any("short-form" in g for g in gaps))
        fields = {r["field"] for r in d.store.find("evidence")}
        self.assertNotIn("trend_12w", fields)
        self.assertNotIn("shortform_median_views", fields)

    def test_a_source_that_reports_numbers_it_didnt_read_fails_the_task(self):
        class Lying(MockSupplier):
            def details(self, pid):
                d, fid = super().details(pid)
                d = dict(d, variants=[dict(v, price_usd=0.01) for v in d["variants"]])     # not what was recorded
                return d, fid
        d, _ = make()
        d.adapters.supplier = Lying(d.adapters.world, d.adapters.supplier._recorder)
        task = d.run_stage("research", {"keywords": ["phone stand"]})
        self.assertEqual(task["status"], "failed")
        self.assertIn("FabricatedEvidence", task["error"])
        self.assertEqual(task["attempts"], 1)          # never retried: it's not a transient failure


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.d, _ = make()
        self.d.run_stage("research", {"keywords": ["phone stand", "kids night light", "cable organizer"]})
        self.out = self.d.run_stage("evaluate")["output"]

    def test_rejections_have_reasons_and_restricted_products_are_rejected(self):
        self.assertTrue(self.out["rejected"])
        self.assertTrue(all(r["reasons"] for r in self.out["rejected"]))
        kids = [c for c in self.d.store.find("candidates") if "Kids" in c["title"]]
        self.assertTrue(kids and all(c["status"] == "rejected" for c in kids))
        self.assertTrue(all(any("Restricted" in r for r in c["reasons"]) for c in kids))

    def test_card_economics_recompute_exactly(self):
        e = self.d.business["economics"]
        for s in self.out["shortlist"]:
            c = self.d.store.get("candidates", s["candidate_id"])
            econ = c["data"]["card"]["economics"]
            again = scoring.unit_economics(econ["price"], econ["product_cost"], econ["shipping"], e["payment_fee_pct"],
                                           e["payment_fee_fixed"], e["return_rate"], e["other_variable_cost"])
            for k in ("contribution", "break_even_cac", "break_even_roas", "margin_pct"):
                self.assertEqual(econ[k], again[k])
            self.assertEqual(s["contribution"], econ["contribution"])

    def test_shortlist_is_ranked_and_capped(self):
        sl = self.out["shortlist"]
        self.assertLessEqual(len(sl), self.d.business["limits"]["shortlist_size"])
        listed = [s for s in sl if s["listed"]]
        self.assertEqual([s["score"] for s in listed], sorted([s["score"] for s in listed], reverse=True))


class CreativeTests(unittest.TestCase):
    def setUp(self):
        self.d, _, _ = prepared()

    def test_concepts_are_distinct_and_product_identity_is_identical(self):
        by = {}
        for b in self.d.store.find("briefs"):
            by.setdefault(b["candidate_id"], []).append(b)
        self.assertTrue(by)
        for cid, briefs in by.items():
            self.assertEqual(len({b["concept"] for b in briefs}), len(briefs))
            self.assertEqual(len({b["data"]["hook"] for b in briefs}), len(briefs))
            idents = {str(sorted(b["data"]["product_identity"].items())) for b in briefs}
            self.assertEqual(len(idents), 1)
            for b in briefs:
                self.assertEqual(b["data"]["aspect"], "9:16")
                self.assertEqual(b["data"]["test_variable"], "concept (angle + hook + scene order)")
        for c in self.d.store.find("creatives"):
            brief = self.d.store.get("briefs", c["brief_id"])
            self.assertEqual(c["spec"]["product_identity"], brief["data"]["product_identity"])

    def test_unlisted_products_get_no_briefs(self):
        cands = self.d.store.find("candidates")
        for c in cands:
            if not (c["data"].get("listing") or {}).get("url"):
                self.assertFalse(self.d.store.find("briefs", candidate_id=c["id"]))

    def test_unsupported_claims_never_reach_a_brief(self):
        from jarvis_marketing import claims
        for b in self.d.store.find("briefs"):
            texts = [b["data"]["hook"], b["data"]["caption"], *b["data"]["vo_lines"], *(s["on_screen_text"] for s in b["data"]["scenes"])]
            claims.check_all(texts, b["data"]["allowed_numbers"])
        for bad in ("Customers love it", "Guaranteed to last", "Only 3 left", "Was £20 now only £9.99", "Rated 5 stars"):
            with self.assertRaises(claims.UnsupportedClaim, msg=bad):
                claims.check(bad, allowed_numbers=[9.99])
        with self.assertRaises(claims.UnsupportedClaim):
            claims.check("Just £8.49 today", allowed_numbers=[12.99])        # a price that isn't the product's


class ProductionQATests(unittest.TestCase):
    def test_no_photos_means_needs_asset_never_a_substitute(self):
        d, _ = make()
        to_mode(d, C.REVIEW)
        d.run_cycle(["research", "evaluate", "brief"])
        brief = d.store.find("briefs")[0]
        data = brief["data"]
        data["product_identity"]["images"] = []
        d.store.update("briefs", brief["id"], data_json=data)
        out = d.run_stage("produce", {"brief_ids": [brief["id"]]})["output"]
        self.assertEqual(out["needs_asset"], [brief["id"]])
        c = d.store.find("creatives", brief_id=brief["id"])[0]
        self.assertEqual(c["status"], "needs_asset")
        self.assertFalse(c["files"])
        self.assertTrue(any("photos" in n["text"] for n in d.store.find("notifications")))

    def test_bad_render_is_rejected_by_qa_with_reasons(self):
        d, _ = make()
        to_mode(d, C.REVIEW)
        d.run_cycle(["research", "evaluate", "brief"])
        d.adapters.video.defect = "landscape"
        d.run_cycle(["produce", "qa"])
        reviews = d.store.find("qa_reviews")
        self.assertTrue(reviews)
        for r in reviews:
            self.assertFalse(r["passed"])
            self.assertEqual(r["scores"]["technical"]["score"], 1)
            self.assertIn("aspect", r["scores"]["technical"]["why"])
            self.assertEqual(r["label"], E.PREDICTION)
        self.assertFalse(d.store.find("creatives", status="qa_passed"))

    def test_qa_independently_catches_a_swapped_product(self):
        d, _, _ = prepared()
        c = d.store.find("creatives", status="qa_passed")[0]
        other = [x for x in d.store.find("candidates") if x["id"] != c["candidate_id"] and x["data"].get("images")][0]
        spec = dict(c["spec"], images=other["data"]["images"][:3])
        d.store.update("creatives", c["id"], spec_json=spec, status="rendered")
        out = d.run_stage("qa", {"creative_ids": [c["id"]]})["output"]
        self.assertFalse(out["reviews"][0]["passed"])
        review = d.store.find("qa_reviews", creative_id=c["id"])[-1]
        self.assertEqual(review["scores"]["product_accuracy"]["score"], 1)

    def test_landing_page_price_change_blocks_the_creative(self):
        d, _, _ = prepared()
        c = d.store.find("creatives", status="qa_passed")[0]
        brief = d.store.get("briefs", c["brief_id"])
        data = dict(brief["data"], price_stated=(brief["data"]["price_stated"] or 0) + 1)
        d.store.update("briefs", brief["id"], data_json=data)
        d.store.update("creatives", c["id"], status="rendered")
        d.run_stage("qa", {"creative_ids": [c["id"]]})
        review = d.store.find("qa_reviews", creative_id=c["id"])[-1]
        self.assertFalse(review["passed"])
        self.assertEqual(review["scores"]["landing_page"]["score"], 1)

    def test_revision_loop_is_capped(self):
        d, _ = make()
        to_mode(d, C.REVIEW)
        d.run_cycle(["research", "evaluate", "brief"])
        d.adapters.video.defect = "long"
        r = d.run_cycle(["produce", "qa"])
        produce_runs = [t for t in r["tasks"] if t["stage"] == "produce"]
        self.assertLessEqual(len(produce_runs), 1 + d.business["testing"]["max_revisions"])
        self.assertFalse(d.store.find("creatives", status="qa_passed"))
        self.assertTrue(all(c["revision"] <= d.business["testing"]["max_revisions"] for c in d.store.find("creatives")))


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.d, self.clock, _ = prepared()
        self.posts = self.d.store.find("posts")
        self.pub = self.d.adapters.publisher

    def test_posts_wait_for_approval_and_nothing_publishes_without_it(self):
        self.assertTrue(self.posts)
        self.assertTrue(all(p["status"] == "pending_approval" and p["approval_id"] for p in self.posts))
        to_mode(self.d, C.TEST)
        out = self.d.publish_approved([p["id"] for p in self.posts])["output"]
        self.assertFalse(out["published"])
        self.assertEqual(self.pub.calls, [])

    def test_approved_post_publishes_once_only(self):
        to_mode(self.d, C.TEST)
        p = self.posts[0]
        self.d.decide_post(DANNY, p["approval_id"], "approve")
        self.assertEqual(self.d.publish_approved([p["id"]])["output"]["published"][0]["post_id"], p["id"])
        self.d.store.update("posts", p["id"], status="approved")            # even if something resets the status...
        again = self.d.publish_approved([p["id"]])["output"]
        self.assertFalse(again["published"])                                  # ...the used approval can't post it twice
        self.assertEqual(self.pub.calls, [p["id"]])

    def test_publish_needs_test_mode(self):
        p = self.posts[0]
        self.d.decide_post(DANNY, p["approval_id"], "approve")
        task = self.d.publish_approved([p["id"]])
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(self.pub.calls, [])

    def test_content_changed_after_approval_is_refused(self):
        to_mode(self.d, C.TEST)
        p = self.posts[0]
        self.d.decide_post(DANNY, p["approval_id"], "approve")
        self.d.store.update("posts", p["id"], caption=p["caption"] + " Buy now!")
        out = self.d.publish_approved([p["id"]])["output"]
        self.assertFalse(out["published"])
        self.assertEqual(self.pub.calls, [])

    def test_rejected_post_never_publishes(self):
        to_mode(self.d, C.TEST)
        p = self.posts[0]
        self.d.decide_post(DANNY, p["approval_id"], "reject")
        self.assertEqual(self.d.store.get("posts", p["id"])["status"], "rejected")
        self.d.publish_approved([p["id"]])
        self.assertEqual(self.pub.calls, [])

    def test_agents_cannot_approve(self):
        with self.assertRaises(C.PermissionDenied):
            self.d.decide_post("testing-agent", self.posts[0]["approval_id"], "approve")

    def test_emergency_stop_blocks_publishing(self):
        to_mode(self.d, C.TEST)
        p = self.posts[0]
        self.d.decide_post(DANNY, p["approval_id"], "approve")
        self.d.controls.emergency_stop("danny", "test")
        self.assertEqual(self.d.publish_approved([p["id"]])["status"], "blocked")
        self.assertEqual(self.pub.calls, [])

    def test_the_same_video_is_never_posted_twice(self):
        to_mode(self.d, C.TEST)
        p = self.posts[0]
        self.d.decide_post(DANNY, p["approval_id"], "approve")
        self.d.publish_approved([p["id"]])
        twin = self.d.store.insert("posts", business_id=self.d.bid, experiment_id=p["experiment_id"], creative_id=p["creative_id"],
                                   platform="tiktok", content_hash="other", caption=p["caption"], link=p["link"], status="approved",
                                   created_at=0, simulated=True)
        out = self.d.publish_approved([twin])["output"]
        self.assertFalse(out["published"])
        self.assertEqual(self.pub.calls, [p["id"]])
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):                       # identical content can't even be stored twice
            self.d.store.insert("posts", business_id=self.d.bid, experiment_id=1, creative_id=1, platform="tiktok",
                                content_hash=p["content_hash"], caption="x", status="draft", created_at=0, simulated=True)

    def test_paid_ads_adapter_always_refuses(self):
        with self.assertRaises(Disabled):
            self.d.adapters.paid.launch({"budget": 10})
        with self.assertRaises(Disabled):
            self.d.adapters.paid.set_budget(1)

    def test_a_failed_publish_is_recorded_and_not_retried_silently(self):
        to_mode(self.d, C.TEST)
        p = self.posts[0]
        self.d.decide_post(DANNY, p["approval_id"], "approve")
        self.pub.fail_next = "TikTok showed a human check"
        out = self.d.publish_approved([p["id"]])["output"]
        self.assertIn("human check", out["refused"][0]["why"])
        self.assertEqual(self.d.store.get("posts", p["id"])["status"], "failed")


class MeasurementTests(unittest.TestCase):
    def published(self, missing=()):
        d, clock, _ = prepared()
        d.adapters.analytics.missing = set(missing)
        to_mode(d, C.TEST)
        for a in d.approvals.pending(d.bid):
            d.decide_post(DANNY, a["id"], "approve")
        d.publish_approved()
        return d, clock

    def test_missing_analytics_are_stored_as_unavailable_never_zero(self):
        d, clock = self.published(missing={"sessions", "link_clicks"})
        clock.advance(2 * DAY)
        d.run_stage("collect")
        rows = d.store.find("metrics", metric="sessions")
        self.assertTrue(rows)
        self.assertTrue(all(r["value"] is None and not r["available"] for r in rows))
        for _ in range(17):
            clock.advance(DAY)
            d.run_cycle(["collect", "judge"])
        for e in d.store.find("experiments"):
            self.assertEqual(e["verdict"], "insufficient_data")
            self.assertIsNone(e["result"]["winner"])

    def test_stale_readings_give_no_verdict(self):
        d, clock = self.published()
        clock.advance(2 * DAY)
        d.run_stage("collect")
        clock.advance(16 * DAY)                    # no new readings since
        d.run_stage("judge")
        for e in d.store.find("experiments"):
            self.assertEqual(e["verdict"], "insufficient_data")
            self.assertIn("stale", " ".join(e["result"]["reasons"]))

    def test_orders_only_attribute_inside_the_window_and_by_utm(self):
        d, clock = self.published()
        for _ in range(4):
            clock.advance(DAY)
            d.run_stage("collect")
        for o in d.store.find("orders"):
            if o["post_id"]:
                p = d.store.get("posts", o["post_id"])
                self.assertEqual(o["attribution"]["utm_content"], f"post-{p['id']}")
                self.assertGreaterEqual(o["created_at"], p["published_at"])
            else:
                self.assertFalse((o["attribution"] or {}).get("utm_content"))
        self.assertEqual(testing.reconcile(d.store, d.bid), [])

    def test_reconcile_catches_a_tampered_record(self):
        d, clock = self.published()
        for _ in range(18):
            clock.advance(DAY)
            d.run_cycle(["collect", "judge"])
        attributed = [o for o in d.store.find("orders") if o["post_id"]]
        if not attributed:
            self.skipTest("the simulation produced no attributed orders for this seed")
        d.store.update("orders", attributed[0]["id"], total=attributed[0]["total"] + 100)
        self.assertTrue(testing.reconcile(d.store, d.bid))


class DirectorSafetyTests(unittest.TestCase):
    def test_agents_hold_no_way_to_change_controls(self):
        import jarvis_marketing.agents as pkg
        folder = os.path.dirname(pkg.__file__)
        for name in os.listdir(folder):
            if name.endswith(".py"):
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    src = f.read()
                for forbidden in ("set_mode", "set_caps", "set_schedule", ".resume(", "HumanPrincipal", "._set(", ".decide(",
                                  "Controls(", "PAID_ADS_ENABLED =", "MAX_MODE ="):
                    self.assertNotIn(forbidden, src, f"{name} uses {forbidden}")
        view = C.AgentControls(C.Controls(temp_store()[0]), 1)
        self.assertEqual({n for n in dir(view) if not n.startswith("_")}, {"mode", "allowed", "stopped"})

    def test_idempotent_task_keys(self):
        d, _ = make()
        self.assertEqual(d.submit("learn", key="k1"), d.submit("learn", key="k1"))

    def _patched(self, fn):
        original = STAGES["learn"]
        STAGES["learn"] = original[:2] + (fn,) + original[3:]
        self.addCleanup(lambda: STAGES.__setitem__("learn", original))

    def test_a_task_that_hangs_times_out(self):
        d, _ = make(limits=dict(business()["limits"], task_timeout_s=0.3))
        release = threading.Event()
        self._patched(lambda ctx, inp: release.wait(5) or {"lessons": [], "report_ids": []})
        t0 = time.time()
        task = d.run_stage("learn")
        release.set()
        self.assertEqual(task["status"], "timed_out")
        self.assertLess(time.time() - t0, 3)

    def test_transient_failure_is_retried_up_to_the_cap(self):
        d, _ = make()
        calls = []

        def flaky(ctx, inp):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("network blip")
            return {"lessons": [], "report_ids": []}
        self._patched(flaky)
        task = d.run_stage("learn")
        self.assertEqual((task["status"], task["attempts"]), ("done", 2))
        self._patched(lambda ctx, inp: (_ for _ in ()).throw(ConnectionError("down")))
        task = d.run_stage("learn")
        self.assertEqual((task["status"], task["attempts"]), ("failed", d.business["limits"]["task_max_attempts"]))

    def test_bad_output_fails_the_task(self):
        d, _ = make()
        self._patched(lambda ctx, inp: {"lessons": "not a list"})
        task = d.run_stage("learn")
        self.assertEqual(task["status"], "failed")
        self.assertIn("schema", task["error"])

    def test_restart_recovery_requeues_tasks_and_never_reposts(self):
        d, clock, _ = prepared()
        to_mode(d, C.TEST)
        p = d.store.find("posts")[0]
        d.decide_post(DANNY, p["approval_id"], "approve")
        d.store.update("posts", p["id"], status="publishing")               # Jarvis died mid-upload
        tid = d.submit("research", {"keywords": ["phone stand"]}, key="was-running")
        d.store.update("tasks", tid, status="running", attempts=1, lease_token="old", lease_until=clock() + 100)
        # a fresh Director over the same database = Jarvis restarted
        d2 = Director(d.business, store=d.store, adapters=d.adapters)
        notes = d2.recover()
        self.assertEqual(d2.store.get("tasks", tid)["status"], "done")
        self.assertEqual(d2.store.get("posts", p["id"])["status"], "check")
        self.assertTrue(any("post" in n for n in notes))
        d2.publish_approved()
        self.assertEqual(d.adapters.publisher.calls, [])

    def test_scheduler_is_off_until_danny_switches_it_on(self):
        d, clock = make()
        s = Scheduler(d)
        self.assertEqual(s.tick(), "idle")
        self.assertFalse(d.store.find("cycles"))
        with self.assertRaises(C.PermissionDenied):
            d.controls.set_schedule("director", d.bid, True)
        d.controls.set_schedule(DANNY, d.bid, True, 24)
        self.assertIn(s.tick(), ("done", "partial"))
        self.assertEqual(s.tick(), "idle")               # not due again for 24h
        clock.advance(25 * 3600)
        self.assertIn(s.tick(), ("done", "partial"))
        d.controls.emergency_stop("danny", "x")
        clock.advance(25 * 3600)
        self.assertEqual(s.tick(), "stopped")


if __name__ == "__main__":
    unittest.main()
