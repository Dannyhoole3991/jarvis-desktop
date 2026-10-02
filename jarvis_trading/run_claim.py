"""
Runs the "3-10% per day" claim experiments against real data and the strategies
the lab has already tested. Run under the Freqtrade venv's Python:

    trading_lab\\ft_venv\\Scripts\\python.exe -m jarvis_trading.run_claim
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_trading import claim as K  # noqa: E402
from jarvis_trading import freqtrade_adapter as A  # noqa: E402
from jarvis_trading import lab as L  # noqa: E402
from jarvis_trading import registry as R  # noqa: E402
from jarvis_trading import strategies as S  # noqa: E402

REPORT_DIR = os.path.join(A.LAB_DIR, "reports")
FULL_RANGE = "20190101-20260930"


def closes(pair_file):
    import pandas as pd
    df = pd.read_feather(os.path.join(A.DATA_DIR, pair_file))
    return [(row.date.timestamp(), float(row.close)) for row in df.itertuples()]


def main():
    os.makedirs(REPORT_DIR, exist_ok=True)
    reg = R.Registry()
    streams = {
        "BTC buy-and-hold": K.daily_returns_from_closes(closes("BTC_USDT-1d.feather")),
        "ETH buy-and-hold": K.daily_returns_from_closes(closes("ETH_USDT-1d.feather")),
    }
    a, b = L._range_epochs(FULL_RANGE)
    for v in reg.list_versions():
        if v["strategy_id"] and "template:lookahead_control" in (reg.get(v["strategy_id"])["source"] or ""):
            continue  # the cheating control would make the numbers meaningless
        template = reg.get(v["strategy_id"])["source"].split(":", 1)[1]
        version = reg.get_version(v["strategy_id"], v["version"])
        name, source, params = S.generate(template, version["parameters"])
        A.write_strategy(name, source)
        res = A.run_backtest(name, FULL_RANGE, params["timeframe"])
        streams[f"{v['strategy_id']} {template}"] = K.strategy_daily_returns(res["trades"], L.STARTING_BALANCE, a, b)

    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "claim": "3-10% per day, sustained",
        "compounding": K.compounding_table(),
        "per_stream": {t: K.assess(streams, t) for t in (0.03, 0.10)},
        "fees_and_slippage_per_side": A.EXCHANGE_FEE + A.SLIPPAGE_ASSUMPTION,
        "note": "Strategy streams are net of fees and slippage; buy-and-hold streams are gross (no costs) and unleveraged.",
    }
    for target in (0.03, 0.10):
        report[f"summary_{int(target * 100)}pct"] = K.verdict_text(report["per_stream"][target], target)
    with open(os.path.join(REPORT_DIR, "claim_experiment.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    reg.log_decision("claim_experiment", "3-10%/day claim experiment run; see trading_lab/reports/claim_experiment.json. "
                     + " | ".join(report["summary_3pct"][:3]))
    for key in ("summary_3pct", "summary_10pct"):
        print(key)
        for line in report[key]:
            print("  " + line)
    print("compounding:", {k: {kk: round(vv, 1) for kk, vv in v.items()} for k, v in report["compounding"].items()})


if __name__ == "__main__":
    main()
