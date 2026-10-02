"""
Command-line entry point for the research lab. Runs under the Freqtrade venv's
Python (it needs pandas to read candle files); the Jarvis engine launches it as
a subprocess:

    trading_lab\\ft_venv\\Scripts\\python.exe -m jarvis_trading.run_lab --templates sma_cross rsi_reversion

Research and backtesting only. Nothing here can place an order.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_trading import freqtrade_adapter as A  # noqa: E402
from jarvis_trading import lab as L  # noqa: E402
from jarvis_trading import registry as R  # noqa: E402
from jarvis_trading import strategies as S  # noqa: E402


def daily_closes_btc():
    import pandas as pd
    df = pd.read_feather(os.path.join(A.DATA_DIR, "BTC_USDT-1d.feather"))
    return [(row.date.timestamp(), float(row.close)) for row in df.itertuples()]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--templates", nargs="+", default=[t for t in S.TEMPLATES])
    ap.add_argument("--params", default="{}", help="JSON overrides applied to every template (rarely used)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    lab = L.Lab(R.Registry(), A, daily_closes=daily_closes_btc())
    overrides = json.loads(args.params)
    results = []
    for template in args.templates:
        r = lab.research(template, overrides or None)
        results.append({k: r[k] for k in ("strategy_id", "version", "status", "verdict")} | {"template": template})
        if not args.json:
            print(f"{template:22s} {r['strategy_id']} v{r['version']}  {r['status'].upper():10s} {r['verdict']}", flush=True)
    if args.json:
        print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
