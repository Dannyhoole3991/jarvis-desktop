"""
Statistical robustness tools: Monte Carlo over trade lists, a sign-flip
significance test, a "what would luck alone produce" benchmark, and
market-regime classification. Standard library only; every random function
takes an explicit seed so any result can be reproduced exactly.
"""

import math
import random
import statistics

from . import metrics as M


def _percentile(sorted_values, q):
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def monte_carlo(trade_ratios, stake_fraction=0.10, n_sims=5000, seed=1, ruin_drawdown=0.5):
    """
    Bootstrap the trade list: resample with replacement, apply each trade to
    `stake_fraction` of equity with compounding, and look at the spread.
    `ruin` means the equity path fell by `ruin_drawdown` (default 50%) at any point.
    """
    if not trade_ratios:
        return None
    rng = random.Random(seed)
    n = len(trade_ratios)
    finals, drawdowns = [], []
    ruined = lost = 0
    for _ in range(n_sims):
        equity, peak, worst = 1.0, 1.0, 0.0
        for _ in range(n):
            equity *= 1.0 + stake_fraction * trade_ratios[rng.randrange(n)]
            peak = max(peak, equity)
            worst = max(worst, (peak - equity) / peak)
        finals.append(equity - 1.0)
        drawdowns.append(worst)
        ruined += worst >= ruin_drawdown
        lost += equity < 1.0
    finals.sort()
    drawdowns.sort()
    return {
        "n_sims": n_sims, "seed": seed, "stake_fraction": stake_fraction,
        "final_return_p5": _percentile(finals, 0.05), "final_return_p50": _percentile(finals, 0.50),
        "final_return_p95": _percentile(finals, 0.95),
        "max_drawdown_p50": _percentile(drawdowns, 0.50), "max_drawdown_p95": _percentile(drawdowns, 0.95),
        "ruin_probability": ruined / n_sims, "probability_of_loss": lost / n_sims,
    }


def sign_flip_p_value(trade_ratios, n_perm=10000, seed=1):
    """
    One-sided test of 'the true mean trade return is not above zero'.
    Under that null each trade's sign is arbitrary, so randomly flipping
    signs gives the distribution of the mean produced by luck alone.
    """
    n = len(trade_ratios)
    if n < 2:
        return None
    observed = sum(trade_ratios) / n
    rng = random.Random(seed)
    magnitudes = [abs(r) for r in trade_ratios]
    at_least = 0
    for _ in range(n_perm):
        total = 0.0
        for mag in magnitudes:
            total += mag if rng.random() < 0.5 else -mag
        if total / n >= observed:
            at_least += 1
    return (at_least + 1) / (n_perm + 1)


def noise_sharpe_benchmark(n_strategies, n_days):
    """
    Expected best annualised Sharpe among `n_strategies` skill-less strategies
    evaluated over `n_days` daily returns (Bailey & Lopez de Prado's
    expected-maximum approximation). A discovered Sharpe near this number is
    what trying many ideas produces by chance alone.
    """
    if n_strategies < 2 or n_days < 2:
        return 0.0
    nd = statistics.NormalDist()
    euler = 0.5772156649
    z = (1 - euler) * nd.inv_cdf(1 - 1.0 / n_strategies) + euler * nd.inv_cdf(1 - 1.0 / (n_strategies * math.e))
    return z / math.sqrt(n_days) * math.sqrt(M.DAYS_PER_YEAR)


def classify_regimes(daily_closes, lookback=90, trend_threshold=0.15, high_vol_quantile=0.67):
    """
    Labels each day from daily closes [(epoch, close)] using only data BEFORE
    that day: 'bull' / 'bear' / 'chop' by trailing return, suffixed '_volatile'
    when trailing volatility is in the top third of its own history so far.
    Returns {day_number: label}.
    """
    labels = {}
    closes = [c for _, c in daily_closes]
    days = [int(e // 86400) for e, _ in daily_closes]
    vols = []
    for i in range(len(closes)):
        if i < lookback:
            continue
        window = closes[i - lookback:i]
        ret = window[-1] / window[0] - 1.0
        rets = [window[j + 1] / window[j] - 1.0 for j in range(len(window) - 1)]
        vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
        trend = "bull" if ret > trend_threshold else "bear" if ret < -trend_threshold else "chop"
        threshold = _percentile(sorted(vols), high_vol_quantile) if len(vols) >= 30 else None
        label = trend + ("_volatile" if threshold is not None and vol > threshold else "")
        vols.append(vol)
        labels[days[i]] = label
    return labels


def metrics_by_regime(trades, regime_labels, starting_balance=1000.0, min_trades=5):
    """Splits trades by the regime in force on the day each opened and measures each group."""
    groups = {}
    for trade in trades:
        day = int(M.to_epoch(trade.get("open_ts") or trade["close_ts"]) // 86400)
        label = regime_labels.get(day)
        if label is None:
            continue
        base = label.split("_")[0]  # report bull/bear/chop; volatility is kept for finer analysis
        groups.setdefault(base, []).append(trade)
    return {name: M.compute_metrics(ts, starting_balance)
            for name, ts in groups.items() if len(ts) >= min_trades}
