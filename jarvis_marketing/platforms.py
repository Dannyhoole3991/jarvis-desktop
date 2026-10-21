"""
Sales platforms: what each one actually allows, costs and supports, as researched on 2026-10-20.

Every rule carries its source and how sure we are:
    verified     read on the platform's own page (quoted in the source note)
    unconfirmed  only third-party guides agree; check the platform before acting on it
    unknown      no reliable source found; treated as a problem, never as a pass

Nothing here registers for anything. A platform is CONNECTED only when Jarvis has an authorised
integration it can use; everything else is research and preparation. Fees are ESTIMATES built from
the researched rate cards and always shown with their basis.
"""

CHECKED = "2026-10-20"
ALLOWED, CONDITIONAL, NOT_ALLOWED, UNCLEAR = "allowed", "allowed_with_conditions", "not_allowed", "unclear"


def _fact(text, status, source):
    return {"text": text, "status": status, "source": source, "checked": CHECKED}


PLATFORMS = {
    "shopify": {
        "name": "Shopify (collectivefinds.co.uk)", "kind": "own_store",
        "dropshipping": ALLOWED, "dropshipping_facts": [_fact("Our own store: we set the policy and fulfil via CJ's Shopify app.", "verified", "own store")],
        "cj_compatible": True,
        "fees": {"referral_pct": 0.0, "per_order": 0.0, "vat_on_fees": False, "monthly": None,
                 "status": "verified", "basis": "card fees are already in the business economics (2% + 25p, conservative)",
                 "includes_payment_processing": False},
        "integration": {"type": "official_api", "name": "Shopify Admin API (guarded client)", "connected": True,
                        "needs": []},
        "sync": {"stock": "CJ app on Shopify (outside Jarvis); Jarvis checks supplier stock", "price": "Shopify is the master price",
                 "orders": "Shopify orders; CJ app forwards to CJ once the product is connected in CJ", "tracking": "CJ app writes tracking to Shopify",
                 "reliable": True},
        "delivery": {"max_days_expected": None, "note": "We state CJ's delivery window on every product page."},
        "restrictions": [], "demand_source": "our own orders", "steps": [],
    },
    "google": {
        "name": "Google free listings (Merchant Center)", "kind": "search_listing",
        "dropshipping": ALLOWED,
        "dropshipping_facts": [_fact("Products must be sold on our own site, which must show accurate price, availability, delivery and returns.",
                                     "unconfirmed", "Merchant Center policies (summary)")],
        "cj_compatible": True,
        "fees": {"referral_pct": 0.0, "per_order": 0.0, "vat_on_fees": False, "monthly": 0.0, "status": "verified",
                 "basis": "free listings cost nothing; the sale happens on our Shopify store", "includes_payment_processing": False},
        "integration": {"type": "shopify_channel", "name": "Shopify 'Google & YouTube' sales channel (syncs products to Merchant Center)",
                        "connected": None,
                        "needs": ["Install the Google & YouTube app in Shopify and link a Google Merchant Center account (UK, GBP).",
                                  "For Jarvis to read approval/disapproval statuses: a Google Cloud OAuth client with the Merchant API "
                                  "and Search Console API enabled (Danny signs in once)."]},
        "sync": {"stock": "automatic via the Shopify channel", "price": "automatic via the Shopify channel",
                 "orders": "orders happen on Shopify", "tracking": "Shopify", "reliable": True},
        "spec": {"title_max": 150, "description_max": 5000, "id_max": 50, "brand_max": 70, "mpn_max": 70, "images_max": 11,
                 "source": "support.google.com/merchants/answer/7052112 (read 2026-10-20)"},
        "delivery": {"max_days_expected": None, "note": "Delivery times shown must match the site."},
        "restrictions": ["No promotional text in titles (e.g. 'free delivery', 'sale').",
                         "Brand: the real brand; for generic products don't invent one. identifier_exists = no when there's no GTIN/MPN."],
        "demand_source": "Search Console queries (needs OAuth); Google Trends API is invite-only", "steps": [],
    },
    "meta_shops": {
        "name": "Facebook & Instagram Shops", "kind": "social_catalog",
        "dropshipping": ALLOWED,
        "dropshipping_facts": [_fact("UK shops use website checkout: every sale completes on our own site (in-app checkout removed for UK by Sept 2025).",
                                     "unconfirmed", "simplybusiness.co.uk / softomatesolutions.com (2025-2026)")],
        "cj_compatible": True,
        "fees": {"referral_pct": 0.0, "per_order": 0.0, "vat_on_fees": False, "monthly": 0.0, "status": "unconfirmed",
                 "basis": "website checkout: no Meta selling fee; the sale is a normal Shopify order", "includes_payment_processing": False},
        "integration": {"type": "shopify_channel", "name": "Shopify 'Facebook & Instagram' channel (catalogue sync)", "connected": None,
                        "needs": ["Install the Facebook & Instagram app in Shopify, connect the Facebook page, Instagram account and a Commerce account.",
                                  "Catalogue review usually takes 24-48 hours (unconfirmed)."]},
        "sync": {"stock": "automatic via the Shopify channel", "price": "automatic via the Shopify channel", "orders": "Shopify",
                 "tracking": "Shopify", "reliable": True},
        "delivery": {"max_days_expected": None, "note": "Meta commerce policies apply to the catalogue."},
        "restrictions": ["Meta Commerce Policies (no prohibited items, accurate listings)."],
        "demand_source": "none via API", "steps": [],
    },
    "pinterest": {
        "name": "Pinterest (catalogue / product Pins)", "kind": "social_catalog",
        "dropshipping": ALLOWED,
        "dropshipping_facts": [_fact("Product Pins link to our site; UK merchants may need the Verified Merchant Program for rich price display.",
                                     "unconfirmed", "simplepinmedia.com; Pinterest Shopify app launch coverage (2020)")],
        "cj_compatible": True,
        "fees": {"referral_pct": 0.0, "per_order": 0.0, "vat_on_fees": False, "monthly": 0.0, "status": "unconfirmed",
                 "basis": "organic product Pins are free; the sale is a Shopify order", "includes_payment_processing": False},
        "integration": {"type": "shopify_channel", "name": "Pinterest app for Shopify (catalogue + tag)", "connected": None,
                        "needs": ["Install the Pinterest app in Shopify and connect a Pinterest business account; check current UK eligibility in the app."]},
        "sync": {"stock": "daily via the app", "price": "daily via the app", "orders": "Shopify", "tracking": "Shopify", "reliable": True},
        "delivery": {"max_days_expected": None, "note": ""},
        "restrictions": ["Pinterest merchant guidelines."], "demand_source": "none via API", "steps": [],
    },
    "ebay_uk": {
        "name": "eBay UK", "kind": "marketplace",
        "dropshipping": CONDITIONAL,
        "dropshipping_facts": [
            _fact("'Dropshipping, where you fulfil orders directly from a wholesale supplier, is allowed on eBay.'", "verified",
                  "ebay.co.uk/help/policies/selling-policies/drop-shipping-policy?id=4176"),
            _fact("'Purchasing the item from another retailer or marketplace that ships directly to your customer is not allowed.'", "verified",
                  "ebay.co.uk drop shipping policy"),
            _fact("The seller stays responsible for delivery within the stated time and for the buyer's experience.", "verified",
                  "ebay.co.uk drop shipping policy")],
        "cj_compatible": None, "cj_note": "CJ presents itself as a dropshipping supplier/agent; whether eBay treats it as a 'wholesale supplier' "
                                          "is a judgement Danny must make (keep the CJ agreement on file).",
        "fees": {"referral_pct": 0.129, "per_order": 0.40, "per_order_low": 0.30, "per_order_threshold": 10.0, "extra_pct": 0.0035,
                 "vat_on_fees": True, "monthly": None, "status": "unconfirmed",
                 "basis": "business seller: final value fee ~12.9% (sources conflict 10.9-13.25%) + 30p/40p per order (eBay notice Feb 2026) "
                          "+ 0.35% regulatory fee; 20% VAT on fees if not VAT-registered", "includes_payment_processing": True},
        "integration": {"type": "official_api", "name": "eBay Sell APIs (Inventory/Fulfillment) or the eBay Shopify app", "connected": False,
                        "needs": ["An eBay UK business seller account (Danny).", "eBay developer keys + user consent, or a reviewed multichannel app."]},
        "sync": {"stock": "not connected", "price": "not connected", "orders": "not connected", "tracking": "not connected", "reliable": False},
        "delivery": {"max_days_expected": 10, "note": "Stated handling + delivery time must be met; late deliveries hurt seller standards (unconfirmed limit)."},
        "restrictions": ["eBay prohibited and restricted items policy.", "Electrical items need UK safety marking information."],
        "demand_source": "eBay Browse API (needs keys)", "steps": [],
    },
    "amazon_uk": {
        "name": "Amazon.co.uk", "kind": "marketplace",
        "dropshipping": CONDITIONAL,
        "dropshipping_facts": [
            _fact("Allowed only if we are the seller of record: our name on packing slips/invoices/packaging, no third-party identifiers, "
                  "and we handle returns.", "unconfirmed", "sellersprite.com; Seller Central Europe forum; looperbuy.com (2026)"),
            _fact("Shipping with another retailer's identity on packing slips can restrict merchant-fulfilled selling.", "unconfirmed",
                  "Seller Central Europe forum post")],
        "cj_compatible": None, "cj_note": "Needs CJ to ship in neutral packaging with our name and no CJ/Chinese-seller paperwork; "
                                          "long CJ delivery windows risk late-delivery metrics.",
        "fees": {"referral_pct": 0.08, "referral_pct_high": 0.15, "referral_threshold": 20.0, "min_fee": 0.25, "fee_tax_pct": 0.02,
                 "vat_on_fees": True, "monthly": 25.0, "status": "unconfirmed",
                 "basis": "Home: 8% up to £20, 15% above (min 25p), +2% Digital Services Tax on fees, Professional plan £25/month ex VAT "
                          "(elverecommerceaccountants.co.uk, worldfirst.com 2026)", "includes_payment_processing": True},
        "integration": {"type": "official_api", "name": "Amazon Selling Partner API", "connected": False,
                        "needs": ["An Amazon Professional seller account with identity verification (Danny).",
                                  "SP-API developer registration and authorisation.", "GTIN exemption or barcodes for each product."]},
        "sync": {"stock": "not connected", "price": "not connected", "orders": "not connected", "tracking": "not connected", "reliable": False},
        "delivery": {"max_days_expected": 7, "note": "Merchant-fulfilled orders are measured on late shipment and valid tracking (unconfirmed thresholds)."},
        "restrictions": ["Products need a GTIN or an approved GTIN exemption.", "Some categories need approval.",
                         "Electrical items need UK safety marking information."],
        "demand_source": "SP-API catalogue (needs authorisation)", "steps": [],
    },
    "tiktok_shop_uk": {
        "name": "TikTok Shop UK", "kind": "marketplace",
        "dropshipping": UNCLEAR,
        "dropshipping_facts": [
            _fact("No official UK rule found. Third-party guides conflict: some say the model is allowed under strict conditions; a Sept 2026 "
                  "report says US packing rules ban shipping directly from other retailers.", "unconfirmed",
                  "looperbuy.com; relevantaudience.com (2026)"),
            _fact("Orders reportedly must be delivered quickly (one guide: within 6 business days of awaiting shipment).", "unconfirmed",
                  "looperbuy.com (2026)")],
        "cj_compatible": None, "cj_note": "CJ's 4-12 day UK delivery is likely too slow for TikTok Shop delivery deadlines.",
        "fees": {"referral_pct": 0.09, "per_order": 0.0, "vat_on_fees": True, "monthly": 0.0, "status": "unconfirmed",
                 "basis": "~9% UK commission (channelx.world, Dec 2025; sources conflict); affiliate commission extra if used",
                 "includes_payment_processing": True},
        "integration": {"type": "shopify_channel", "name": "TikTok app for Shopify (TikTok Shop) / TikTok Shop Partner API", "connected": False,
                        "needs": ["A TikTok Shop UK seller account (Danny).", "Confirm in Seller Center whether CJ-fulfilled orders meet UK delivery rules."]},
        "sync": {"stock": "not connected", "price": "not connected", "orders": "not connected", "tracking": "not connected", "reliable": False},
        "delivery": {"max_days_expected": 6, "note": "Unconfirmed: fast delivery deadlines."},
        "restrictions": ["TikTok Shop prohibited/restricted products policy."], "demand_source": "none via API", "steps": [],
    },
    "onbuy": {
        "name": "OnBuy (UK marketplace)", "kind": "marketplace",
        "dropshipping": UNCLEAR,
        "dropshipping_facts": [_fact("No dropshipping policy found in public sources.", "unknown", "searched 2026-10-20")],
        "cj_compatible": None, "cj_note": "Unknown until OnBuy's seller terms are checked.",
        "fees": {"referral_pct": 0.15, "per_order": 0.0, "min_fee": 0.25, "vat_on_fees": True, "monthly": 25.0, "status": "unconfirmed",
                 "basis": "sources conflict (~7% home to 15% default) + subscription from £25/month; 15% used to be safe",
                 "includes_payment_processing": False, "payment_pct": 0.02},
        "integration": {"type": "official_api", "name": "OnBuy seller API", "connected": False,
                        "needs": ["An OnBuy seller account (Danny).", "Check OnBuy's seller terms for dropshipping."]},
        "sync": {"stock": "not connected", "price": "not connected", "orders": "not connected", "tracking": "not connected", "reliable": False},
        "delivery": {"max_days_expected": None, "note": "unknown"},
        "restrictions": [], "demand_source": "none via API", "steps": [],
    },
    "etsy": {
        "name": "Etsy", "kind": "marketplace",
        "dropshipping": NOT_ALLOWED,
        "dropshipping_facts": [
            _fact("Etsy sells items made or designed by the seller, vintage (20+ years) and craft supplies; reselling ready-made goods isn't allowed, "
                  "and production partners can't be dropshipping suppliers of ready-made products.", "unconfirmed",
                  "shopify.co.uk/blog/etsy-dropshipping; nifty.ai (Etsy's own pages refused automated reading)")],
        "cj_compatible": False, "cj_note": "Ready-made CJ products are not our own designs, vintage or craft supplies.",
        "fees": {"referral_pct": 0.065, "per_order": 0.16, "vat_on_fees": True, "monthly": 0.0, "status": "unconfirmed",
                 "basis": "not assessed further: products are ineligible", "includes_payment_processing": False},
        "integration": {"type": "official_api", "name": "Etsy Open API", "connected": False, "needs": []},
        "sync": {"stock": "n/a", "price": "n/a", "orders": "n/a", "tracking": "n/a", "reliable": False},
        "delivery": {"max_days_expected": None, "note": ""},
        "restrictions": ["Only handmade/designed-by-seller, vintage or craft supplies."], "demand_source": "n/a", "steps": [],
    },
}
OTHER_UK = [
    {"name": "Not On The High Street", "verdict": "unlikely", "why": "curated independent/handmade brands; generic CJ products are a poor fit (not verified)"},
    {"name": "Fruugo", "verdict": "not assessed", "why": "cross-border marketplace; terms not checked yet"},
    {"name": "ManoMano", "verdict": "not assessed", "why": "DIY/home marketplace; seller terms not checked yet"},
]


