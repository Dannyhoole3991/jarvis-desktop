"""
Offline tests for jarvis_store (fake Shopify + fake CJ; nothing real is touched).

    py -3 -m unittest jarvis_store.test_store -v
"""

import json
import os
import tempfile
import unittest

from jarvis_store import config, copywriter, pipeline, pricing
from jarvis_store.cj import CJ
from jarvis_store.shopify import GuardError, Shopify, _mutation_names


class FakeShopify:
    """Records every request; answers like the Admin API would."""

    def __init__(self, existing_status="DRAFT", existing_tags=("jarvis",)):
        self.calls = []
        self.existing_status = existing_status
        self.existing_tags = list(existing_tags)

    def __call__(self, method, url, headers=None, data=None, json_body=None, timeout=30):
        self.calls.append((url, json_body))
        if url.endswith("/oauth/access_token"):
            return 200, {"access_token": "tok", "expires_in": 86399}
        q = json_body["query"]
        if "product(id:" in q:
            return 200, {"data": {"product": {"id": json_body["variables"]["id"], "status": self.existing_status,
                                              "tags": self.existing_tags}}}
        if "productCreate" in q:
            status = json_body["variables"]["product"]["status"]
            return 200, {"data": {"productCreate": {"product": {"id": "gid://shopify/Product/1", "handle": "x",
                                                                 "status": status,
                                                                 "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/9"}]}},
                                                     "userErrors": []}}}
        if "productVariantsBulkCreate" in q:
            return 200, {"data": {"productVariantsBulkCreate": {"productVariants": [], "userErrors": []}}}
        if "productVariantsBulkUpdate" in q:
            return 200, {"data": {"productVariantsBulkUpdate": {"productVariants": [], "userErrors": []}}}
        if "productVariants(first" in q:
            return 200, {"data": {"productVariants": {"nodes": [{"sku": "CJJT1538272-09IR"}]}}}
        if "products(first" in q:
            return 200, {"data": {"products": {"nodes": [{"id": "gid://shopify/Product/5", "title": "Self-Cleaning Hair Brush",
                                                           "handle": "b", "status": "ACTIVE", "productType": "", "tags": [],
                                                           "totalInventory": 1, "priceRangeV2": {}}]}}}
        return 200, {"data": {}}

    def mutations_sent(self):
        return [b["query"] for u, b in self.calls if b and "mutation" in b.get("query", "")]


CREDS = {"shopify_client_id": "id", "shopify_client_secret": "secret", "shopify_token": "", "cj_api_key": "k"}


class FakeCJ:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, headers=None, params=None, json_body=None, timeout=30):
        self.calls.append(url)
        if url.endswith("getAccessToken"):
            return 200, {"code": 200, "result": True, "data": {"accessToken": "cjtok"}}
        if "listV2" in url:
            return 200, {"code": 200, "result": True, "data": {"content": [{"productList": [
                {"id": "P1", "nameEn": "Baby Toy Rattle", "sku": "CJAA0000001", "sellPrice": "2.00", "listedNum": 500},
                {"id": "P2", "nameEn": "New Hot Silicone Cable Organizer Clips 2024", "sku": "CJBB0000002",
                 "sellPrice": "1.50", "listedNum": 300}]}]}}
        if "product/query" in url:
            return 200, {"code": 200, "result": True, "data": {
                "pid": params["pid"], "productNameEn": "New Hot Silicone Cable Organizer Clips 2024", "productSku": "CJBB0000002",
                "description": "<p>Material: Silicone</p><p>Size: 4 x 2 cm</p><p>Packing list: Clips x5</p>",
                "productKeyEn": "Color", "productImageSet": [f"https://img/{i}.jpg" for i in range(6)],
                "variants": [{"vid": "V1", "variantSku": "CJBB000000201AZ", "variantKey": "Black", "variantSellPrice": 1.5,
                              "variantWeight": 30},
                             {"vid": "V2", "variantSku": "CJBB000000202BY", "variantKey": "White", "variantSellPrice": 1.5,
                              "variantWeight": 30}]}}
        if "freightCalculate" in url:
            return 200, {"code": 200, "result": True, "data": [
                {"logisticName": "Slow Post", "logisticPrice": 2.0, "logisticAging": "15-40"},
                {"logisticName": "YunExpress", "logisticPrice": 4.0, "logisticAging": "5-9"}]}
        return 404, {}


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeShopify()
        self.shop = Shopify(transport=self.fake, credentials=CREDS)

    def test_delete_and_publish_are_refused(self):
        for q in ["mutation { productDelete(input: {id: \"1\"}) { deletedProductId } }",
                  "mutation { publishablePublish(id: \"1\", input: []) { userErrors { message } } }",
                  "mutation { themeDelete(id: \"1\") { deletedThemeId } }",
                  "mutation { menuUpdate(id: \"1\", title: \"x\", items: []) { menu { id } } }"]:
            with self.assertRaises(GuardError):
                self.shop._mutate(q, {})
        self.assertEqual(self.fake.calls, [])

    def test_query_method_refuses_mutations(self):
        with self.assertRaises(GuardError):
            self.shop.query("mutation { productCreate(product: {}) { product { id } } }")

    def test_cannot_set_active(self):
        with self.assertRaises(GuardError):
            self.shop._mutate("mutation($p: ProductCreateInput!) { productCreate(product: $p) { product { id } } }",
                              {"p": {"title": "x", "status": "ACTIVE"}})

    def test_cannot_edit_live_or_dannys_products(self):
        for status, tags in [("ACTIVE", ["jarvis"]), ("DRAFT", [])]:
            shop = Shopify(transport=FakeShopify(status, tags), credentials=CREDS)
            with self.assertRaises(GuardError):
                shop._mutate("mutation($pid: ID!) { productUpdate(product: {id: $pid}) { product { id } } }",
                             {"pid": "gid://shopify/Product/5"}, product_id="gid://shopify/Product/5")

    def test_create_is_always_draft_and_tagged(self):
        out = self.shop.create_draft_product("Clips", "<p>x</p>", "Home & Living", ["cj:CJBB0000002"],
                                             [{"name": "Colour", "values": ["Black", "White"]}],
                                             [{"options": {"Colour": "Black"}, "price": "6.99", "sku": "A"},
                                              {"options": {"Colour": "White"}, "price": "6.99", "sku": "B"}],
                                             [{"url": "https://img/1.jpg"}])
        create = next(b for u, b in self.fake.calls if b and "productCreate" in b.get("query", ""))
        product = create["variables"]["product"]
        self.assertEqual(product["status"], "DRAFT")
        self.assertIn("jarvis", product["tags"])
        self.assertIn("needs-review", product["tags"])
        self.assertTrue(out["admin_url"].endswith("/products/1"))
        for q in self.fake.mutations_sent():
            self.assertTrue(set(_mutation_names(q)) <= {"productCreate", "productVariantsBulkCreate",
                                                         "productVariantsBulkUpdate", "productUpdate"})

    def test_mutation_name_parser(self):
        self.assertEqual(_mutation_names("mutation($a: ID!) { productCreate(x: $a) { product { id } } }"), ["productCreate"])
        self.assertEqual(_mutation_names("mutation { a: productDelete(id: 1) { id } }"), ["productDelete"])


