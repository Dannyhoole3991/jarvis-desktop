"""LIVE adapters with stand-in transports (the real ones need Danny's PC): CJ, competitor and store pages,
Shopify orders (API and browser), TikTok Studio numbers, the TikTok poster, and ffprobe on real renders."""

import os
import shutil
import subprocess
import tempfile
import unittest

from jarvis_marketing import evidence as E
from jarvis_marketing.adapters import Adapter, Unavailable, live
from jarvis_marketing.adapters.video import probe
from jarvis_marketing.agents import qa
from jarvis_marketing.director import Director
from jarvis_marketing.tests.helpers import Clock, business, temp_store

IMAGES = [f"https://cdn.example/p1/{i}.jpg" for i in range(5)]


def cj_transport(calls):
    def transport(method, url, headers=None, params=None, json_body=None, timeout=30):
        calls.append(url)
        if url.endswith("authentication/getAccessToken"):
            return 200, {"code": 200, "result": True, "data": {"accessToken": "tok"}}
        if url.endswith("product/listV2"):
            return 200, {"code": 200, "result": True, "data": {"content": [{"productList": [
                {"id": "P1", "nameEn": "Cable Organizer Box", "sku": "CJAB1234567", "nowPrice": "3.20", "bigImage": IMAGES[0], "listedNum": 1500}]}]}}
        if url.endswith("product/query"):
            return 200, {"code": 200, "result": True, "data": {
                "pid": "P1", "productNameEn": "Cable Organizer Box", "productSku": "CJAB1234567", "description": "Material: ABS",
                "productKeyEn": "Color", "productImageSet": IMAGES, "categoryName": "Home", "materialNameEn": "ABS",
                "variants": [{"vid": "V1", "variantSku": "CJAB1234567-Black", "variantNameEn": "Black", "variantKey": "Black",
                              "variantSellPrice": 3.2, "variantWeight": 100, "variantImage": IMAGES[0]}]}}
        if url.endswith("logistic/freightCalculate"):
            return 200, {"code": 200, "result": True, "data": [{"logisticName": "CJPacket", "logisticPrice": 2.5, "totalPostageFee": 2.5,
                                                                 "logisticAging": "5-9"}]}
        return 404, {}
    return transport


def shop_fetch(url):
    product = {"title": "Cable Organizer Box", "handle": "cable-organizer-box", "tags": ["cj:CJAB1234567"],
               "variants": [{"price": "12.99", "available": True, "sku": "CJAB1234567-Black"}]}
    if url.endswith("/products.json?limit=250"):
        return {"products": [product]}
    if url.endswith("/products/cable-organizer-box.json"):
        return {"product": product}
    raise RuntimeError("HTTP 404")


def rival_fetch(url):
    return {"products": [{"title": "Cable Organizer Set", "handle": "set", "variants": [{"price": "14.99"}]},
                         {"title": "Desk Lamp", "handle": "lamp", "variants": [{"price": "30.00"}]},
                         {"title": "Cable Organiser Pro", "handle": "pro", "variants": [{"price": "11.50"}, {"price": "13.00"}]}]}


class FakeVideo(Adapter):
    name = "fake_local_video"

    def render(self, spec, out_dir):
        raise Unavailable("not rendering in this test")


