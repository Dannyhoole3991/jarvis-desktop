"""
Passive benchmark: what simply holding the universe did over the same windows the
strategies were judged on. All strategies here are long-only, so any of them can
look good just because the market rose. A strategy whose out-of-sample Sharpe is
not clearly above this benchmark's has shown market exposure, not an edge.

The math functions are pure and tested; `run()` reads candle files (needs pandas,
so run it under the Freqtrade venv's Python) and writes
trading_lab/reports/benchmark.json for the dashboard.

    trading_lab\\ft_venv\\Scripts\\python.exe -m jarvis_trading.benchmark
"""

import json
import math
import os

from . import metrics as M

WINDOWS = {"training": ("2019-01-01", "2022-12-31"), "validation": ("2023-01-01", "2024-06-30"), "test": ("2024-07-01", "2026-09-30")}


def equal_weight_daily_returns(per_pair_returns):
    """per_pair_returns: {pair: {day_number: return}}. Daily-rebalanced equal weight over the pairs that exist that day."""
    days = sorted({d for series in per_pair_returns.values() for d in series})
    out = []
    for d in days:
        vals = [series[d] for series in per_pair_returns.values() if d in series]
        out.append(sum(vals) / len(vals))
    return out


def summarise(daily):
    if len(daily) < 2:
        return {"days": len(daily), "sharpe": None, "total_return": None, "max_drawdown": None}
    equity, peak, worst = 1.0, 1.0, 0.0
    for r in daily:
        equity *= 1 + r
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return {"days": len(daily), "sharpe": M.sharpe(daily), "total_return": equity - 1.0, "max_drawdown": worst,
            "annualised_return": equity ** (M.DAYS_PER_YEAR / len(daily)) - 1.0 if equity > 0 else -1.0}


def run(data_dir=None, pairs=None, out_dir=None):
    import pandas as pd
    from . import freqtrade_adapter as A
    from . import universe as U
    data_dir = data_dir or A.DATA_DIR
    pairs = pairs or U.UNIVERSE
    out_dir = out_dir or os.path.join(A.LAB_DIR, "reports")
    per_pair = {}
    for pair in pairs:
        df = pd.read_feather(os.path.join(data_dir, f"{pair.replace('/', '_')}-1d.feather"))
        df["date"] = pd.to_datetime(df["date"], utc=True)
        ret = df["close"].pct_change().dropna()
        per_pair[pair] = {int(ts.timestamp() // 86400): float(r) for ts, r in zip(df["date"].iloc[1:], ret)}
    result = {}
    for name, (a, b) in WINDOWS.items():
        lo, hi = int(pd.Timestamp(a, tz="UTC").timestamp() // 86400), int(pd.Timestamp(b, tz="UTC").timestamp() // 86400)
        window = {p: {d: r for d, r in s.items() if lo <= d <= hi} for p, s in per_pair.items()}
        result[name] = {"universe_equal_weight": summarise(equal_weight_daily_returns(window)),
                        "btc": summarise([r for d, r in sorted(window["BTC/USDT"].items())])}
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "benchmark.json"), "w", encoding="utf-8") as f:
        json.dump({"windows": WINDOWS, "results": result,
                   "note": "Buy-and-hold, gross of costs. Strategies are long-only, so compare their Sharpe with this, not with zero."}, f, indent=2)
    return result


if __name__ == "__main__":
    for window, res in run().items():
        print(window, {k: {kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()} for k, v in res.items()})
