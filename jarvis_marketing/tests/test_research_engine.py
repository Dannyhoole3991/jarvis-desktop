"""The winning-ad research engine: research sources, advert reverse-engineering, verified Shopify listings,
original creatives informed by research, pre-launch ranking, prepared (never launched) Facebook/Instagram
tests, and the full workflow end to end on SIMULATED adapters."""

import datetime as dt
import json
import os
import tempfile
import unittest

from jarvis_marketing import api, controls as C, evidence as E, sources
from jarvis_marketing.adapters import Adapter, Disabled, Unavailable
from jarvis_marketing.adapters.adlibrary import ManualAdImports, MetaAdLibrary, normal
from jarvis_marketing.adapters.commerce import MetaMarketing, assert_disabled, check_plan
from jarvis_marketing.agents import adintel, assessment, listing
from jarvis_marketing.tests.test_agents import DANNY, make, to_mode

TODAY = dt.date(2026, 10, 20)


def unlisted(d):
    """Products not yet in the (simulated) store that passed evaluation."""
    return [c for c in d.store.find("candidates") if not c["data"].get("listing") and c["status"] in ("shortlisted", "reserve")]


def listed_and_live(d):
    """Research, evaluate, list the unlisted products, approve and publish them: returns the director."""
    to_mode(d, C.REVIEW)
    d.run_cycle(["research", "ads", "evaluate"])
    ids = [c["id"] for c in unlisted(d)]
    d.run_stage("list", {"candidate_ids": ids})
    for a in d.approvals.pending(d.bid):
        if a["action"] == "publish_product":
            d.decide(DANNY, a["id"], "approve")
    d.publish_listing()
    return ids


class FailingAds(Adapter):
    name = "broken_ads"

    def ads(self, keyword, limit=25):
        raise RuntimeError("HTTP 500 from the source")


class SourceTests(unittest.TestCase):
    def test_a_failing_source_is_recorded_and_research_carries_on(self):
        d, _ = make()
        d.adapters.ad_library = FailingAds()
        d.run_stage("research", {"keywords": ["cable organizer"]})
        t = d.run_stage("ads", {"keywords": ["cable organizer"]})
        self.assertEqual(t["status"], "done", t["error"])
        out = t["output"]["keywords"][0]
        self.assertIn("meta_ad_library", out["sources_failed"])
        runs = d.store.find("source_runs", source="meta_ad_library")
        self.assertTrue(runs and not runs[-1]["ok"] and "HTTP 500" in runs[-1]["error"])
        cov = {c["source"]: c for c in sources.coverage(d.store, d.bid, d.adapters)}
        self.assertEqual(cov["meta_ad_library"]["state"], "failing")
        self.assertEqual(cov["tiktok_commercial_content"]["state"], "unavailable")
        self.assertTrue(all(c["access"] and c["limits"] for c in cov.values()))
        self.assertTrue(d.store.find("source_runs", source="supplier") is not None)

    def test_every_source_has_an_adapter_slot_and_limits_written_down(self):
        for name, spec in sources.REGISTRY.items():
            self.assertIn(spec["kind"], ("ads", "supplier", "competitors", "demand", "social", "reviews", "own", "own_sales", "google"), name)
            self.assertTrue(spec["access"] and spec["limits"] and spec["adapter"], name)


