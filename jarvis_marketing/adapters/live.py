"""
LIVE adapters, built on what Jarvis already has. They need Danny's PC (his stored credentials,
Jarvis's logged-in browser), so in the cloud they are only tested with stand-in transports.

Every reading is recorded as a fetch. A failed or unavailable reading raises; it is never
replaced with an estimate.
"""

import datetime as dt
import os
import re
import shutil

from . import Adapter, Unavailable
from .mock import Unsupported
from .video import LocalSlideshow

UA = {"User-Agent": "Jarvis/marketing (+collectivefinds.co.uk)"}


def _get_json(url, timeout=20):
    import requests
    r = requests.get(url, headers=UA, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"{url} answered HTTP {r.status_code}")
    return r.json()


class CJSupplier(Adapter):
    """CJdropshipping's official API through jarvis_store.cj (read-only, 1 request a second)."""
    name = "cj"

    def __init__(self, recorder=None, client=None):
        super().__init__(recorder)
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from jarvis_store.cj import CJ
            self._client = CJ()
        return self._client

    def search(self, keyword):
        from jarvis_store import config as SC
        items = self.client.search(keyword, size=20)
        return items, self.record(f"{SC.CJ_API}/product/listV2?keyWord={keyword}", items)

    def details(self, pid):
        from jarvis_store import config as SC
        d = self.client.details(pid)
        d = dict(d, stock=None)          # CJ's product query doesn't give stock here: unknown, not assumed
        return d, self.record(f"{SC.CJ_API}/product/query?pid={pid}", d)

    def shipping(self, vid):
        from jarvis_store import config as SC
        opts = self.client.uk_shipping(vid)
        return opts, self.record(f"{SC.CJ_API}/logistic/freightCalculate?vid={vid}&to=GB", opts)

    def details_by_spu(self, spu):
        from jarvis_store import config as SC
        d = dict(self.client.details_by_spu(spu), stock=None)
        return d, self.record(f"{SC.CJ_API}/product/query?productSku={spu}", d)

    def stock(self, vid):
        from jarvis_store import config as SC
        d = {"vid": vid, "stock": self.client.stock(vid)}
        return d, self.record(f"{SC.CJ_API}/product/stock/queryByVid?vid={vid}", d)

    def listing_plan(self, pid):
        """jarvis_store's own product check (photos, UK delivery, price caps, honest copy, real-cost pricing),
        reused as the listing. Raises with the reason if the product fails it."""
        from jarvis_store import config as SC, pipeline
        p = pipeline.evaluate(self.client, pid)
        plan = {"supplier_product_id": pid, "spu": p["cj"]["spu"], "title": p["title"], "product_type": "Home & Living",
                "description_html": p["description_html"], "tags": [f"cj:{p['cj']['spu']}"], "options": p["options"],
                "variants": [{"options": v["options"], "price": v["price_gbp"], "sku": v["sku"], "vid": v["vid"], "cost_usd": v["price_usd"]}
                             for v in p["variants"]],
                "images": p["images"], "shipping": p["shipping"], "facts": {}}
        return plan, self.record(f"{SC.CJ_API}/product/query?pid={pid}#listing-plan", plan)


def _spelling(text):
    """US/UK spellings and plurals compare equal ("organizers" ~ "organiser")."""
    return re.sub(r"s\b", "", text.replace("iz", "is").replace("yz", "ys"))


class ShopifyPublicCompetitors(Adapter):
    """Prices from competitor Shopify stores' public /products.json (only stores Danny lists in config)."""
    name = "shopify_public_competitors"

    def __init__(self, stores, recorder=None, fetch=_get_json):
        super().__init__(recorder)
        self.stores, self.fetch = [s.strip().rstrip("/") for s in stores if s.strip()], fetch
        self._cache = {}

    def _catalogue(self, store):
        if store not in self._cache:
            base = store if store.startswith("http") else "https://" + store
            data = self.fetch(base + "/products.json?limit=250")
            items = []
            for p in data.get("products", []):
                prices = sorted(float(v["price"]) for v in p.get("variants") or [] if v.get("price"))
                if prices:
                    items.append({"store": store, "title": p.get("title", ""), "price": prices[0],
                                  "url": f"{base}/products/{p.get('handle')}"})
            fid = self.record(base + "/products.json?limit=250", items)   # snapshot keeps title/price/url per product
            self._cache[store] = (items, fid)
        return self._cache[store]

    def prices(self, keyword):
        if not self.stores:
            raise Unavailable("No competitor stores are listed in businesses.json yet.")
        words = [_spelling(w) for w in re.split(r"\W+", keyword.lower()) if len(w) > 2]
        found, fids = [], []
        for store in self.stores:
            try:
                items, fid = self._catalogue(store)
            except Exception as error:
                self.record(f"https://{store}/products.json", {"error": str(error)[:200]}, ok=False)
                continue
            hits = [i for i in items if all(w in _spelling(i["title"].lower()) for w in words)]
            for h in hits:
                found.append(dict(h, fetch_id=fid))
            fids.append(fid)
        return found, (fids[0] if len(fids) == 1 else None)


