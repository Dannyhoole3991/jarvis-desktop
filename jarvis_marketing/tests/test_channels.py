"""Multi-platform listings: platform fees and profit, readiness (READY / NEEDS_FIXES / REJECTED), safe fixes, the Google
feed, stock/price/duplicate/order safeguards, permissions and failure recovery."""

import copy
import json
import unittest

from jarvis_marketing import api, controls as C, platforms as P, readiness as R, sync
from jarvis_marketing.tests.test_agents import DANNY, make, to_mode

ECON = {"payment_fee_pct": 0.02, "payment_fee_fixed": 0.25, "return_rate": 0.05, "other_variable_cost": 0.0, "usd_to_business": 0.75,
        "planned_cpa": 3.0}
TH = {"min_contribution": 4.0, "min_stock": 1, "min_profit_after_ads": 1.0}
NOW = 1_800_000_000.0


def product(**over):
    """A complete, verified central record (a live Shopify product linked to CJ)."""
    data = {"title": "Magnetic Cable Organiser Box - Free UK Delivery!!", "description_html": "<p>" + "Keeps every cable tidy in one place. " * 4 + "</p>",
            "description_text": "", "specs": {}, "images": [f"https://cdn.example/{i}.jpg" for i in range(14)], "videos": [],
            "handle": "cable-box", "url": "https://collectivefinds.co.uk/products/cable-box", "shopify_ref": "gid://1", "shopify_status": "ACTIVE",
            "product_type": "Home", "vendor": "Collective", "tags": [],
            "supplier": {"name": "cj", "pid": "P1", "spu": "CJAB1234567", "verified_at": NOW - 3600, "material": "ABS",
                         "shipping": {"name": "CJPacket", "usd": 3.0, "min_days": 4, "max_days": 9}, "variants": []},
            "variants": [{"sku": "CJAB1234567-Black", "price": 14.99, "options": {"Colour": "Black"}, "barcode": None,
                          "supplier_vid": "V1", "supplier_cost_usd": 2.5, "stock": 900},
                         {"sku": "CJAB1234567-White", "price": 14.99, "options": {"Colour": "White"}, "barcode": None,
                          "supplier_vid": "V2", "supplier_cost_usd": 2.5, "stock": 40}],
            "identifiers": {"gtin": None, "mpn": None, "brand": None, "brand_note": "generic", "identifier_exists": False},
            "compliance": {"restricted_terms": [], "electrical": False, "claims_ok": True, "claims_note": "ok", "notes": []},
            "approved_claims": ["Material: ABS"], "demand": {"supplier_listed": 1500, "ads_analysed": 2, "own_orders": None}, "evidence": {}}
    data.update(over)
    return {"id": 1, "title": data["title"], "status": "live", "simulated": False, "data": data}


class FeeTests(unittest.TestCase):
    def test_platform_fee_maths(self):
        fee, _ = P.fee_for("ebay_uk", 14.99)             # (12.9% + 0.35%) x 14.99 + 0.40, plus 20% VAT
        self.assertAlmostEqual(fee, round((14.99 * 0.129 + 0.40 + 14.99 * 0.0035) * 1.2, 2), places=2)
        fee_low, _ = P.fee_for("ebay_uk", 9.99)          # 30p per-order fee at or under £10
        self.assertAlmostEqual(fee_low, round((9.99 * 0.129 + 0.30 + 9.99 * 0.0035) * 1.2, 2), places=2)
        a_low, _ = P.fee_for("amazon_uk", 18.0)          # 8% up to £20, +2% DST on the fee, +VAT
        self.assertAlmostEqual(a_low, round(18.0 * 0.08 * 1.02 * 1.2, 2), places=2)
        a_high, _ = P.fee_for("amazon_uk", 25.0)         # 15% above £20
        self.assertAlmostEqual(a_high, round(25.0 * 0.15 * 1.02 * 1.2, 2), places=2)
        a_min, _ = P.fee_for("amazon_uk", 2.0)           # 25p minimum
        self.assertAlmostEqual(a_min, round(0.25 * 1.02 * 1.2, 2), places=2)
        self.assertEqual(P.fee_for("google", 14.99)[0], 0.0)

    def test_channel_contribution_is_after_that_platforms_fees(self):
        shop = P.channel_economics("shopify", 14.99, 1.88, 2.25, ECON)
        ebay = P.channel_economics("ebay_uk", 14.99, 1.88, 2.25, ECON)
        self.assertEqual(shop["contribution"], round(14.99 - 1.88 - 2.25 - (14.99 * 0.02 + 0.25) - 14.99 * 0.05, 2))
        self.assertEqual(ebay["payment_fees"], 0.0)                       # eBay's fee covers payment processing
        self.assertLess(ebay["contribution"], shop["contribution"])
        self.assertEqual(ebay["label"], "estimate")
        amazon = P.channel_economics("amazon_uk", 14.99, 1.88, 2.25, ECON)
        self.assertIsNotNone(amazon["orders_to_cover_monthly_fee"])