def fee_for(platform, price):
    """Platform selling fees for one order at `price` (GBP). Returns (fees, basis). An ESTIMATE from the rate card above."""
    f = PLATFORMS[platform]["fees"]
    pct = f.get("referral_pct", 0.0)
    if f.get("referral_threshold") and price > f["referral_threshold"]:
        pct = f.get("referral_pct_high", pct)
    fee = price * pct
    if f.get("min_fee"):
        fee = max(fee, f["min_fee"])
    per = f.get("per_order", 0.0)
    if f.get("per_order_threshold") is not None and price <= f["per_order_threshold"]:
        per = f.get("per_order_low", per)
    fee += per + price * f.get("extra_pct", 0.0) + price * f.get("payment_pct", 0.0)
    fee += fee * f.get("fee_tax_pct", 0.0)
    if f.get("vat_on_fees"):
        fee *= 1.2
    return round(fee, 2), f["basis"] + (" (fees include 20% VAT, assuming not VAT-registered)" if f.get("vat_on_fees") else "")


def channel_economics(platform, price, product_cost, shipping, econ_cfg):
    """Contribution per order on one platform: price - cost - shipping - that platform's fees - payment fees
    (unless the platform's fee already covers payment) - expected returns. Everything is an ESTIMATE."""
    f = PLATFORMS[platform]["fees"]
    fees, basis = fee_for(platform, price)
    payment = 0.0 if f.get("includes_payment_processing") else price * econ_cfg["payment_fee_pct"] + econ_cfg["payment_fee_fixed"]
    if f.get("includes_payment_processing") is False and f.get("payment_pct"):
        payment = 0.0                    # already inside fee_for
    returns = price * econ_cfg["return_rate"]
    contribution = round(price - product_cost - shipping - fees - payment - returns - econ_cfg["other_variable_cost"], 2)
    return {"platform": platform, "price": round(price, 2), "product_cost": round(product_cost, 2), "shipping": round(shipping, 2),
            "platform_fees": fees, "payment_fees": round(payment, 2), "expected_returns": round(returns, 2), "contribution": contribution,
            "break_even_cac": max(contribution, 0.0), "monthly_fee": f.get("monthly"),
            "orders_to_cover_monthly_fee": (round(f["monthly"] / contribution, 1) if f.get("monthly") and contribution > 0 else None),
            "fee_status": f["status"], "basis": basis, "label": "estimate"}