class ShopifyPublicShop(Adapter):
    """Our own store's public pages (no credentials needed): what customers actually see."""
    name = "shopify_public"

    def __init__(self, public_url, recorder=None, fetch=_get_json):
        super().__init__(recorder)
        self.base, self.fetch = "https://" + public_url.strip("/"), fetch

    def _listing(self, p):
        prices = sorted(float(v["price"]) for v in p.get("variants") or [] if v.get("price"))
        return {"handle": p.get("handle"), "title": p.get("title", ""), "price_min": prices[0] if prices else None,
                "available": any(v.get("available", True) for v in p.get("variants") or []),
                "url": f"{self.base}/products/{p.get('handle')}", "tags": p.get("tags") or [],
                "skus": [v.get("sku") or "" for v in p.get("variants") or []]}

    def products(self):
        data = self.fetch(self.base + "/products.json?limit=250")
        items = [self._listing(p) for p in data.get("products", [])]
        return items, self.record(self.base + "/products.json?limit=250", items)

    def product_page(self, handle):
        try:
            data = self.fetch(f"{self.base}/products/{handle}.json")
        except Exception as error:
            self.record(f"{self.base}/products/{handle}.json", {"error": str(error)[:200]}, ok=False)
            return None, None
        item = self._listing(data.get("product") or {})
        return item, self.record(f"{self.base}/products/{handle}.json", item)


ORDERS_QUERY = """query($q: String, $after: String) {
  orders(first: 100, after: $after, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes { name createdAt
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      lineItems(first: 20) { nodes { title quantity sku } }
      customerJourneySummary { firstVisit { source landingPage referrerUrl
        utmParameters { source medium campaign content } } } } } }"""


class ShopifyApiOrders(Adapter):
    """Orders through the guarded Shopify client (read-only query). Needs the read_orders scope (last 60 days)."""
    name = "shopify_orders_api"

    def __init__(self, recorder=None, client=None):
        super().__init__(recorder)
        self._client = client

    def orders(self, since, now):
        if self._client is None:
            from jarvis_store.shopify import Shopify
            self._client = Shopify()
        q = "created_at:>=" + dt.datetime.fromtimestamp(since, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out, after = [], None
        for _ in range(20):
            data = self._client.query(ORDERS_QUERY, {"q": q, "after": after})["orders"]
            for o in data["nodes"]:
                visit = ((o.get("customerJourneySummary") or {}).get("firstVisit") or {})
                utm = visit.get("utmParameters") or {}
                money = o["currentTotalPriceSet"]["shopMoney"]
                created = dt.datetime.fromisoformat(o["createdAt"].replace("Z", "+00:00")).timestamp()
                out.append({"order_ref": o["name"], "created_at": created, "total": float(money["amount"]), "currency": money["currencyCode"],
                            "items": [{"title": li["title"], "qty": li["quantity"], "sku": li.get("sku")} for li in o["lineItems"]["nodes"]],
                            "attribution": {"utm_source": utm.get("source"), "utm_medium": utm.get("medium"),
                                            "utm_campaign": utm.get("campaign"), "utm_content": utm.get("content"),
                                            "landing_page": visit.get("landingPage"), "referrer": visit.get("referrerUrl"),
                                            "source": visit.get("source")} if visit else None})
            if not data["pageInfo"]["hasNextPage"]:
                break
            after = data["pageInfo"]["endCursor"]
        return out, self.record("shopify-admin-api://orders?" + q, out)


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_admin_date(text, now):
    """'Today at 3:12 pm' / 'Yesterday at 9:05 am' / 'Oct 18 at 4:05 pm' / '18 Oct at ...' (Shopify admin list). None if unsure."""
    t = (text or "").strip().lower().replace(" ", " ")
    m = re.search(r"at (\d{1,2}):(\d{2}) ?([ap]m)", t)
    if not m:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3) == "pm" else 0)
    base = dt.datetime.fromtimestamp(now)
    if t.startswith("today"):
        day = base.date()
    elif t.startswith("yesterday"):
        day = base.date() - dt.timedelta(days=1)
    else:
        md = re.match(r"(?:([a-z]{3})\w* (\d{1,2})|(\d{1,2}) ([a-z]{3})\w*)", t)
        if not md:
            return None
        mon = _MONTHS.get(md.group(1) or md.group(4))
        dom = int(md.group(2) or md.group(3))
        if not mon:
            return None
        year = base.year if (mon, dom) <= (base.month, base.day) else base.year - 1
        day = dt.date(year, mon, dom)
    return dt.datetime.combine(day, dt.time(hour, int(m.group(2)))).timestamp()


