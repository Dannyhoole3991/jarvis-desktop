"""
SIMULATED adapters. Nothing here is real: every reading is recorded with simulated=1 and every
URL starts with mock://, so the dashboard and reports always show it as SIMULATED.

The World is a deterministic simulation (seeded per business), so the same inputs give the same
outputs, and it holds no state: what a post "earned" is worked out from the post itself and the
time, which keeps restarts and recovery testable. Some concepts are truly better than others
inside the simulation, so the testing maths has something real to find (or to call inconclusive).
"""

import hashlib
import math
import random
import re

from . import Adapter, Unavailable

KEYWORDS = ["cable organizer", "drawer organizer", "phone stand", "lint remover", "magnetic hook", "spice rack",
            "kids night light", "desk organizer"]
CONCEPT_LIFT = {"problem_solution": 1.6, "demo_closeup": 1.3, "price_reveal": 1.0, "options_showcase": 0.9,
                "gift_idea": 1.1, "unboxing": 0.8}
DAY = 86400


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _rng(*parts):
    return random.Random(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest())


class World:
    def __init__(self, seed="collective-finds"):
        self.seed = seed

    # ------------------------------------------------------------ catalogue
    def products_for(self, keyword):
        r = _rng(self.seed, "kw", keyword)
        out = []
        for n in range(1, 5):
            pid = f"MOCK-{_slug(keyword)}-{n}"
            out.append(self.product(pid))
        r.shuffle(out)
        return out

    def product(self, pid):
        m = re.fullmatch(r"MOCK-(.+)-(\d)", pid)
        if not m:
            raise KeyError(pid)
        keyword, n = m.group(1).replace("-", " "), int(m.group(2))
        r = _rng(self.seed, "p", pid)
        style = ["Compact", "Premium", "Foldable", "Classic"][n - 1]
        price = round(r.uniform(1.6, 8.5), 2)
        if n == 4:
            price = round(r.uniform(11.0, 16.0), 2)            # always too expensive for the thresholds
        images = r.randint(2, 9) if n != 2 else r.randint(5, 9)
        days_max = r.choice([7, 8, 9, 10, 11, 12, 15, 18]) if n != 1 else r.choice([7, 8, 9])
        colours = r.sample(["Black", "White", "Grey", "Green", "Pink", "Blue"], r.randint(1, 3))
        return {"id": pid, "keyword": keyword, "name": f"{style} {keyword.title()}", "price_usd": price,
                "listed": r.choice([15, 80, 300, 900, 2400, 5200]), "images": [f"mock://images/{pid}/{i}.jpg" for i in range(images)],
                "delivery_days": [max(3, days_max - 4), days_max], "shipping_usd": round(r.uniform(1.5, 4.0), 2),
                "stock": r.choice([0, 40, 250, 1200]) if n == 3 else r.choice([40, 250, 1200]),
                "colours": colours, "material": r.choice(["ABS plastic", "silicone", "bamboo", "aluminium"]),
                "packing_list": f"1 x {keyword}", "true_cvr": round(r.uniform(0.008, 0.03), 4),
                "in_store": n in (1, 2), "store_price": None}

    # ------------------------------------------------------------ what posts "earn"
    def post_outcome(self, post, now):
        """Simulated numbers for one published post, growing over its first 3 days."""
        r = _rng(self.seed, "post", post["content_hash"])
        product = self.product(post["supplier_product_id"])
        age_days = max(0.0, (now - post["published_at"]) / DAY)
        grown = min(1.0, age_days / 3.0)
        lift = CONCEPT_LIFT.get(post.get("concept"), 1.0)
        views = int(r.lognormvariate(math.log(2500 * lift), 0.6) * grown)
        ctr = r.uniform(0.008, 0.016) * lift
        sessions = int(views * ctr)
        cvr = product["true_cvr"] * lift
        orders = sum(1 for _ in range(sessions) if r.random() < cvr)
        return {"views": views, "likes": int(views * r.uniform(0.03, 0.08)), "comments": int(views * r.uniform(0.001, 0.004)),
                "shares": int(views * r.uniform(0.002, 0.01)), "avg_watch_seconds": round(r.uniform(3.0, 9.0) * min(lift, 1.4), 1),
                "completion_rate": round(r.uniform(0.08, 0.3) * min(lift, 1.4), 3), "link_clicks": sessions,
                "sessions": sessions, "orders": orders}

    def orders(self, posts, since, now, price_for):
        """Simulated orders: attributed ones carry the post's UTM; a few baseline orders carry none."""
        out = []
        for post in posts:
            if not post.get("published_at"):
                continue
            n = self.post_outcome(post, now)["orders"]
            r = _rng(self.seed, "orders", post["content_hash"])
            for i in range(n):
                created = post["published_at"] + r.uniform(0.05, 3.0) * DAY
                if since <= created <= now:
                    price = price_for(post)
                    out.append({"order_ref": f"#SIM-{post['id']}-{i + 1}", "created_at": created, "total": price, "currency": "GBP",
                                "items": [{"title": post.get("title", ""), "qty": 1, "price": price}],
                                "attribution": {"utm_source": "tiktok", "utm_medium": "organic",
                                                "utm_campaign": f"exp-{post['experiment_id']}", "utm_content": f"post-{post['id']}"}})
        r = _rng(self.seed, "baseline", int(since // DAY))
        day = math.floor(since / DAY) * DAY
        k = 0
        while day < now:
            for _ in range(r.choice([0, 0, 1])):
                created = day + r.uniform(0, DAY)
                if since <= created <= now:
                    k += 1
                    out.append({"order_ref": f"#SIM-BASE-{int(day // DAY)}-{k}", "created_at": created, "total": 14.99, "currency": "GBP",
                                "items": [{"title": "Existing product", "qty": 1, "price": 14.99}], "attribution": None})
            day += DAY
        return out


# ---------------------------------------------------------------- adapters
class MockSupplier(Adapter):
    name, simulated = "mock_supplier", True

    def __init__(self, world, recorder=None):
        super().__init__(recorder)
        self.world = world

    def search(self, keyword):
        items = [{"id": p["id"], "name": p["name"], "price_usd": p["price_usd"], "listed": p["listed"], "image": p["images"][0] if p["images"] else None}
                 for p in self.world.products_for(keyword)]
        return items, self.record(f"mock://supplier/search?q={_slug(keyword)}", items)

    def details(self, pid):
        p = self.world.product(pid)
        d = {"pid": pid, "name": p["name"], "images": p["images"], "material": p["material"], "packing_list": p["packing_list"],
             "category": p["keyword"], "stock": p["stock"], "options": [{"name": "Colour", "values": p["colours"]}],
             "variants": [{"vid": f"{pid}-{c}", "name": c, "price_usd": p["price_usd"]} for c in p["colours"]],
             "description": f"Material: {p['material']}. Packing list: {p['packing_list']}."}
        return d, self.record(f"mock://supplier/product/{pid}", d)

    def shipping(self, vid):
        p = self.world.product(vid.rsplit("-", 1)[0])
        opts = [{"name": "Mock Packet UK", "usd": p["shipping_usd"], "min_days": p["delivery_days"][0], "max_days": p["delivery_days"][1]}]
        return opts, self.record(f"mock://supplier/shipping/{vid}", opts)


class MockCompetitors(Adapter):
    name, simulated = "mock_competitors", True

    def __init__(self, world, recorder=None):
        super().__init__(recorder)
        self.world = world

    def prices(self, keyword):
        r = _rng(self.world.seed, "comp", keyword)
        base = sorted(p["price_usd"] for p in self.world.products_for(keyword))[1] * 0.75 + 11
        items = [{"store": f"mock-rival-{i}.example", "title": f"{keyword.title()} {i}", "price": round(base * r.uniform(0.8, 1.35), 2),
                  "url": f"mock://rival-{i}/products/{_slug(keyword)}"} for i in range(1, r.randint(3, 7))]
        return items, self.record(f"mock://competitors?q={_slug(keyword)}", items)


class MockTrends(Adapter):
    name, simulated = "mock_trends", True

    def __init__(self, world, recorder=None):
        super().__init__(recorder)
        self.world = world

    def interest(self, keyword):
        r = _rng(self.world.seed, "trend", keyword)
        peak = r.randint(1, 12)
        weekly = [round(40 + 30 * math.cos((w / 52 * 12 + 1 - peak) / 12 * 2 * math.pi) + r.uniform(-5, 5)) for w in range(52)]
        d = {"keyword": keyword, "weekly_index": weekly, "peak_month": peak, "trend_12w": round((sum(weekly[-12:]) - sum(weekly[-24:-12])) / 12, 1)}
        return d, self.record(f"mock://trends?q={_slug(keyword)}", d)


class MockShortForm(Adapter):
    name, simulated = "mock_shortform", True

    def __init__(self, world, recorder=None):
        super().__init__(recorder)
        self.world = world

    def signals(self, keyword):
        r = _rng(self.world.seed, "sf", keyword)
        d = {"keyword": keyword, "videos_sampled": r.randint(10, 60), "median_views": r.choice([800, 4000, 15000, 60000]),
             "shop_videos_share": round(r.uniform(0.1, 0.9), 2)}
        return d, self.record(f"mock://shortform?q={_slug(keyword)}", d)


class MockReviews(Adapter):
    name, simulated = "mock_reviews", True

    def __init__(self, world, recorder=None):
        super().__init__(recorder)
        self.world = world

    def complaints(self, pid):
        r = _rng(self.world.seed, "rev", pid)
        themes = r.sample(["smaller than expected", "slow delivery", "flimsy", "colour differs from photos", "works well"], 2)
        d = {"pid": pid, "reviews_sampled": r.randint(5, 80), "themes": themes}
        return d, self.record(f"mock://reviews/{pid}", d)


class MockShop(Adapter):
    """Our own store, simulated: products 1 and 2 of each mock keyword are 'listed' at a .99 price."""
    name, simulated = "mock_store", True

    def __init__(self, world, recorder=None):
        super().__init__(recorder)
        self.world = world

    def _listing(self, p):
        price = math.floor(p["price_usd"] * 0.75 + p["shipping_usd"] * 0.75 + 7) + 0.99
        return {"handle": _slug(p["name"]), "title": p["name"], "price_min": price, "available": True,
                "url": f"https://collectivefinds.co.uk/products/{_slug(p['name'])}", "supplier_product_id": p["id"]}

    def products(self):
        items = [self._listing(p) for kw in KEYWORDS for p in self.world.products_for(kw) if p["in_store"]]
        return items, self.record("mock://store/products.json", items)

    def product_page(self, handle):
        for item in self.products()[0]:
            if item["handle"] == handle:
                return item, self.record(f"mock://store/products/{handle}", item)
        self.record(f"mock://store/products/{handle}", {"error": "not found"}, ok=False)
        return None, None


class MockOrders(Adapter):
    name, simulated = "mock_orders", True

    def __init__(self, world, recorder=None):
        super().__init__(recorder)
        self.world = world
        self.posts_source = lambda: []
        self.price_for = lambda post: 9.99

    def orders(self, since, now):
        items = self.world.orders(self.posts_source(), since, now, self.price_for)
        return items, self.record(f"mock://shopify/orders?since={int(since)}", items)


class MockAnalytics(Adapter):
    name, simulated = "mock_analytics", True

    def __init__(self, world, recorder=None):
        super().__init__(recorder)
        self.world = world
        self.missing = set()           # tests: pretend the platform doesn't report these

    def post_metrics(self, post, now):
        out = self.world.post_outcome(post, now)
        out.pop("orders")
        for m in self.missing:
            out[m] = None
        return out, self.record(f"mock://tiktok/analytics/{post['external_ref']}", out)


class MockPublisher(Adapter):
    name, simulated = "mock_publisher", True

    def __init__(self, recorder=None):
        super().__init__(recorder)
        self.calls = []
        self.fail_next = None          # tests: an error to raise on the next publish

    def publish(self, post, video_path):
        self.calls.append(post["id"])
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise RuntimeError(error)
        ref = "mock-tt-" + post["content_hash"][:12]
        self.record(f"mock://tiktok/post/{ref}", {"posted": ref, "caption": post["caption"]})
        return {"external_ref": ref, "status": "posted"}


class Unsupported(Adapter):
    """A capability we have no real source for. Every call says so; nothing is guessed."""

    def __init__(self, name, reason, recorder=None):
        super().__init__(recorder)
        self.name, self.unavailable_reason = name, reason

    def __getattr__(self, item):
        if item.startswith("_"):
            raise AttributeError(item)

        def refuse(*args, **kwargs):
            raise Unavailable(self.unavailable_reason)
        return refuse


def bundle(world, recorder):
    from .video import MockVideo
    return {"supplier": MockSupplier(world, recorder), "competitors": MockCompetitors(world, recorder),
            "trends": MockTrends(world, recorder), "shortform": MockShortForm(world, recorder),
            "reviews": MockReviews(world, recorder), "shop": MockShop(world, recorder),
            "orders": MockOrders(world, recorder), "analytics": MockAnalytics(world, recorder),
            "publisher": MockPublisher(recorder), "video": MockVideo(recorder), "world": world}