class AnalysisTests(unittest.TestCase):
    def test_text_only_ad_reports_visuals_as_not_observable(self):
        ad = normal(external_id="1", source="meta_ad_library", bodies=["Tired of tangled cables? No more mess. Free UK delivery. Shop now"],
                    start="2026-08-01", reach=52000, cta=None)
        a = adintel.analyse(ad, TODAY)
        self.assertEqual(a["hook"], "Tired of tangled cables?")
        self.assertEqual(a["hook_type"], "problem")
        self.assertEqual(a["concept"], "problem_solution")
        for k in ("first_shot", "scenes_pacing", "product_reveal", "text_overlays", "captions", "audio"):
            self.assertFalse(a["elements"][k]["observed"], k)
            self.assertIsNone(a["elements"][k]["value"], k)
        self.assertIn("free uk delivery", [x.lower() for x in a["elements"]["offer"]["value"]])
        self.assertEqual(a["elements"]["call_to_action"]["value"].lower(), "shop now")
        self.assertEqual(a["performance"]["run_days"], 80)
        self.assertEqual(a["performance"]["run_days_label"], E.ESTIMATE)
        self.assertTrue(a["performance"]["long_running"])
        self.assertTrue(any("No sales" in l for l in a["limitations"]))
        self.assertTrue(any("Reach" in l for l in a["limitations"]))
        self.assertEqual(a["elements"]["hook"]["label_why"], E.HYPOTHESIS)
        self.assertEqual(a["elements"]["customer_and_angle"]["label"], E.HYPOTHESIS)

    def test_measured_video_gives_pacing(self):
        ad = normal(external_id="2", source="manual:tiktok", bodies=["Watch this."],
                    video={"duration_s": 12.0, "cuts": [1.2, 2.5, 4.0, 6.0, 9.0], "has_audio": True})
        a = adintel.analyse(ad, TODAY)
        self.assertTrue(a["elements"]["scenes_pacing"]["observed"])
        self.assertEqual(a["elements"]["scenes_pacing"]["value"]["cuts"], 5)
        self.assertEqual(a["elements"]["first_seconds"]["value"]["first_shot_s"], 1.2)
        self.assertEqual(a["hook_type"], "demo")
        self.assertFalse(a["elements"]["product_reveal"]["observed"])     # still needs a person

    def test_real_outcome_led_ads_are_read_as_benefit_hooks(self):
        # wording from two real adverts in Meta's public Ad Library (20 Oct 2026)
        a = adintel.analyse(normal(external_id="r1", source="manual:meta_ad_library_web", bodies=[
            "Bring your best self to every workday and boost your productivity with our thoughtfully designed range of work accessories. "
            "Highlights include: Desk Mat - featuring a document hideaway, magnetic cable holder and stationery toolbar."]), TODAY)
        self.assertEqual(a["hook_type"], "benefit")
        self.assertTrue(a["elements"]["benefits"]["value"])
        b = adintel.analyse(normal(external_id="r2", source="manual:meta_ad_library_web", bodies=[
            "Your hair tools finally get somewhere proper to live — one pocket each, a deep compartment for the cable, all zipped away. "
            "Takes the stress out of packing for a trip, since nothing's loose and nothing gets forgotten."]), TODAY)
        self.assertEqual(b["hook_type"], "benefit")
        self.assertGreaterEqual(len(b["elements"]["benefits"]["value"]), 3)
        self.assertIn("relief", b["elements"]["emotional_triggers"]["value"])

    def test_social_proof_is_never_turned_into_our_concept(self):
        a = adintel.analyse(normal(external_id="3", source="x", bodies=["Everyone keeps asking where I got this."]), TODAY)
        self.assertEqual(a["hook_type"], "social_proof")
        self.assertIsNone(a["concept"])

    def test_principles_weight_long_running_ads_and_stay_hypotheses(self):
        d, _ = make()
        d.run_stage("research", {"keywords": ["cable organizer"]})
        d.run_stage("ads", {"keywords": ["cable organizer"]})
        ps = adintel.principles(d.store, d.bid, "cable organizer")
        self.assertTrue(ps)
        self.assertTrue(all(p["label"] == E.HYPOTHESIS for p in ps))
        hooks = [p for p in ps if p["hook_type"]]
        self.assertAlmostEqual(sum(p["weight"] for p in hooks), 1.0, places=1)
        for p in hooks:
            self.assertLessEqual(p["long_running_ads"], p["ads"])
        ads = d.store.find("ads", keyword="cable organizer")
        self.assertTrue(all(a["simulated"] for a in ads))
        ev = d.store.find("evidence", subject_type="ad")
        self.assertTrue(ev and all(e["label"] == E.FACT and e["fetch_id"] and e["simulated"] for e in ev))

    def test_too_close_catches_copied_wording(self):
        rivals = ["Tired of cable chaos? This fixes it in seconds. Free UK delivery."]
        self.assertTrue(adintel.too_close("Tired of cable chaos? This fixes it in seconds.", rivals))
        self.assertTrue(adintel.too_close("Honestly, cable chaos? This fixes it in seconds, trust me", rivals))
        self.assertFalse(adintel.too_close("What do you think this is?", rivals))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "ad_imports.json")
        self.imp = ManualAdImports(self.path)

    def test_imports_need_a_traceable_link_and_real_files(self):
        with self.assertRaises(ValueError):
            self.imp.add({"url": "not a link", "hook": "x"}, DANNY)
        with self.assertRaises(ValueError):
            self.imp.add({"url": "https://ads.tiktok.com/x", "hook": ""}, DANNY)
        with self.assertRaises(ValueError):
            self.imp.add({"url": "https://ads.tiktok.com/x", "hook": "x", "video_path": "C:/nope/ad.mp4"}, DANNY)
        with self.assertRaises(Unavailable):
            self.imp.ads("cable organizer")

    def test_imported_ad_comes_back_with_its_url_and_signals(self):
        self.imp.add({"source": "tiktok_creative_center", "url": "https://ads.tiktok.com/business/creativecenter/x", "keyword": "cable organizer",
                      "hook": "Stop losing your chargers.", "text": "Keeps every cable in one place.", "observed_signals": {"as_shown": "CTR top 10%"}},
                     DANNY)
        ads, _ = self.imp.ads("cable organizer")
        self.assertEqual(ads[0]["url"], "https://ads.tiktok.com/business/creativecenter/x")
        self.assertEqual(ads[0]["source"], "manual:tiktok_creative_center")
        a = adintel.analyse(ads[0], TODAY)
        self.assertEqual(a["hook"], "Stop losing your chargers.")
        self.assertTrue(any("platform's own signals" in l for l in a["limitations"]))


