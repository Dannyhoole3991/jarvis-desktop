"""
What does the exchange's real minimum order size mean for a small account?

Uses PUBLIC market data only (no key). A £10 account is about 13 USDT; an exchange minimum order is typically
5-10 USDT, so a tiny account can hold at most one or two minimum-size positions, and per-position limits sized as
a small percentage of capital (as in research) cannot work. This report computes it from live exchange data.
"""

from . import exchange as X
from . import limits as L

FEE_BUFFER = 1.05   # stake must clear the minimum even after rounding / price movement


def _fx_to_usdt(client, currency):
    currency = currency.upper()
    if currency in ("USD", "USDT"):
        return 1.0, "1:1"
    for pair, invert in ((f"{currency}/USDT", False), (f"USDT/{currency}", True)):
        try:
            last = client.fetch_ticker(pair)["last"]
            return (1.0 / last if invert else last), pair
        except X.ExchangeError:
            continue
    return None, None


def report(capital, currency="GBP", client=None, limits=None):
    client = client or X.CcxtExchange("binance")
    capital = float(capital)
    rate, source = _fx_to_usdt(client, currency)
    capital_usdt = capital * rate if rate else None
    limits = limits or L.LiveLimits()
    rows = []
    for pair in limits.allowed_pairs:
        try:
            m = client.market_limits(pair)
        except Exception as error:
            rows.append({"pair": pair, "error": str(error)[:100]})
            continue
        min_stake = max(m["min_cost"], 0.0) * FEE_BUFFER
        row = {"pair": pair, "exchange_min_order_usdt": m["min_cost"], "min_stake_with_buffer": round(min_stake, 2), "taker_fee": m["taker_fee"]}
        if capital_usdt:
            row["max_positions_at_minimum"] = int(capital_usdt * limits.max_total_exposure_pct // min_stake) if min_stake else None
            row["position_pct_needed"] = round(min_stake / capital_usdt, 3)
            row["tradable_under_limits"] = min_stake <= capital_usdt * limits.max_position_pct
        rows.append(row)
    worst = max((r.get("min_stake_with_buffer", 0) for r in rows), default=0)
    out = {"capital": capital, "currency": currency, "fx_rate_to_usdt": rate, "fx_source": source, "capital_usdt": capital_usdt,
           "pairs": rows,
           "smallest_practical_capital_usdt": round(worst / limits.max_position_pct, 2) if worst else None,
           "notes": ["Fees are a larger share on tiny orders; the lab's research assumed 0.10% fee + 0.05% slippage per side.",
                     "Stop-loss orders held on the exchange must also meet the exchange's minimum order size.",
                     "Whether you can use this exchange (availability/regulation for your country) is your decision; check it before funding."]}
    if capital_usdt:
        feasible = [r["pair"] for r in rows if r.get("tradable_under_limits")]
        out["verdict"] = (f"{capital:g} {currency} is about {capital_usdt:.2f} USDT. Pairs a minimum-size position fits within the "
                          f"{limits.max_position_pct:.0%} per-position limit: {', '.join(feasible) if feasible else 'none'}.")
    return out
