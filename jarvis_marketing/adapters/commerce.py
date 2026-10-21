"""
Store admin and paid-ads adapters.

    ShopifyAdmin      live: drafts through jarvis_store's guarded client (always DRAFT, tagged jarvis),
                      read-back of what Shopify holds, and publish of ONE Jarvis draft after Danny's
                      approval (the controls gate consumes the approval before this is called).
    MockShopAdmin     SIMULATED store admin; a published product appears on the mock store's pages.
    MetaMarketing     live Meta Marketing API: validates a prepared test plan and reads campaign
                      insights. launch()/set_budget() ALWAYS refuse: paid ads are off in code.
    MockMetaMarketing SIMULATED, same refusals.
"""

import hashlib
import json

from . import Adapter, Disabled, PaidAds, Unavailable


class ShopifyAdmin(Adapter):
    name = "shopify_admin"

    def __init__(self, recorder=None, client=None):
        super().__init__(recorder)
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from jarvis_store.shopify import Shopify
            self._client = Shopify()
        return self._client

    def create_draft(self, listing):
        created = self.client.create_draft_product(
            listing["title"], listing["description_html"], listing["product_type"], listing["tags"], listing["options"],
            [{"options": v["options"], "price": f"{v['price']:.2f}", "sku": v["sku"]} for v in listing["variants"]],
            [{"url": u, "alt": listing["title"]} for u in listing["images"]])
        self.record(f"shopify-admin://products/{created['id']}?action=create_draft", created)
        return {"product_ref": created["id"], "handle": created["handle"], "admin_url": created["admin_url"]}

    def active_products(self, first=50):
        """Our live (ACTIVE) products as Shopify holds them: the start of the central product record. Read-only."""
        data = self.client.query("""query($n: Int!) { products(first: $n, query: "status:active") { nodes {
              id title handle status descriptionHtml productType vendor onlineStoreUrl tags
              variants(first: 50) { nodes { sku price barcode selectedOptions { name value } } }
              media(first: 12) { nodes { ... on MediaImage { image { url } } } } } } }""", {"n": first})
        out = []
        for p in data["products"]["nodes"]:
            out.append({"ref": p["id"], "title": p["title"], "handle": p["handle"], "status": p["status"],
                        "description_html": p.get("descriptionHtml") or "", "product_type": p.get("productType"), "vendor": p.get("vendor"),
                        "url": p.get("onlineStoreUrl"), "tags": p.get("tags") or [],
                        "variants": [{"sku": v.get("sku") or "", "price": float(v["price"]), "barcode": v.get("barcode"),
                                      "options": {o["name"]: o["value"] for o in v.get("selectedOptions") or [] if o["name"] != "Title"}}
                                     for v in p["variants"]["nodes"]],
                        "images": [m["image"]["url"] for m in p["media"]["nodes"] if m.get("image")]})
        return out, self.record("shopify-admin://products?status=active", out)

    def snapshot(self, product_ref):
        snap = self.client.product_snapshot(product_ref)
        return snap, self.record(f"shopify-admin://products/{product_ref}", snap or {"error": "not found"}, ok=bool(snap))

    def publish(self, product_ref, approved_by):
        out = self.client.publish_jarvis_draft(product_ref, approved_by)
        self.record(f"shopify-admin://products/{product_ref}?action=publish", out)
        return out


class MockShopAdmin(Adapter):
    name, simulated = "mock_shop_admin", True

    def __init__(self, shop, recorder=None):
        super().__init__(recorder)
        self.shop = shop                  # the MockShop whose public pages show published products
        self.products = {}
        self.fail_publish = None          # tests

    def create_draft(self, listing):
        ref = "gid://mock/Product/" + hashlib.sha256(json.dumps(listing, sort_keys=True, default=str).encode()).hexdigest()[:10]
        handle = "-".join(listing["title"].lower().split())[:60]
        self.products[ref] = {"id": ref, "title": listing["title"], "handle": handle, "status": "DRAFT", "tags": list(listing["tags"]) + ["jarvis"],
                              "description_html": listing["description_html"], "online_store_url": None,
                              "variants": [{"price": f"{v['price']:.2f}", "sku": v["sku"], "title": " / ".join(v["options"].values()) or "Default"}
                                           for v in listing["variants"]],
                              "images": len(listing["images"]), "_listing": listing}
        self.record(f"mock://shop-admin/products/{ref}?action=create_draft", {"id": ref, "handle": handle})
        return {"product_ref": ref, "handle": handle, "admin_url": f"mock://admin/products/{ref}"}

    def active_products(self, first=50):
        """The simulated store's live products (with SIMULATED supplier codes as SKUs)."""
        out = []
        for item in self.shop.products()[0][:first]:
            pid = item["supplier_product_id"]
            p = self.shop.world.product(pid)
            out.append({"ref": f"gid://mock/Product/{item['handle']}", "title": item["title"], "handle": item["handle"], "status": "ACTIVE",
                        "description_html": f"<p>The {p['name']}.</p><ul><li>Material: {p['material']}</li></ul>",
                        "product_type": "Home & Living", "vendor": "Collective", "url": item["url"], "tags": [],
                        "variants": [{"sku": f"{pid}-{c}", "price": item["price_min"], "barcode": None,
                                      "options": {"Colour": c} if len(p["colours"]) > 1 else {}} for c in p["colours"]],
                        "images": p["images"]})
        return out, self.record("mock://shop-admin/products?status=active", out)

    def snapshot(self, product_ref):
        p = self.products.get(product_ref)
        snap = {k: v for k, v in p.items() if not k.startswith("_")} if p else None
        return snap, self.record(f"mock://shop-admin/products/{product_ref}", snap or {"error": "not found"}, ok=bool(snap))

    def publish(self, product_ref, approved_by):
        if self.fail_publish:
            raise RuntimeError(self.fail_publish)
        p = self.products[product_ref]
        if p["status"] != "DRAFT" or "jarvis" not in p["tags"]:
            raise RuntimeError("Refused: only Jarvis's own drafts can be published.")
        p["status"] = "ACTIVE"
        price = min(float(v["price"]) for v in p["variants"])
        self.shop.extra[p["handle"]] = {"handle": p["handle"], "title": p["title"], "price_min": price, "available": True,
                                         "url": f"https://collectivefinds.co.uk/products/{p['handle']}",
                                         "supplier_product_id": p["_listing"]["supplier_product_id"]}
        out = {"id": product_ref, "status": "ACTIVE", "handle": p["handle"], "channel": "online store"}
        self.record(f"mock://shop-admin/products/{product_ref}?action=publish", out)
        return out


