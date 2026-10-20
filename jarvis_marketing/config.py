"""
Marketing department settings: paths, the business list and the default thresholds.

Every business has its own entry (store, supplier, competitors, thresholds), so a second shop is
one more entry in data/businesses.json, not new code. Secrets never live here.
"""

import copy
import json
import os

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(PACKAGE_DIR)
DATA_ROOT = os.environ.get("JARVIS_MARKETING_DATA", os.path.join(PACKAGE_DIR, "data"))
BUSINESSES_FILE = os.path.join(DATA_ROOT, "businesses.json")
PASSWORD_FILE = os.path.join(DATA_ROOT, "remote_login.json")     # salted PBKDF2 hash only, never the password
UI_FILE = os.path.join(PACKAGE_DIR, "ui", "marketing.html")

PROFILES = ("mock", "live")
MODEL_VERSION = "jarvis-marketing/1 (rules)"     # the deterministic agents; an LLM adds its own model name

# Product evaluation: a candidate failing ANY of these is rejected, with the reason.
DEFAULT_THRESHOLDS = {
    "min_contribution": 4.00,        # per order, after every variable cost (business currency)
    "min_margin_pct": 0.25,          # contribution / price
    "max_break_even_roas": 4.0,      # above this, paid testing could never pay back
    "max_price": 25.00,
    "max_product_cost": 10.00,       # landed product cost before shipping (business currency)
    "max_delivery_days": 12,
    "min_images": 4,
    "min_facts": 3,                  # sourced facts needed before anything is scored
    "min_confidence": 0.5,           # share of the score backed by real data
    "max_competitor_price_ratio": 1.20,   # proposed price vs the median competitor price
    "min_stock": 1,
}
DEFAULT_WEIGHTS = {"margin": 0.30, "demand": 0.25, "competition": 0.15, "supplier": 0.15, "content_fit": 0.15}

DEFAULT_ECONOMICS = {
    "payment_fee_pct": 0.02,         # same conservative card fee jarvis_store uses
    "payment_fee_fixed": 0.25,
    "return_rate": 0.05,             # ESTIMATE until real refunds exist
    "other_variable_cost": 0.00,
    "target_contribution": 5.00,
    "usd_to_business": 0.75,         # fixed rate from jarvis_store config: conversions are labelled estimates
}

DEFAULT_TESTING = {
    "attribution_window_days": 7,    # an order counts for a post only within this many days of the post
    "data_delay_hours": 48,          # analytics/orders younger than this are not final yet
    "min_sessions_per_variant": 150,
    "stop_sessions_without_order": 400,
    "significance": 0.05,
    "experiment_days": 7,
    "posts_per_experiment": 3,
    "max_revisions": 2,
    "analytics_ttl_hours": 26,       # a reading older than this is stale and never used for a verdict
}

DEFAULT_LIMITS = {
    "max_tasks_per_cycle": 60,
    "task_timeout_s": 300,
    "task_max_attempts": 2,
    "cycle_timeout_s": 3600,
    "keywords_per_cycle": 4,
    "products_per_keyword": 4,
    "shortlist_size": 3,
    "briefs_per_product": 3,
}

COLLECTIVE_FINDS = {
    "slug": "collective-finds",
    "name": "Collective Finds",
    "profile": "mock",                # "live" only once Danny has configured section 9 of the plan
    "currency": "GBP",
    "shop_domain": "mwrfta-mh.myshopify.com",
    "public_url": "collectivefinds.co.uk",
    "supplier": "cj",
    "platforms": ["tiktok"],
    "competitor_stores": [],          # Shopify stores whose public /products.json may be read for prices
    "seed_keywords": [],              # empty = jarvis_store's everyday seed list
    "orders_source": "browser",       # "api" needs the read_orders scope on the Shopify app
    "thresholds": {},
    "weights": {},
    "economics": {},
    "testing": {},
    "limits": {},
}


def _merge(defaults, overrides):
    out = dict(defaults)
    out.update({k: v for k, v in (overrides or {}).items() if k in defaults})
    return out


def load_businesses(path=None):
    """All businesses, with defaults filled in. A missing file means just Collective Finds."""
    path = path or BUSINESSES_FILE
    items = [copy.deepcopy(COLLECTIVE_FINDS)]
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            items = json.load(f).get("businesses") or items
    out = []
    for b in items:
        full = dict(copy.deepcopy(COLLECTIVE_FINDS), **b)
        if full["profile"] not in PROFILES:
            raise ValueError(f"Business {full['slug']}: profile must be one of {PROFILES}.")
        full["thresholds"] = _merge(DEFAULT_THRESHOLDS, b.get("thresholds"))
        full["weights"] = _merge(DEFAULT_WEIGHTS, b.get("weights"))
        full["economics"] = _merge(DEFAULT_ECONOMICS, b.get("economics"))
        full["testing"] = _merge(DEFAULT_TESTING, b.get("testing"))
        full["limits"] = _merge(DEFAULT_LIMITS, b.get("limits"))
        out.append(full)
    return out


def business(slug=None, path=None):
    items = load_businesses(path)
    if slug is None:
        return items[0]
    for b in items:
        if b["slug"] == slug:
            return b
    raise ValueError(f"No business called {slug}.")


def data_dir(profile):
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {PROFILES}")
    path = os.path.join(DATA_ROOT, profile)
    os.makedirs(path, exist_ok=True)
    return path


def db_path(profile):
    return os.path.join(data_dir(profile), "marketing.db")


def assets_dir(profile):
    path = os.path.join(data_dir(profile), "assets")
    os.makedirs(path, exist_ok=True)
    return path