class MetaAdLibraryTests(unittest.TestCase):
    def test_parses_the_archive_and_never_records_the_token(self):
        recorded = []
        seen = {}

        def transport(params):
            seen.update(params)
            return {"data": [{"id": "123", "page_name": "Tidy Co", "ad_creative_bodies": ["No more tangled cables."],
                              "ad_creative_link_titles": ["Cable box"], "ad_delivery_start_time": "2026-07-01T00:00:00+0000",
                              "ad_snapshot_url": "https://www.facebook.com/ads/archive/render_ad/?id=123", "eu_total_reach": 41000,
                              "publisher_platforms": ["facebook", "instagram"]}]}
        lib = MetaAdLibrary(lambda *a: recorded.append(a) or 1, token="SECRET-TOKEN", transport=transport)
        ads, fid = lib.ads("cable organizer")
        self.assertEqual(seen["ad_type"], "ALL")
        self.assertEqual(json.loads(seen["ad_reached_countries"]), ["IE"])
        self.assertEqual(ads[0]["advertiser"], "Tidy Co")
        self.assertEqual(ads[0]["start"], "2026-07-01")
        self.assertEqual(ads[0]["reach"], 41000)
        self.assertNotIn("SECRET-TOKEN", json.dumps(recorded))

    def test_without_a_token_it_says_so(self):
        lib = MetaAdLibrary(token="")
        with self.assertRaises(Unavailable):
            lib.ads("cable organizer")


