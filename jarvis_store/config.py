"""Store settings. Plain constants; secrets never live here (see creds.py)."""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)

SHOP_DOMAIN = os.environ.get("JARVIS_SHOP_DOMAIN", "mwrfta-mh.myshopify.com")
SHOP_PUBLIC_URL = "collectivefinds.co.uk"
SHOP_API_VERSION = os.environ.get("JARVIS_SHOP_API_VERSION", "2026-07")
BRAND = "Collective"
VENDOR = "Collective"

# Every product Jarvis creates carries these tags, so his drafts are easy to find
# and so the guard in shopify.py can tell his products from Danny's.
JARVIS_TAG = "jarvis"
REVIEW_TAG = "needs-review"

# Pricing (see pricing.py). Danny's rules: about GBP 5 profit on everyday items,
# about GBP 2 on Christmas items; costs (product + UK shipping + fees) always covered.
USD_TO_GBP = float(os.environ.get("JARVIS_USD_TO_GBP", "0.75"))
PROFIT_GBP = {"everyday": 5.0, "christmas": 2.0}
SHOPIFY_FEE_PCT = 0.02          # assumed card fee, conservative
SHOPIFY_FEE_FIXED_GBP = 0.25
SHIPPING_SAFETY_USD = 0.50      # small buffer on top of CJ's quoted postage

# Research limits for everyday products.
MAX_DELIVERY_DAYS = 12          # slowest acceptable end of CJ's delivery window
MIN_IMAGES = 4
MIN_LISTED = 30                 # how many other stores list it (a popularity signal)
MAX_PRODUCT_USD = 12.0
MAX_SELL_PRICE_GBP = 25.0

STORE_DATA = os.path.join(PROJECT_ROOT, "store_data")
AUDIT_LOG = os.path.join(STORE_DATA, "audit.jsonl")
DRAFTS_DIR = os.path.join(PROJECT_ROOT, "generated_videos", "jarvis_drafts")

CJ_API = "https://developers.cjdropshipping.com/api2.0/v1"
CJ_MIN_INTERVAL = 1.2           # CJ allows 1 request per second
