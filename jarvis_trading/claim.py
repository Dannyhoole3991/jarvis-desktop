"""
Experiments on the claim "an automated system can make 3-10% per day".

The lab does not assume the claim is true or false; it measures what the claim
would require and what the data says. Pure functions over daily return series,
standard library only, so every number is reproducible.

Questions answered:
  1. What does the claim imply arithmetically (compounding)?
  2. How often did the market itself ever deliver such a day / such a month?
  3. What leverage would a strategy need to average the claimed return,
     and what happens to that leveraged path (drawdown, liquidation)?
  4. Do the strategies the lab actually tested come anywhere near it
     (after fees and slippage, out of sample)?
"""

import math
import random

from . import metrics as M


def compounding_table(daily_rates=(0.03, 0.05, 0.10), start=1000.0):
    """What starting capital would become if the claimed daily rate held."""
    out = {}
    for r in daily_rates:
        out[r] = {"after_30_days": start * (1 + r) ** 30, "after_1_year": start * (1 + r) ** 365,
                  "doubling_time_days": math.log(2) / math.log(1 + r)}
    return out


def daily_returns_from_closes(closes):
    return [closes[i][1] / closes[i - 1][1] - 1.0 for i in range(1, len(closes))]


def claim_frequency(daily, target=0.03, window=30):
    """How often did a day (or a rolling window's average day) reach the claimed rate?"""
    n = len(daily)
    if n == 0:
        return {"days": 0}
    days_hit = sum(1 for r in daily if r >= target)
    windows = 0
    hit = 0
    for i in range(0, n - window + 1):
        windows += 1
        hit += (sum(daily[i:i + window]) / window) >= target
    return {"days": n, "target": target, "days_at_or_above_target": days_hit, "fraction_of_days": days_hit / n,
            f"rolling_{window}d_windows": windows, f"rolling_{window}d_windows_averaging_target": hit,
            "best_day": max(daily), "mean_daily": sum(daily) / n}


def leverage_required(daily, target=0.03):
    """Leverage needed for the average daily return to equal `target` (None if the stream has no positive mean)."""
    if not daily:
        return None
    mean = sum(daily) / len(daily)
    return target / mean if mean > 0 else None


def leveraged_path(daily, leverage, maintenance_loss=1.0):
    """
    Applies `leverage` to a daily return stream. A day whose levered loss
    reaches `maintenance_loss` of equity liquidates the account (equity -> 0).
    Returns final multiple, max drawdown and whether liquidation happened.
    """
    equity, peak, worst = 1.0, 1.0, 0.0
    for r in daily:
        levered = r * leverage
        if levered <= -maintenance_loss:
            return {"final_multiple": 0.0, "max_drawdown": 1.0, "liquidated": True}
        equity *= 1 + levered
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return {"final_multiple": equity, "max_drawdown": worst, "liquidated": False}


def block_bootstrap_liquidation(daily, leverage, n_sims=2000, block=10, horizon=365, seed=1):
    """Share of resampled one-year paths liquidated (or drawn down 90%+) at the given leverage."""
    if len(daily) < block * 2:
        return None
    rng = random.Random(seed)
    bad = 0
    for _ in range(n_sims):
        path = []
        while len(path) < horizon:
            s = rng.randrange(0, len(daily) - block)
            path.extend(daily[s:s + block])
        res = leveraged_path(path[:horizon], leverage)
        bad += res["liquidated"] or res["max_drawdown"] >= 0.9
    return bad / n_sims


def strategy_daily_returns(trades, starting_balance, start_epoch, end_epoch):
    curve = M.equity_curve(trades, starting_balance)
    return M.daily_returns(curve, starting_balance, start_epoch, end_epoch)


def assess(named_streams, target=0.03):
    """
    named_streams: {name: daily return list}. For each: how close it gets to the
    claim unleveraged, the leverage the claim would need, and what that does.
    """
    report = {}
    for name, daily in named_streams.items():
        freq = claim_frequency(daily, target)
        lev = leverage_required(daily, target)
        entry = {"frequency": freq, "mean_daily": freq.get("mean_daily"), "leverage_required": lev}
        if lev is not None:
            entry["at_required_leverage"] = leveraged_path(daily, lev)
            entry["bootstrap_blowup_probability"] = block_bootstrap_liquidation(daily, lev)
        report[name] = entry
    return report


def verdict_text(report, target=0.03):
    lines = []
    for name, e in report.items():
        mean = e["mean_daily"] or 0.0
        lev = e["leverage_required"]
        if lev is None:
            lines.append(f"{name}: average daily return {mean:.3%} is not positive, so no leverage reaches {target:.0%}/day.")
            continue
        blow = e.get("bootstrap_blowup_probability")
        liq = e["at_required_leverage"]["liquidated"]
        lines.append(
            f"{name}: averages {mean:.3%}/day unleveraged ({e['frequency']['fraction_of_days']:.1%} of days at or above {target:.0%}). "
            f"Reaching {target:.0%}/day would take about {lev:.0f}x leverage; on the historical path that "
            f"{'LIQUIDATES the account' if liq else 'draws down ' + format(e['at_required_leverage']['max_drawdown'], '.0%')}"
            + (f", and {blow:.0%} of resampled years blow up." if blow is not None else "."))
    return lines