class ListingTests(unittest.TestCase):
    def test_draft_is_verified_then_waits_for_approval_and_goes_live_only_after_it(self):
        d, _ = make()
        to_mode(d, C.REVIEW)
        d.run_cycle(["research", "evaluate"])
        ids = [c["id"] for c in unlisted(d)]
        self.assertTrue(ids)
        out = d.run_stage("list", {"candidate_ids": ids})["output"]
        rows = d.store.find("listings")
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(r["status"], "awaiting_approval", r["error"])
            self.assertTrue(all(v["ok"] or v.get("unknown") for v in r["checks"].values()), r["checks"])
            self.assertEqual(d.adapters.shop_admin.products[r["product_ref"]]["status"], "DRAFT")
        self.assertEqual(len(out["listings"]), len(rows))
        # nothing goes live without the approval
        res = d.publish_listing()["output"]
        self.assertEqual(res["live"], [])
        self.assertTrue(all(p["status"] == "DRAFT" for p in d.adapters.shop_admin.products.values()))
        first = rows[0]
        d.decide(DANNY, first["approval_id"], "approve")
        res = d.publish_listing([first["id"]])["output"]
        self.assertEqual(res["live"][0]["status"], "live_verified")
        cand = d.store.get("candidates", first["candidate_id"])
        self.assertTrue(cand["data"]["listing"]["url"])
        ev = [e for e in E.for_subject(d.store, "candidate", f"{cand['supplier']}:{cand['supplier_product_id']}") if e["field"] == "store_listing"]
        self.assertTrue(ev and ev[-1]["label"] == E.FACT)
        # the approval was used up: publishing again can't reuse it
        self.assertIsNotNone(d.store.get("approvals", first["approval_id"])["used_at"])

    def test_a_draft_changed_after_approval_is_refused(self):
        d, _ = make()
        to_mode(d, C.REVIEW)
        d.run_cycle(["research", "evaluate"])
        d.run_stage("list", {"candidate_ids": [c["id"] for c in unlisted(d)]})
        row = d.store.find("listings")[0]
        d.decide(DANNY, row["approval_id"], "approve")
        d.adapters.shop_admin.products[row["product_ref"]]["variants"][0]["price"] = "1.00"      # someone edits the draft
        res = d.publish_listing([row["id"]])["output"]
        self.assertEqual(res["live"], [])
        self.assertIn("changed after approval", res["refused"][0]["why"])
        self.assertEqual(d.adapters.shop_admin.products[row["product_ref"]]["status"], "DRAFT")

    def test_profit_after_ad_cost_rejects_with_reasons(self):
        d, _ = make()
        d.business["economics"] = dict(d.business["economics"], planned_cpa=50.0)
        to_mode(d, C.REVIEW)
        d.run_cycle(["research", "evaluate"])
        cands = d.store.find("candidates")
        self.assertTrue(all(c["status"] == "rejected" for c in cands))
        self.assertTrue(any("planned ad cost" in r for c in cands for r in (c["reasons"] or [])))

    def test_draft_compare_spots_differences(self):
        plan = {"title": "Box", "variants": [{"sku": "A", "price": 9.99}], "images": ["1", "2"], "description_html": "<p>Hi</p>"}
        snap = {"status": "DRAFT", "title": "Box", "variants": [{"sku": "A", "price": "9.99"}], "images": 2, "tags": ["jarvis"],
                "description_html": "<p>Hi</p>"}
        self.assertEqual(listing.compare(plan, snap), [])
        self.assertTrue(listing.compare(plan, dict(snap, status="ACTIVE")))
        self.assertTrue(listing.compare(plan, dict(snap, variants=[{"sku": "A", "price": "8.99"}])))
        self.assertTrue(listing.compare(plan, dict(snap, tags=[])))
        self.assertEqual(listing.compare(plan, None), ["Shopify has no such product."])

    def test_interrupted_publish_is_flagged_never_retried(self):
        d, _ = make()
        to_mode(d, C.REVIEW)
        d.run_cycle(["research", "evaluate"])
        d.run_stage("list", {"candidate_ids": [c["id"] for c in unlisted(d)]})
        row = d.store.find("listings")[0]
        d.store.update("listings", row["id"], status="publishing")
        notes = d.recover()
        self.assertEqual(d.store.get("listings", row["id"])["status"], "check")
        self.assertTrue(any("manual check" in n for n in notes))


class ShopifyPublishGuardTests(unittest.TestCase):
    def transport(self, product):
        calls = []

        def t(method, url, headers=None, data=None, json_body=None, timeout=30):
            calls.append(json_body)
            q = (json_body or {}).get("query", "")
            if "product(id" in q:
                return 200, {"data": {"product": product}}
            if "productUpdate" in q:
                return 200, {"data": {"productUpdate": {"product": {"id": "gid://1", "status": "ACTIVE", "handle": "box"}, "userErrors": []}}}
            if "publications" in q:
                return 200, {"data": {"publications": {"nodes": [{"id": "gid://pub/1", "name": "Online Store"}]}}}
            if "publishablePublish" in q:
                return 200, {"data": {"publishablePublish": {"userErrors": []}}}
            return 200, {"data": {}}
        return t, calls

    def client(self, product):
        from jarvis_store import config as SC
        from jarvis_store.shopify import Shopify
        SC.AUDIT_LOG = os.path.join(tempfile.mkdtemp(), "audit.jsonl")
        SC.STORE_DATA = os.path.dirname(SC.AUDIT_LOG)
        t, calls = self.transport(product)
        return Shopify(transport=t, credentials={"shopify_token": "x"}), calls

    def test_only_jarvis_drafts_can_be_published(self):
        from jarvis_store.shopify import GuardError
        for product in ({"id": "gid://1", "status": "ACTIVE", "tags": ["jarvis"]}, {"id": "gid://1", "status": "DRAFT", "tags": []}):
            shop, calls = self.client(product)
            with self.assertRaises(GuardError):
                shop.publish_jarvis_draft("gid://1", "Danny")
            self.assertFalse(any("productUpdate" in (c or {}).get("query", "") for c in calls))
        shop, calls = self.client({"id": "gid://1", "status": "DRAFT", "tags": ["jarvis"]})
        out = shop.publish_jarvis_draft("gid://1", "Danny")
        self.assertEqual(out["status"], "ACTIVE")
        self.assertEqual(out["channel"], "online store")

    def test_the_guarded_writes_still_refuse_publishing(self):
        from jarvis_store.shopify import GuardError
        shop, _ = self.client({"id": "gid://1", "status": "DRAFT", "tags": ["jarvis"]})
        with self.assertRaises(GuardError):
            shop._mutate('mutation { productUpdate(input: {id: "gid://1", status: ACTIVE}) { product { id } } }',
                         {"input": {"id": "gid://1", "status": "ACTIVE"}}, product_id="gid://1")