# ---------------------------------------------------------------- paid (Meta)
REQUIRED_META = ("ad_account_id", "page_id", "pixel_id")


def check_plan(plan, meta_config):
    """Problems that would stop a prepared Facebook/Instagram test from being launchable (empty = ready
    for Danny's authorisation). Checked in code; launching itself is still disabled."""
    problems = []
    for k in REQUIRED_META:
        if not (meta_config or {}).get(k):
            problems.append(f"Meta {k.replace('_', ' ')} isn't set in businesses.json (meta.{k}).")
    if not plan.get("destination_live"):
        problems.append("The product page isn't published and verified yet.")
    if not plan.get("audiences_approved"):
        problems.append("No approved audience: Danny must approve the targeting.")
    ads = plan.get("ads") or []
    if len(ads) < 2:
        problems.append("A comparison needs at least two creatives.")
    for a in ads:
        if not a.get("video_file"):
            problems.append(f"Creative {a.get('creative_id')}: no rendered video file (SIMULATED renders can't run).")
        if "utm_content=" not in (a.get("link") or ""):
            problems.append(f"Creative {a.get('creative_id')}: link has no UTM, so orders couldn't be attributed.")
    b = plan.get("budget") or {}
    if not b.get("daily_per_ad_set") or not b.get("days"):
        problems.append("No budget proposed.")
    return problems


class MetaMarketing(PaidAds):
    name = "meta_marketing"

    def __init__(self, meta_config=None, recorder=None, token=None, transport=None, version="v23.0"):
        super().__init__(recorder)
        self.cfg, self._token, self.transport, self.version = meta_config or {}, token, transport, version

    def _get_token(self):
        if self._token is None:
            try:
                from jarvis_store import creds
                self._token = creds.load().get("meta_ads_token") or ""
            except Exception:
                self._token = ""
        if not self._token:
            raise Unavailable("No Meta Marketing API token is stored (needed only to read results of a paid test).")
        return self._token

    def validate(self, plan):
        return check_plan(plan, self.cfg)

    def insights(self, campaign_id):
        """Read-only numbers for one campaign. Anything Meta doesn't report stays None."""
        params = {"fields": "spend,impressions,reach,clicks,inline_link_clicks,actions,action_values",
                  "level": "ad", "access_token": self._get_token()}
        if self.transport:
            body = self.transport(f"{campaign_id}/insights", params)
        else:
            import requests
            r = requests.get(f"https://graph.facebook.com/{self.version}/{campaign_id}/insights", params=params, timeout=30)
            body = r.json()
            if r.status_code != 200:
                raise RuntimeError(f"Meta insights refused: {(body.get('error') or {}).get('message')}")
        out = []
        for row in body.get("data") or []:
            acts = {a["action_type"]: float(a["value"]) for a in row.get("actions") or []}
            vals = {a["action_type"]: float(a["value"]) for a in row.get("action_values") or []}
            out.append({"ad_id": row.get("ad_id"), "spend": float(row["spend"]) if row.get("spend") is not None else None,
                        "impressions": int(row["impressions"]) if row.get("impressions") is not None else None,
                        "link_clicks": int(row["inline_link_clicks"]) if row.get("inline_link_clicks") is not None else None,
                        "purchases": acts.get("offsite_conversion.fb_pixel_purchase", acts.get("purchase")),
                        "purchase_value": vals.get("offsite_conversion.fb_pixel_purchase", vals.get("purchase"))})
        return out, self.record(f"https://graph.facebook.com/{self.version}/{campaign_id}/insights", out)


class MockMetaMarketing(PaidAds):
    name, simulated = "mock_meta_marketing", True

    def __init__(self, meta_config=None, recorder=None):
        super().__init__(recorder)
        self.cfg = meta_config or {"ad_account_id": "act_SIM", "page_id": "SIM", "pixel_id": "SIM"}

    def validate(self, plan):
        return [p for p in check_plan(plan, self.cfg) if "SIMULATED renders" not in p]

    def insights(self, campaign_id):
        raise Unavailable("No simulated paid campaigns exist: paid launching is disabled.")


def assert_disabled(adapter):
    """Used by tests: every spending call refuses."""
    for call in (adapter.launch, adapter.set_budget):
        try:
            call({})
        except Disabled:
            continue
        raise AssertionError(f"{call} did not refuse")
