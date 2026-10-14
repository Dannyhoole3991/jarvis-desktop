"""Price = everything it costs us + Danny's profit target, rounded up to .49 / .99."""

import math

from . import config


def round_up_49_99(amount):
    whole = math.floor(amount)
    if amount <= whole + 0.49:
        return round(whole + 0.49, 2)
    if amount <= whole + 0.99:
        return round(whole + 0.99, 2)
    return round(whole + 1.49, 2)


def price_for(product_usd, shipping_usd, kind="everyday"):
    """Returns {price_gbp, cost_gbp, fee_gbp, profit_gbp}. Shipping is free to the customer, so it's inside the price."""
    cost_gbp = (product_usd + shipping_usd + config.SHIPPING_SAFETY_USD) * config.USD_TO_GBP
    target = config.PROFIT_GBP[kind]
    price = round_up_49_99((cost_gbp + config.SHOPIFY_FEE_FIXED_GBP + target) / (1 - config.SHOPIFY_FEE_PCT))
    fee = price * config.SHOPIFY_FEE_PCT + config.SHOPIFY_FEE_FIXED_GBP
    return {"price_gbp": price, "cost_gbp": round(cost_gbp, 2), "fee_gbp": round(fee, 2),
            "profit_gbp": round(price - fee - cost_gbp, 2)}
