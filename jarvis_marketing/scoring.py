"""
Product economics and the scoring rules, all transparent and configurable (config.DEFAULT_THRESHOLDS
/ DEFAULT_WEIGHTS, overridable per business).

    contribution per order = price - product cost - shipping - payment fees - expected returns - other variable costs
    break-even CAC         = contribution            (the most one order can cost to win before it loses money)
    break-even ROAS        = price / contribution    (revenue per 1 of ad spend needed to break even)

Expected returns = return rate x price (a dropshipped return is refunded and the goods are not recovered).
A score component with no real data scores 0 and lowers the confidence; nothing unknown is filled in.
"""

import math
import statistics


def round_up_49_99(amount):
    whole = math.floor(amount)
    if amount <= whole + 0.49:
        return round(whole + 0.49, 2)
    if amount <= whole + 0.99:
        return round(whole + 0.99, 2)
    return round(whole + 1.49, 2)


def unit_economics(price, product_cost, shipping, payment_fee_pct=0.02, payment_fee_fixed=0.25, return_rate=0.05,
                   other_variable_cost=0.0):
    for name, v in (("price", price), ("product_cost", product_cost), ("shipping", shipping)):
        if v is None or v < 0:
            raise ValueError(f"{name} must be a known, non-negative number")
    if not 0 <= return_rate < 1 or not 0 <= payment_fee_pct < 1:
        raise ValueError("rates must be between 0 and 1")
    payment_fees = price * payment_fee_pct + payment_fee_fixed
    expected_returns = price * return_rate
    contribution = price - product_cost - shipping - payment_fees - expected_returns - other_variable_cost
    out = {"price": round(price, 2), "product_cost": round(product_cost, 2), "shipping": round(shipping, 2),
           "payment_fees": round(payment_fees, 2), "expected_returns": round(expected_returns, 2),
           "other_variable_cost": round(other_variable_cost, 2), "contribution": round(contribution, 2),
           "margin_pct": round(contribution / price, 4) if price else None,
           "break_even_cac": round(contribution, 2) if contribution > 0 else 0.0,
           "break_even_roas": round(price / contribution, 2) if contribution > 0 else None,
           "formula": "price - product cost - shipping - payment fees - expected returns - other variable costs"}
    return out


def propose_price(product_cost, shipping, target_contribution, payment_fee_pct=0.02, payment_fee_fixed=0.25,
                  return_rate=0.05, other_variable_cost=0.0):
    """The lowest .49/.99 price that still makes `target_contribution` per order. A PROPOSAL only: prices are never changed."""
    keep = 1 - payment_fee_pct - return_rate
    if keep <= 0:
        raise ValueError("fees and returns take the whole price")
    return round_up_49_99((product_cost + shipping + payment_fee_fixed + other_variable_cost + target_contribution) / keep)


def _clamp(x):
    return max(0.0, min(1.0, x))


def components(econ, signals):
    """0..1 per component, or None when there is no real data for it. `signals` come from FACTS only."""
    out = {"margin": _clamp((econ.get("margin_pct") or 0) / 0.5) if econ else None}
    listed = signals.get("supplier_listed")
    out["demand"] = _clamp(math.log10(listed + 1) / 3.5) if isinstance(listed, (int, float)) else None
    comp = signals.get("competitor_prices") or []
    if comp and econ:
        ratio = econ["price"] / statistics.median(comp)
        out["competition"] = _clamp(1.4 - ratio)        # cheaper than the median scores higher
    else:
        out["competition"] = None
    days, images = signals.get("delivery_days_max"), signals.get("images")
    if isinstance(days, (int, float)):
        out["supplier"] = _clamp((15 - days) / 10) * (1.0 if (signals.get("stock") or 0) > 0 else 0.5 if signals.get("stock") is None else 0.0)
    else:
        out["supplier"] = None
    out["content_fit"] = _clamp(images / 8) if isinstance(images, (int, float)) else None
    return out


def score(econ, signals, thresholds, weights):
    """{score 0..100, confidence, components, rejections[...], explanation}. Rejected if ANY threshold fails."""
    comps = components(econ, signals)
    total_w = sum(weights.values()) or 1.0
    known_w = sum(w for k, w in weights.items() if comps.get(k) is not None)
    value = sum(w * comps[k] for k, w in weights.items() if comps.get(k) is not None) / total_w
    confidence = known_w / total_w
    t, reasons = thresholds, []

    def need(cond, why):
        if not cond:
            reasons.append(why)
    if econ is None:
        reasons.append("Cost or shipping unknown, so the margin can't be worked out.")
    else:
        need(econ["contribution"] >= t["min_contribution"], f"Contribution {econ['contribution']:.2f} is below the minimum {t['min_contribution']:.2f} per order.")
        need((econ["margin_pct"] or 0) >= t["min_margin_pct"], f"Margin {100 * (econ['margin_pct'] or 0):.0f}% is below {100 * t['min_margin_pct']:.0f}%.")
        need(econ["break_even_roas"] is not None and econ["break_even_roas"] <= t["max_break_even_roas"],
             f"Break-even ROAS {econ['break_even_roas']} is above {t['max_break_even_roas']} (paid tests could not pay back).")
        need(econ["price"] <= t["max_price"], f"Proposed price {econ['price']:.2f} is above the {t['max_price']:.2f} limit.")
        need(econ["product_cost"] <= t["max_product_cost"], f"Product cost {econ['product_cost']:.2f} is above {t['max_product_cost']:.2f}.")
        comp = signals.get("competitor_prices") or []
        if comp:
            med = statistics.median(comp)
            need(econ["price"] <= med * t["max_competitor_price_ratio"],
                 f"Proposed price {econ['price']:.2f} is more than {int(round(100 * (t['max_competitor_price_ratio'] - 1)))}% above the competitor median {med:.2f}.")
    days = signals.get("delivery_days_max")
    need(days is not None, "Delivery time unknown.")
    if days is not None:
        need(days <= t["max_delivery_days"], f"Delivery up to {days} days is slower than {t['max_delivery_days']}.")
    images = signals.get("images")
    need(images is not None and images >= t["min_images"], f"Only {images or 0} product images (need {t['min_images']}).")
    if signals.get("stock") is not None:
        need(signals["stock"] >= t["min_stock"], "Supplier shows no stock.")
    need(signals.get("fact_count", 0) >= t["min_facts"], f"Only {signals.get('fact_count', 0)} sourced facts (need {t['min_facts']}).")
    need(confidence >= t["min_confidence"], f"Only {int(confidence * 100)}% of the score is backed by data (need {int(t['min_confidence'] * 100)}%).")
    if signals.get("denied"):
        reasons.append(f"Restricted product type ({signals['denied']}).")
    if signals.get("duplicate_of"):
        reasons.append(f"Too similar to '{signals['duplicate_of']}', already in the store.")
    missing = [k for k, v in comps.items() if v is None]
    explanation = (f"Score {round(100 * value)} = " + " + ".join(
        f"{k} {weights[k]:.2f}x{comps[k]:.2f}" for k in weights if comps.get(k) is not None)
        + (f"; no data for {', '.join(missing)} (scored 0)" if missing else ""))
    return {"score": round(100 * value, 1), "confidence": round(confidence, 2), "components": comps,
            "rejections": reasons, "rejected": bool(reasons), "explanation": explanation}


def rank(items, key="score"):
    """Highest score first; ties broken by confidence, then by id so the order is stable."""
    return sorted(items, key=lambda i: (-(i.get(key) or 0), -(i.get("confidence") or 0), i.get("id") or 0))