class ReadinessTests(unittest.TestCase):
    def test_google_fixes_are_safe_and_recorded_and_brand_is_escalated(self):
        p = product()
        r = R.assess(p, "google", ECON, TH, NOW)
        pay = r["payload"]
        self.assertNotIn("Free UK Delivery", pay["title"])
        self.assertLessEqual(len(pay["title"]), 150)
        self.assertNotIn("<", pay["description"])
        self.assertEqual(len(pay["images"]), 11)
        self.assertIs(pay["identifier_exists"], False)
        self.assertEqual(pay["availability"], "in_stock")
        rules = " ".join(f["rule"] for f in r["fixes"])
        for bit in ("promotional", "plain text", "at most 11 images", "identifier_exists"):
            self.assertIn(bit, rules)
        # identity unchanged: same SKUs, prices, and only the product's own photos
        self.assertEqual([v["sku"] for v in pay["variants"]], [v["sku"] for v in p["data"]["variants"]])
        self.assertEqual([v["price"] for v in pay["variants"]], [14.99, 14.99])
        self.assertTrue(set(pay["images"]) <= set(p["data"]["images"]))
        self.assertEqual(r["readiness"], R.NEEDS_FIXES)                    # brand is a human decision
        self.assertEqual([e["check"] for e in r["escalations"]], ["brand"])

    def test_one_failed_mandatory_check_blocks_ready_whatever_the_score(self):
        p = product(identifiers={"gtin": None, "mpn": None, "brand": "Collective", "brand_note": "Danny's own brand", "identifier_exists": False})
        r = R.assess(p, "google", ECON, TH, NOW)
        self.assertEqual(r["readiness"], R.READY, r["escalations"])
        stale = copy.deepcopy(p)
        stale["data"]["supplier"]["verified_at"] = NOW - 10 * 86400
        r2 = R.assess(stale, "google", ECON, TH, NOW)
        self.assertGreater(r2["score"], 80)
        self.assertEqual(r2["readiness"], R.NEEDS_FIXES)

    def test_ebay_title_is_cut_at_a_word_boundary(self):
        p = product(title="Magnetic Cable Organiser Box with Lid for Desk Office Home Charging Station Cord Management Tidy")
        r = R.assess(p, "ebay_uk", ECON, TH, NOW)
        t = r["payload"]["title"]
        self.assertLessEqual(len(t), 80)
        self.assertTrue(p["data"]["title"].startswith(t))
        self.assertFalse(t.endswith(" "))

    def test_platforms_that_forbid_the_model_or_lose_money_are_rejected(self):
        self.assertEqual(R.assess(product(), "etsy", ECON, TH, NOW)["readiness"], R.REJECTED)
        cheap = product(variants=[dict(product()["data"]["variants"][0], price=3.0)])
        r = R.assess(cheap, "shopify", ECON, TH, NOW)
        self.assertEqual(r["readiness"], R.REJECTED)
        self.assertTrue(any(c["key"] == "profit" and c["blocking"] for c in r["checks"]))

    def test_marketplaces_need_sync_identifiers_and_confirmed_policy(self):
        r = R.assess(product(), "amazon_uk", ECON, TH, NOW)
        self.assertEqual(r["readiness"], R.NEEDS_FIXES)
        failed = {c["key"] for c in r["checks"] if not c["ok"]}
        self.assertTrue({"eligibility", "identifiers", "sync", "orders"} <= failed)
        ok, reasons = R.recommend("amazon_uk", r, TH)
        self.assertFalse(ok)
        t = R.assess(product(), "tiktok_shop_uk", ECON, TH, NOW)
        self.assertIn("delivery", {c["key"] for c in t["checks"] if not c["ok"]})       # 4-9 days vs ~6 expected

    def test_electrical_items_need_safety_docs_on_marketplaces_only(self):
        p = product(compliance={"restricted_terms": [], "electrical": True, "claims_ok": True, "claims_note": "ok", "notes": []})
        self.assertIn("safety_docs", {c["key"] for c in R.assess(p, "ebay_uk", ECON, TH, NOW)["checks"]})
        self.assertNotIn("safety_docs", {c["key"] for c in R.assess(p, "google", ECON, TH, NOW)["checks"]})

    def test_supplier_codes_are_read_from_real_sku_shapes(self):
        from jarvis_marketing.catalog import spu_from_sku
        for sku, spu in (("CJJT153827211KP", "CJJT1538272"), ("CJJZSNSN00770-Ash 2pcs", "CJJZSNSN00770"),
                         ("CJJJJTJT03010-Pink-B", "CJJJJTJT03010"), ("CJBJPFMB02068-White-USB", "CJBJPFMB02068"),
                         ("CJJSBGBG00728-3 clips2PCS", "CJJSBGBG00728"), ("MOCK-cable-organizer-1-Black", "MOCK-cable-organizer-1"), ("", None)):
            self.assertEqual(spu_from_sku(sku), spu, sku)

    def test_compliance_reads_negations_and_platform_specific_terms(self):
        from jarvis_marketing import catalog
        c = catalog._compliance("Press & Chop Food Chopper", "<p>Chops food in seconds. No batteries needed, no electricity.</p>")
        self.assertFalse(c["electrical"])
        self.assertEqual(c["restricted_terms"], [])
        self.assertIn("food", c["marketplace_terms"])
        self.assertTrue(catalog._compliance("Rechargeable LED Light", "<p>USB charging.</p>")["electrical"])
        p = product(compliance=c)
        self.assertTrue(next(x for x in R.assess(p, "google", ECON, TH, NOW)["checks"] if x["key"] == "policy")["ok"])
        self.assertFalse(next(x for x in R.assess(p, "ebay_uk", ECON, TH, NOW)["checks"] if x["key"] == "policy")["ok"])

    def test_missing_live_page_and_demand_are_escalated_not_invented(self):
        p = product(url=None, demand={})
        r = R.assess(p, "meta_shops", ECON, TH, NOW)
        failed = {c["key"] for c in r["checks"] if not c["ok"]}
        self.assertTrue({"data", "demand"} <= failed)
        self.assertIsNone(r["payload"]["link"])


