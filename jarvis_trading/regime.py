"""
Market-regime tracking: trend, volatility, volume, sideways/ranging, and major regime changes.

Strictly INFORMATIONAL. Regimes are labelled from past data only (a label never changes when later data changes),
they explain WHEN a strategy does well or badly, and they are logged. They are never used to tune, filter or switch
a live strategy: that would be an easy way to overfit history. A strategy may only use regime logic that was part
of its own definition and passed the same research gates (e.g. vol_regime_trend).

Pure functions over daily closes/volumes so everything is testable; `run()` reads the BTC/ETH-led universe candles
(needs pandas, so run it under the Freqtrade venv's Python) and writes trading_lab/reports/regime.json.
"""

import json
import math
import os
import statistics

from . import stats as ST

VOL_LOOKBACK = 30
VOLUME_LOOKBACK = 90
RANGE_LOOKBACK = 30
RANGE_BAND = 0.08        # a 30-day net move inside +/-8% with calm volatility is "sideways"


def _percentile_rank(history, value):
    if not history:
        return 0.5
    return sum(1 for h in history if h <= value) / len(history)


def classify(closes, volumes=None):
    """
    closes: [(epoch, close)], volumes: [(epoch, volume)] or None. Returns [{day, trend, volatility, volume, sideways, label}]
    where every field at day d is computed from data up to and including d-1's information window only.
    """
    trend = ST.classify_regimes(closes)          # {day_number: 'bull'|'bear'|'chop' (+ '_volatile')}
    vol_by_day = dict(volumes or [])
    out, vols_hist, volume_hist = [], [], []
    price = [c for _, c in closes]
    for i, (epoch, close) in enumerate(closes):
        day = int(epoch // 86400)
        label = trend.get(day)
        if label is None or i < VOL_LOOKBACK + 1:
            continue
        window = price[i - VOL_LOOKBACK - 1:i]           # excludes today's close: nothing here sees day i
        rets = [window[j + 1] / window[j] - 1.0 for j in range(len(window) - 1)]
        vol = statistics.pstdev(rets) if len(rets) > 1 else 0.0
        vol_rank = _percentile_rank(vols_hist, vol)
        vols_hist.append(vol)
        volatility = "high" if vol_rank > 0.80 else "low" if vol_rank < 0.30 else "normal"
        vol_class = "normal"
        if vol_by_day:
            v_hist = [vol_by_day.get(int(closes[k][0] // 86400)) for k in range(max(0, i - VOLUME_LOOKBACK), i)]
            v_hist = [v for v in v_hist if v]
            if len(v_hist) >= 30:
                m, sd = statistics.mean(v_hist), statistics.pstdev(v_hist) or 1.0
                last = vol_by_day.get(int(closes[i - 1][0] // 86400))
                if last:
                    z = (last - m) / sd
                    vol_class = "high" if z > 1.0 else "low" if z < -1.0 else "normal"
        net30 = price[i - 1] / price[i - 1 - RANGE_LOOKBACK] - 1.0 if i - 1 - RANGE_LOOKBACK >= 0 else 0.0
        sideways = abs(net30) < RANGE_BAND and volatility != "high"
        base_trend = label.split("_")[0]
        composite = "sideways" if sideways and base_trend == "chop" else base_trend
        out.append({"day": day, "trend": base_trend, "volatility": volatility, "volume": vol_class, "sideways": sideways,
                    "label": f"{composite}/{volatility}-vol/{vol_class}-volume"})
    return out


def detect_changes(history, min_persist=5):
    """
    Regime changes with hysteresis: a new composite label counts only after it has held for `min_persist` days.
    A MAJOR change is a trend flip (bull<->bear) or volatility jumping between low and high.
    Returns [{day, from, to, major}].
    """
    events = []
    current = None
    candidate, run = None, 0
    for row in history:
        label = row["label"]
        if current is None:
            current = row
            continue
        if label == current["label"]:
            candidate, run = None, 0
            continue
        if candidate == label:
            run += 1
        else:
            candidate, run = label, 1
        if run >= min_persist:
            major = {current["trend"], row["trend"]} == {"bull", "bear"} or {current["volatility"], row["volatility"]} == {"low", "high"}
            events.append({"day": row["day"], "from": current["label"], "to": label, "major": major})
            current, candidate, run = row, None, 0
    return events


def current_state(history, min_persist=5):
    if not history:
        return None
    changes = detect_changes(history, min_persist)
    last = history[-1]
    since = changes[-1]["day"] if changes else history[0]["day"]
    return {"label": last["label"], "trend": last["trend"], "volatility": last["volatility"], "volume": last["volume"],
            "sideways": last["sideways"], "since_day": since, "days_in_regime": last["day"] - since,
            "recent_changes": changes[-5:]}


def performance_by_regime(trades, history, starting_balance=1000.0, min_trades=10):
    """Trade performance split by the regime in force when each trade opened. Small groups are omitted, not guessed."""
    from . import metrics as M
    labels = {row["day"]: row["label"] for row in history}
    groups = {}
    for t in trades:
        day = int(M.to_epoch(t.get("open_ts") or t["close_ts"]) // 86400)
        if day in labels:
            groups.setdefault(labels[day], []).append(t)
    return {k: M.compute_metrics(v, starting_balance) for k, v in groups.items() if len(v) >= min_trades}


def run(data_dir=None, out_dir=None):
    import pandas as pd
    from . import freqtrade_adapter as A
    data_dir = data_dir or A.DATA_DIR
    out_dir = out_dir or os.path.join(A.LAB_DIR, "reports")
    df = pd.read_feather(os.path.join(data_dir, "BTC_USDT-1d.feather"))
    closes = [(r.date.timestamp(), float(r.close)) for r in df.itertuples()]
    volumes = [(int(r.date.timestamp() // 86400), float(r.volume)) for r in df.itertuples()]
    history = classify(closes, [(d, v) for d, v in volumes])
    state = current_state(history)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "regime.json"), "w", encoding="utf-8") as f:
        json.dump({"asof_day": history[-1]["day"] if history else None, "state": state, "market": "BTC/USDT daily",
                   "note": "Informational only. Regimes are never used to tune or filter the live strategy."}, f, indent=2)
    return state


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