class LiveResearchTests(unittest.TestCase):
    def setUp(self):
        from jarvis_store.cj import CJ
        self.calls = []
        store, _ = temp_store(Clock())
        b = business(profile="live", competitor_stores=["rival.example"])
        self.d = Director(b, store=store, overrides={"video": FakeVideo()})
        rec = self.d.adapters.supplier._recorder
        self.d.adapters.supplier = live.CJSupplier(rec, client=CJ(transport=cj_transport(self.calls), api_key="k",
                                                                  sleep=lambda s: None, token_cache=None))
        self.d.adapters.shop = live.ShopifyPublicShop("collectivefinds.co.uk", rec, fetch=shop_fetch)
        self.d.adapters.competitors = live.ShopifyPublicCompetitors(["rival.example"], rec, fetch=rival_fetch)
        self.d.adapters.simulated = False

    def test_live_research_records_real_facts_and_gaps(self):
        out = self.d.run_stage("research", {"keywords": ["cable organizer"]})
        self.assertEqual(out["status"], "done", out["error"])
        opp = out["output"]["opportunities"][0]
        self.assertTrue(any("Google Trends" in g for g in opp["gaps"]))
        self.assertTrue(any("Research API" in g for g in opp["gaps"]))
        facts = self.d.store.find("evidence", label="fact")
        self.assertTrue(facts)
        self.assertFalse(any(f["simulated"] for f in facts))
        fields = {f["field"] for f in facts}
        self.assertTrue({"supplier_listed", "supplier_price_usd", "uk_shipping", "images", "store_listing", "competitor_price"} <= fields)
        comp = sorted(f["value"] for f in facts if f["field"] == "competitor_price")
        self.assertEqual(comp, [11.5, 14.99])               # only matching titles; the lamp is ignored
        cand = self.d.store.find("candidates")[0]
        self.assertEqual(cand["data"]["listing"]["handle"], "cable-organizer-box")
        self.assertIn("gaps", cand["data"])
        self.assertTrue(any("stock" in g for g in cand["data"]["gaps"]))        # CJ doesn't report stock here: a gap

    def test_live_evaluation_uses_the_store_price_and_labels_estimates(self):
        self.d.run_stage("research", {"keywords": ["cable organizer"]})
        out = self.d.run_stage("evaluate")["output"]
        cand = self.d.store.find("candidates")[0]
        econ = cand["data"]["card"]["economics"]
        self.assertEqual(econ["price"], 12.99)
        self.assertIn("live store price", econ["price_source"])
        self.assertEqual(econ["product_cost"], round(3.2 * 0.75, 2))
        est = E.estimates(self.d.store, "candidate", "cj:P1")
        self.assertIn("product_cost", est)
        self.assertNotIn("product_cost", E.facts(self.d.store, "candidate", "cj:P1"))
        self.assertTrue(out["shortlist"] or out["rejected"])

    def test_no_competitor_stores_is_a_gap_not_a_guess(self):
        self.d.adapters.competitors = live.ShopifyPublicCompetitors([], None, fetch=rival_fetch)
        opp = self.d.run_stage("research", {"keywords": ["cable organizer"]})["output"]["opportunities"][0]
        self.assertTrue(any("competitor" in g for g in opp["gaps"]))


class OrdersTests(unittest.TestCase):
    def test_api_orders_carry_utm_attribution(self):
        class Client:
            def query(self, q, variables):
                assert "mutation" not in q
                return {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [
                    {"name": "#1001", "createdAt": "2026-10-20T10:00:00Z",
                     "currentTotalPriceSet": {"shopMoney": {"amount": "12.99", "currencyCode": "GBP"}},
                     "lineItems": {"nodes": [{"title": "Cable Organizer Box", "quantity": 1, "sku": "CJAB1234567-Black"}]},
                     "customerJourneySummary": {"firstVisit": {"source": "tiktok", "landingPage": "/products/x", "referrerUrl": None,
                                                               "utmParameters": {"source": "tiktok", "medium": "organic",
                                                                                 "campaign": "exp-1", "content": "post-3"}}}},
                    {"name": "#1002", "createdAt": "2026-10-20T11:00:00Z",
                     "currentTotalPriceSet": {"shopMoney": {"amount": "9.49", "currencyCode": "GBP"}},
                     "lineItems": {"nodes": []}, "customerJourneySummary": None}]}}
        orders, _ = live.ShopifyApiOrders(None, client=Client()).orders(0, 2e9)
        self.assertEqual(orders[0]["attribution"]["utm_content"], "post-3")
        self.assertIsNone(orders[1]["attribution"])                 # unknown stays unknown

    def test_browser_orders_parse_dates_and_never_invent_attribution(self):
        now = 1_792_500_000.0     # a fixed moment
        reader = lambda: {"ok": True, "orders": [{"number": "#1003", "date": "Today at 3:12 pm", "total": "£12.99", "items": "1 item"},
                                                  {"number": "#1004", "date": "sometime", "total": "£5.00"}]}
        orders, _ = live.BrowserOrders(None, reader=reader).orders(0, now)
        self.assertEqual([o["order_ref"] for o in orders], ["#1003"])      # an unreadable date is skipped, not guessed
        self.assertIsNone(orders[0]["attribution"])
        with self.assertRaises(RuntimeError):
            live.BrowserOrders(None, reader=lambda: {"ok": False, "problem": "logged out"}).orders(0, now)

    def test_admin_dates(self):
        import datetime as dt
        now = dt.datetime(2026, 10, 20, 12, 0).timestamp()
        self.assertEqual(dt.datetime.fromtimestamp(live.parse_admin_date("Yesterday at 9:05 am", now)), dt.datetime(2026, 10, 19, 9, 5))
        self.assertEqual(dt.datetime.fromtimestamp(live.parse_admin_date("Oct 18 at 4:05 pm", now)), dt.datetime(2026, 10, 18, 16, 5))
        self.assertEqual(dt.datetime.fromtimestamp(live.parse_admin_date("18 Dec at 1:00 am", now)), dt.datetime(2025, 12, 18, 1, 0))
        self.assertIsNone(live.parse_admin_date("", now))