class GoogleFeedTests(unittest.TestCase):
    def test_feed_rows_follow_the_spec(self):
        p = product(identifiers={"gtin": None, "mpn": None, "brand": "Collective", "brand_note": "", "identifier_exists": False})
        r = R.assess(p, "google", ECON, TH, NOW)
        rows = R.google_feed_rows(p, r["payload"])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["price"], "14.99 GBP")
        self.assertEqual(rows[0]["identifier_exists"], "no")
        self.assertEqual(rows[0]["item_group_id"], "CJAB1234567")
        self.assertEqual(rows[0]["color"], "Black")
        self.assertEqual(R.validate_feed_rows(rows), [])
        bad = dict(rows[0], price="14.99", title="Box SALE!!", link="http://x")
        problems = " ".join(p for _, p in R.validate_feed_rows([bad]))
        for bit in ("price", "promotional", "link"):
            self.assertIn(bit, problems)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.d, _ = make()
        self.s, self.bid = self.d.store, self.d.bid

    def add(self, key, data, status="live"):
        return self.s.insert("catalog", business_id=self.bid, key=key, source="shopify_live", title=data["title"], status=status, data_json=data,
                             verified_at=data["supplier"]["verified_at"], created_at=NOW, updated_at=NOW, simulated=False)

    def test_health_finds_oversell_duplicates_mapping_and_loss_without_changing_anything(self):
        base = product()["data"]
        base["supplier"]["verified_at"] = self.s.clock()
        a = copy.deepcopy(base)
        a["variants"][1]["stock"] = 0
        b = copy.deepcopy(base)
        b["title"] = "Same box again"
        b["variants"][0]["supplier_vid"] = None
        b["variants"][0]["price"] = 3.0
        self.add("shopify:a", a)
        self.add("shopify:b", b)
        before = json.dumps(self.s.find("catalog"), default=str)
        h = sync.health(self.s, self.bid, ECON, self.s.clock())
        kinds = {p["kind"] for p in h["problems"]}
        self.assertTrue({"oversell_risk", "duplicate", "duplicate_sku", "sku_mapping", "price_below_cost"} <= kinds, kinds)
        self.assertFalse(h["ok"])
        self.assertEqual(json.dumps(self.s.find("catalog"), default=str), before)      # report only, no changes

    def test_orders_are_held_unless_mapped_in_stock_and_connected(self):
        data = product()["data"]
        data["variants"][1]["stock"] = 0
        self.add("shopify:a", data)
        fwd = sync.route_order(self.s, self.bid, {"lines": [{"sku": "CJAB1234567-Black", "qty": 1}]}, "shopify")
        self.assertEqual(fwd["decision"], "forward")
        self.assertIn("never places supplier orders", fwd["action"])
        for order, platform in (({"lines": [{"sku": "CJAB1234567-White", "qty": 1}]}, "shopify"),
                                ({"lines": [{"sku": "NOPE", "qty": 1}]}, "shopify"),
                                ({"lines": [{"sku": "CJAB1234567-Black", "qty": 1}]}, "ebay_uk")):
            self.assertEqual(sync.route_order(self.s, self.bid, order, platform)["decision"], "hold", (order, platform))