class CreativeResearchTests(unittest.TestCase):
    def test_briefs_record_the_research_and_never_copy_ads(self):
        d, _ = make()
        to_mode(d, C.REVIEW)
        d.run_cycle(["research", "ads", "evaluate", "brief"])
        briefs = d.store.find("briefs")
        self.assertTrue(briefs)
        for b in briefs:
            self.assertIn("adverts analysed", b["data"]["research_note"])
            rivals = adintel.ad_texts(d.store, d.bid, d.store.get("candidates", b["candidate_id"])["data"]["keyword"])
            for line in [b["data"]["hook"], b["data"]["caption"], *b["data"]["vo_lines"]]:
                if len(line.split()) >= 4:
                    self.assertFalse(adintel.too_close(line, rivals), line)
            self.assertIn("instagram_reels", b["data"]["paid_placements"])
        informed = [b for b in briefs if b["data"]["informed_by"]]
        self.assertTrue(informed)


class AssessmentAndPaidTests(unittest.TestCase):
    def setUp(self):
        self.d, _ = make()
        to_mode(self.d, C.REVIEW)
        self.r = self.d.run_cycle(["research", "ads", "evaluate", "brief", "produce", "qa", "rank", "paid_plan"])

    def test_ranking_is_a_labelled_prediction_with_the_missing_predictor_stated(self):
        rows = self.d.store.find("assessments")
        self.assertTrue(rows)
        for a in rows:
            data = a["data"]
            self.assertEqual(a["label"], E.PREDICTION)
            self.assertFalse(data["prediction_service"]["available"])
            totals = [r["total"] for r in data["ranking"]]
            self.assertEqual(totals, sorted(totals, reverse=True))
            for r in data["ranking"]:
                self.assertEqual(set(r["scores"]), set(assessment.CRITERIA))
                self.assertNotIn("ctr", json.dumps(r).lower())               # no invented forecasts
            self.assertEqual(data["test_first"], [r["creative_id"] for r in data["ranking"]][:3])

    def test_paid_tests_are_prepared_but_launching_is_refused_and_audited(self):
        exps = self.d.store.find("experiments", kind="paid_meta")
        self.assertTrue(exps)
        e = exps[0]
        self.assertEqual(e["status"], "prepared")
        plan = e["data"]
        self.assertEqual(plan["status_on_create"], "PAUSED")
        self.assertIn("disabled in code", plan["launch"])
        self.assertTrue(all("utm_content=creative-" in a["link"] for a in plan["ads"]))
        self.assertIn("Danny must approve the targeting", " ".join(plan["problems"]))
        with self.assertRaises(C.PermissionDenied):
            self.d.launch_paid(DANNY, e["id"])
        self.d.store.update("experiments", e["id"], data_json=dict(plan, problems=[]))      # even a "ready" plan can't launch
        with self.assertRaises(C.PermissionDenied):
            self.d.launch_paid(DANNY, e["id"])
        refused = [a for a in self.d.store.find("audit", action="launch_paid") if a["outcome"] == "refused"]
        self.assertTrue(refused)
        with self.assertRaises(C.PermissionDenied):
            self.d.launch_paid("agent", e["id"])
        assert_disabled(self.d.adapters.paid)
        assert_disabled(MetaMarketing({}))

    def test_a_plan_is_not_ready_until_the_page_is_live_and_tracking_set(self):
        problems = check_plan({"destination_live": False, "audiences_approved": False, "ads": [{"creative_id": 1, "link": "x"}],
                               "budget": {}}, {})
        text = " ".join(problems)
        for bit in ("pixel id", "isn't published", "approved audience", "at least two", "no rendered video", "UTM", "No budget"):
            self.assertIn(bit, text)

    def test_caps_cannot_be_raised_for_paid_spend(self):
        with self.assertRaises(C.PermissionDenied):
            self.d.controls.set_caps(DANNY, self.d.bid, 50, 20)