class TikTokTests(unittest.TestCase):
    POST = {"id": 3, "caption": "Tired of this? Cables everywhere.\n#homefinds", "content_hash": "abc123def4567890", "title": "Box"}

    def test_studio_numbers_only_what_tiktok_shows(self):
        data = {"tiktok": {"ok": True, "checked_at": 100.0, "videos": [
            {"caption": "Tired of this? Cables everywhere. #homefinds", "views": 1200, "likes": 40, "comments": 3}]}}
        out, _ = live.TikTokStudioAnalytics(None, loader=lambda: data).post_metrics(self.POST, 200.0)
        self.assertEqual((out["views"], out["likes"], out["comments"]), (1200, 40, 3))
        for m in ("shares", "sessions", "link_clicks", "avg_watch_seconds", "completion_rate"):
            self.assertIsNone(out[m], m)
        with self.assertRaises(Unavailable):
            live.TikTokStudioAnalytics(None, loader=lambda: {"tiktok": {"ok": False, "problem": "logged out"}}).post_metrics(self.POST, 0)
        with self.assertRaises(Unavailable):
            live.TikTokStudioAnalytics(None, loader=lambda: {"tiktok": {"ok": True, "videos": []}}).post_metrics(self.POST, 0)

    def test_browser_poster_reuses_the_existing_queue(self):
        root = tempfile.mkdtemp()
        src = os.path.join(tempfile.mkdtemp(), "v.mp4")
        with open(src, "wb") as f:
            f.write(b"video")
        added = []
        add = lambda video, caption, title, source="", ai_generated=False: added.append((video, caption, ai_generated)) or {"id": 9}
        pub = live.TikTokBrowserPublisher(None, add=add, post=lambda i: {"id": i, "status": "posted"}, video_root=root)
        out = pub.publish(self.POST, src)
        self.assertEqual(out, {"external_ref": "tiktok-queue-9", "status": "posted"})
        self.assertTrue(added[0][0].startswith(os.path.join(root, "marketing")))
        self.assertEqual(added[0][1], self.POST["caption"])
        self.assertTrue(added[0][2])                                         # AI label on
        fail = live.TikTokBrowserPublisher(None, add=add, post=lambda i: {"status": "failed", "error": "human check"}, video_root=root)
        with self.assertRaises(RuntimeError):
            fail.publish(self.POST, src)
        with self.assertRaises(RuntimeError):
            pub.publish(self.POST, None)                                     # no file, no post


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class ProbeTests(unittest.TestCase):
    def clip(self, w, h, audio=True, seconds=2):
        path = os.path.join(tempfile.mkdtemp(), "c.mp4")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=orange:s={w}x{h}:d={seconds}"]
        if audio:
            cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
        cmd += ["-c:v", "libx264", "-b:v", "3M", "-pix_fmt", "yuv420p", path]
        subprocess.run(cmd, check=True, timeout=120)
        return path

    def test_a_good_vertical_render_passes(self):
        p = probe(self.clip(1080, 1920))
        checks = qa.technical(p, {"duration_s": 2.0})
        self.assertTrue(checks["aspect_9_16"]["ok"])
        self.assertTrue(checks["audio"]["ok"])
        self.assertTrue(checks["duration"]["ok"])

    def test_landscape_and_silent_renders_fail(self):
        p = probe(self.clip(1280, 720, audio=False))
        checks = qa.technical(p, {"duration_s": 2.0})
        self.assertFalse(checks["aspect_9_16"]["ok"])
        self.assertFalse(checks["audio"]["ok"])


if __name__ == "__main__":
    unittest.main()