class PricingAndCopyTests(unittest.TestCase):
    def test_profit_target_met(self):
        for usd, ship in [(1.5, 4.0), (6.9, 5.51), (12.0, 13.67)]:
            for kind in ("everyday", "christmas"):
                p = pricing.price_for(usd, ship, kind)
                self.assertGreaterEqual(p["profit_gbp"], config.PROFIT_GBP[kind] - 0.01)
                self.assertIn(round(p["price_gbp"] % 1, 2), (0.49, 0.99))

    def test_banned_claims(self):
        for bad in ["Relieves pain fast", "Was £20 now £10", "5-star reviews", "Hurry, only 3 left", "Best seller"]:
            with self.assertRaises(ValueError):
                copywriter.check_claims(bad)
        copywriter.check_claims("Soft silicone clips in black or white.")

    def test_christmas_kind_allows_christmas_but_keeps_safety_list(self):
        self.assertTrue(pipeline._deny("everyday").search("Christmas Tree Topper Star"))
        self.assertIsNone(pipeline._deny("christmas").search("Christmas Tree Topper Star"))
        for bad in ["Christmas Kids Toy Train", "Christmas Lights EU Plug", "Halloween Pumpkin"]:
            self.assertTrue(pipeline._deny("christmas").search(bad), bad)

    def test_title_uses_only_source_words(self):
        title = copywriter.clean_title("New Hot Silicone Cable Organizer Clips 2024 Home Decoration")
        self.assertEqual(title, "Silicone Cable Organizer Clips")


class PipelineTests(unittest.TestCase):
    def test_end_to_end_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = config.DRAFTS_DIR, config.STORE_DATA, config.AUDIT_LOG
            config.DRAFTS_DIR, config.STORE_DATA = tmp, tmp
            config.AUDIT_LOG = os.path.join(tmp, "audit.jsonl")
            pipeline._download = lambda url, path: open(path, "wb").write(b"x")
            # add_product queues a "review this draft" job in Jarvis's REAL business database; a test must not.
            import sys
            import types
            real_jbiz = sys.modules.get("jarvis_business")
            sys.modules["jarvis_business"] = types.SimpleNamespace(queue_human_action=lambda *a, **k: None)
            try:
                fake_shop = FakeShopify()
                shop = Shopify(transport=fake_shop, credentials=CREDS)
                cj = CJ(transport=FakeCJ(), api_key="k", sleep=lambda s: None, token_cache=None)
                result = pipeline.add_product("cable organizer", cj=cj, shop=shop, video=False, log=lambda m: None)
                self.assertEqual(result["title"], "Silicone Cable Organizer Clips")
                self.assertEqual(result["shipping_days"], 9)          # slow 15-40 day post was rejected
                self.assertTrue(os.path.exists(os.path.join(result["folder"], "REVIEW.md")))
                create = next(b for u, b in fake_shop.calls if b and "productCreate" in b.get("query", ""))
                self.assertEqual(create["variables"]["product"]["status"], "DRAFT")
                self.assertNotIn("Baby", create["variables"]["product"]["title"])
                desc = create["variables"]["product"]["descriptionHtml"]
                self.assertIn("Silicone", desc)
                self.assertIn("5-9 working days", desc)
                log = [json.loads(l) for l in open(config.AUDIT_LOG, encoding="utf-8")]
                self.assertEqual(log[0]["action"], "productCreate")
            finally:
                config.DRAFTS_DIR, config.STORE_DATA, config.AUDIT_LOG = old
                if real_jbiz is not None:
                    sys.modules["jarvis_business"] = real_jbiz
                else:
                    sys.modules.pop("jarvis_business", None)


if __name__ == "__main__":
    unittest.main()