class ApiResearchTests(unittest.TestCase):
    def test_overview_shows_sources_ads_listings_and_the_listing_approval_route_works(self):
        d, _ = make()
        dept = api.Department(directors={d.business["slug"]: d})
        slug = d.business["slug"]
        to_mode(d, C.REVIEW)
        d.run_cycle(["research", "ads", "evaluate"])
        d.run_stage("list", {"candidate_ids": [c["id"] for c in unlisted(d)]})
        v = api.overview(dept, slug)
        self.assertEqual(len(v["ads"]), min(80, len(d.store.find("ads"))))
        self.assertEqual(len(v["listings"]), len(d.store.find("listings")))
        self.assertTrue(any(s["source"] == "meta_ad_library" for s in v["sources"]))
        self.assertTrue(v["principles"])
        json.dumps(v, default=str)
        a = next(x for x in v["approvals_pending"] if x["listing"])
        with self.assertRaises(PermissionError):
            api.handle_post(dept, f"/api/marketing/{slug}/approvals/{a['id']}/approve", {}, principal=None)
        r = api.handle_post(dept, f"/api/marketing/{slug}/approvals/{a['id']}/approve", {}, principal=DANNY)
        self.assertIn("publishing it now", r["text"])
        job = dept.background[slug]
        for _ in range(200):
            if job["status"] != "running":
                break
            import time
            time.sleep(0.05)
        self.assertEqual(d.store.one("listings", approval_id=a["id"])["status"], "live_verified")
        with self.assertRaises(PermissionError):
            api.handle_post(dept, f"/api/marketing/{slug}/ads/import", {"url": "https://x.example/ad", "hook": "hi"}, principal=None)
        e = d.run_stage("paid_plan")
        self.assertEqual(e["status"], "done")


class EndToEndTests(unittest.TestCase):
    def test_the_whole_workflow_in_simulation(self):
        """Research -> adverts -> principles -> supplier check + profit -> draft -> approval -> live page -> original
        creatives -> ranking -> organic test + prepared paid test (launch refused) -> results -> lessons."""
        d, clock = make()
        ids = listed_and_live(d)
        self.assertTrue(d.store.find("ad_analyses"))                                   # 2-3 adverts found and explained
        live = d.store.find("listings", status="live_verified")                         # 4-5 verified, approved, published
        self.assertTrue(live)
        d.run_cycle(["brief", "produce", "qa", "rank", "design", "paid_plan"])          # 6-7 original creatives, ranked
        cand_ids = {l["candidate_id"] for l in live}
        self.assertTrue(any(b["candidate_id"] in cand_ids for b in d.store.find("briefs")))
        paid = d.store.find("experiments", kind="paid_meta")                             # 9 prepared
        self.assertTrue(paid)
        with self.assertRaises(C.PermissionDenied):                                     # 10 never launched
            d.launch_paid(DANNY, paid[0]["id"])
        to_mode(d, C.TEST)
        for a in d.approvals.pending(d.bid):
            if a["action"] == "publish_organic":
                d.decide(DANNY, a["id"], "approve")
        d.publish_approved()
        for _ in range(18):                                                             # 11 results
            clock.advance(86400)
            d.run_cycle(["collect", "judge", "learn"])
        self.assertTrue(d.store.find("lessons"))                                       # 12 fed back
        self.assertTrue(all(l["simulated"] and l["label"] != E.VERIFIED for l in d.store.find("lessons")))
        self.assertEqual(d.controls.spent(d.bid), 0.0)
        self.assertTrue(ids)


if __name__ == "__main__":
    unittest.main()