class PermissionTests(unittest.TestCase):
    def test_channel_publishing_is_off_even_with_an_approval(self):
        d, _ = make()
        to_mode(d, C.REVIEW)
        content = {"platform": "google", "product": 1}
        aid = d.approvals.request(d.bid, "publish_channel", "channel_listing", 1, content, "test", "test")
        d.approvals.decide(DANNY, aid, "approve")
        with self.assertRaises(C.PermissionDenied):
            d.controls.require(d.bid, "publish_channel", "channels-agent", approval_id=aid, content=content)
        self.assertTrue(d.controls.summary(d.bid)["hard_off"]["channel_publishing"])


class ChannelsStageTests(unittest.TestCase):
    def test_catalogue_and_readiness_end_to_end_without_publishing(self):
        d, _ = make()
        to_mode(d, C.REVIEW)
        d.run_cycle(["research", "evaluate"])
        t = d.run_stage("channels")
        self.assertEqual(t["status"], "done", t["error"])
        products = d.store.find("catalog")
        self.assertTrue(products)
        rows = d.store.find("channel_listings", limit=10000)
        self.assertEqual(len(rows), len(products) * len(P.PLATFORMS))
        self.assertTrue(all(r["stage"] in ("assessed", "live", "draft") for r in rows))            # nothing published
        self.assertTrue(all(r["readiness"] == R.REJECTED for r in rows if r["platform"] == "etsy"))
        self.assertTrue(all(r["simulated"] for r in rows))
        self.assertTrue(any(r["readiness"] == R.READY for r in rows if r["platform"] == "shopify"))
        for r in rows:
            if r["readiness"] == R.READY:
                self.assertTrue(all(c["ok"] for c in r["checks"] if c["mandatory"]))
        g = [r for r in rows if r["platform"] == "google"][0]
        self.assertIsNone(g["statuses"]["approval"]["value"])                                    # unknown, never invented
        self.assertIn("isn't connected", g["statuses"]["approval"]["note"])
        d.run_stage("channels")                                                                  # idempotent
        self.assertEqual(len(d.store.find("channel_listings", limit=10000)), len(rows))
        self.assertTrue(d.store.find("reports", kind="sync"))
        v = api.overview(api.Department(directors={d.business["slug"]: d}), d.business["slug"])
        self.assertEqual(len(v["channels"]["products"]), len(products))
        self.assertEqual(len(v["channels"]["platforms"]), len(P.PLATFORMS))
        json.dumps(v, default=str)

    def test_a_failing_store_read_is_reported_and_drafts_still_assessed(self):
        d, _ = make()

        def boom(*a, **k):
            raise RuntimeError("Shopify 503")
        d.adapters.shop_admin.active_products = boom
        t = d.run_stage("channels")
        self.assertEqual(t["status"], "done", t["error"])
        self.assertTrue(any("Shopify 503" in n for n in t["output"]["notes"]))


if __name__ == "__main__":
    unittest.main()