class BrowserOrders(Adapter):
    """Orders from the Shopify admin list Jarvis's browser already reads (jarvis_money). No attribution:
    the list doesn't show where a customer came from, so attribution stays unavailable."""
    name = "shopify_orders_browser"

    def __init__(self, recorder=None, reader=None):
        super().__init__(recorder)
        self.reader = reader

    def orders(self, since, now):
        reader = self.reader
        if reader is None:
            import jarvis_money
            reader = jarvis_money.shopify_orders
        reading = reader()
        if not reading.get("ok"):
            self.record("shopify-admin://orders", reading, ok=False)
            raise RuntimeError(reading.get("problem") or "couldn't read Shopify orders")
        out = []
        for o in reading.get("orders") or []:
            created = parse_admin_date(o.get("date"), now)
            try:
                total = float(re.sub(r"[^\d.]", "", o.get("total") or ""))
            except ValueError:
                continue
            if created is None or created < since:
                continue
            out.append({"order_ref": o["number"], "created_at": created, "total": total, "currency": "GBP",
                        "items": [{"title": o.get("items") or "", "qty": None}], "attribution": None})
        return out, self.record("shopify-admin://orders", out)


class TikTokStudioAnalytics(Adapter):
    """Per-video numbers from the TikTok Studio reading the Advertising tab keeps (jarvis_v2.advertising).
    TikTok Studio's list has views, likes and comments only; everything else is reported unavailable."""
    name = "tiktok_studio"
    METRICS = ("views", "likes", "comments", "shares", "avg_watch_seconds", "completion_rate", "link_clicks", "sessions")

    def __init__(self, recorder=None, loader=None):
        super().__init__(recorder)
        self.loader = loader

    def post_metrics(self, post, now):
        loader = self.loader
        if loader is None:
            from jarvis_v2 import advertising
            loader = advertising.load
        t = (loader() or {}).get("tiktok") or {}
        if not t.get("ok"):
            raise Unavailable(t.get("problem") or "TikTok hasn't been read yet (Advertising tab).")
        start = " ".join(post["caption"].split())[:50]
        video = next((v for v in t.get("videos") or [] if " ".join((v.get("caption") or "").split()).startswith(start[:40])), None)
        if video is None:
            raise Unavailable("This post isn't in the latest TikTok Studio reading yet.")
        out = {m: None for m in self.METRICS}
        out.update(views=video.get("views"), likes=video.get("likes"), comments=video.get("comments"),
                   observed_at=t.get("checked_at"))
        return out, self.record("https://www.tiktok.com/tiktokstudio/content", dict(video, checked_at=t.get("checked_at")))


class TikTokBrowserPublisher(Adapter):
    """Posts through jarvis_v2.posting (Jarvis's logged-in browser, caption verified word for word,
    AI label on). Only ever called by the testing agent AFTER controls.require consumed Danny's approval."""
    name = "tiktok_browser"

    def __init__(self, recorder=None, add=None, post=None, video_root=None, ai_label=True):
        super().__init__(recorder)
        self._add, self._post, self.video_root, self.ai_label = add, post, video_root, ai_label

    def publish(self, post, video_path):
        from jarvis_v2 import posting
        add, do_post = self._add or posting.add, self._post or posting.post
        root = self.video_root or posting.VIDEO_ROOT
        if not video_path or not os.path.isfile(video_path):
            raise RuntimeError("There's no rendered video file to post.")
        dest_dir = os.path.join(root, "marketing")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"post-{post['id']}-{post['content_hash'][:10]}.mp4")
        shutil.copyfile(video_path, dest)
        item = add(dest, post["caption"], f"Marketing: {post.get('title') or 'post ' + str(post['id'])}",
                   source=f"marketing post {post['id']}", ai_generated=self.ai_label)
        result = do_post(item["id"])
        self.record(f"tiktok-browser://post/{item['id']}", {k: result.get(k) for k in ("id", "status", "error", "note")})
        if result.get("status") == "posted":
            return {"external_ref": f"tiktok-queue-{item['id']}", "status": "posted"}
        if result.get("status") == "check":
            return {"external_ref": f"tiktok-queue-{item['id']}", "status": "check", "note": result.get("error")}
        raise RuntimeError(result.get("error") or "TikTok posting failed")


def bundle(business, recorder):
    from .adlibrary import MetaAdLibrary
    from .commerce import MetaMarketing, ShopifyAdmin
    orders = ShopifyApiOrders(recorder) if business.get("orders_source") == "api" else BrowserOrders(recorder)
    meta = business.get("meta") or {}
    return {"supplier": CJSupplier(recorder),
            "ad_library": MetaAdLibrary(recorder, countries=meta.get("ad_library_countries") or ["IE"]),
            "shop_admin": ShopifyAdmin(recorder), "paid": MetaMarketing(meta, recorder),
            "competitors": ShopifyPublicCompetitors(business.get("competitor_stores") or [], recorder),
            "trends": Unsupported("trends", "Google Trends' official API is an invite-only alpha; no access is configured.", recorder),
            "shortform": Unsupported("shortform", "TikTok's Research API is for non-profit academic researchers only.", recorder),
            "reviews": Unsupported("reviews", "No supported review source is configured yet.", recorder),
            "shop": ShopifyPublicShop(business["public_url"], recorder),
            "orders": orders, "analytics": TikTokStudioAnalytics(recorder),
            "publisher": TikTokBrowserPublisher(recorder, ai_label=business.get("ai_label", True)),
            "video": LocalSlideshow(recorder)}

