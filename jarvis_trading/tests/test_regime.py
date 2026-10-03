import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import regime as RG

DAY = 86400


def series(n=500, drift=0.003, vol=0.01, seed=1, start=18000):
    rng = random.Random(seed)
    price, closes, volumes = 100.0, [], []
    for i in range(n):
        price *= 1 + rng.gauss(drift, vol)
        closes.append(((start + i) * DAY, price))
        volumes.append((start + i, 1000 + rng.gauss(0, 100)))
    return closes, volumes


class RegimeTests(unittest.TestCase):
    def test_labels_cover_trend_volatility_volume_and_sideways(self):
        closes, volumes = series()
        h = RG.classify(closes, volumes)
        self.assertTrue(h)
        for row in h[:5]:
            for key in ("trend", "volatility", "volume", "sideways", "label"):
                self.assertIn(key, row)

    def test_uptrend_is_bull_and_flat_noise_is_sideways(self):
        up = RG.classify(*series(drift=0.006, vol=0.005))
        self.assertGreater(sum(r["trend"] == "bull" for r in up) / len(up), 0.8)
        flat = RG.classify(*series(drift=0.0, vol=0.004, seed=3))
        self.assertGreater(sum(r["sideways"] for r in flat) / len(flat), 0.5)

    def test_labels_never_change_when_future_data_changes(self):
        closes, volumes = series()
        base = {r["day"]: r["label"] for r in RG.classify(closes, volumes)}
        cut = 350
        altered = closes[:cut] + [(e, c * random.Random(i).uniform(0.2, 5)) for i, (e, c) in enumerate(closes[cut:])]
        alt_vol = volumes[:cut] + [(d, v * 9) for d, v in volumes[cut:]]
        changed = {r["day"]: r["label"] for r in RG.classify(altered, alt_vol)}
        cut_day = closes[cut][0] // DAY
        for day, label in base.items():
            if day <= cut_day:
                self.assertEqual(changed.get(day), label, day)

    def test_volume_spike_is_classified_high(self):
        closes, volumes = series()
        volumes = volumes[:300] + [(d, v * 10) for d, v in volumes[300:305]] + volumes[305:]
        h = {r["day"]: r for r in RG.classify(closes, volumes)}
        spike_day = closes[301][0] // DAY + 1
        self.assertEqual(h[spike_day]["volume"], "high")

    def test_change_detection_needs_persistence(self):
        mk = lambda day, label, trend="bull", vol="normal": {"day": day, "label": label, "trend": trend, "volatility": vol}
        hist = [mk(i, "A") for i in range(10)] + [mk(10 + i, "B") if i % 2 else mk(10 + i, "A") for i in range(6)]  # flickering
        self.assertEqual(RG.detect_changes(hist, min_persist=3), [])
        hist = [mk(i, "A") for i in range(10)] + [mk(10 + i, "B", trend="bear") for i in range(6)]
        events = RG.detect_changes(hist, min_persist=3)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["major"])          # bull -> bear
        self.assertEqual(events[0]["to"], "B")

    def test_current_state_reports_duration(self):
        closes, volumes = series(n=400)
        state = RG.current_state(RG.classify(closes, volumes))
        self.assertIn("days_in_regime", state)
        self.assertGreaterEqual(state["days_in_regime"], 0)

    def test_performance_by_regime_omits_small_groups(self):
        closes, volumes = series()
        h = RG.classify(closes, volumes)
        trades = [{"open_ts": (h[i]["day"]) * DAY + 100, "close_ts": h[i]["day"] * DAY + 3600, "profit_ratio": 0.01, "profit_abs": 1.0, "stake": 100.0}
                  for i in range(0, 120, 2)]
        out = RG.performance_by_regime(trades, h, min_trades=10)
        self.assertTrue(all(m["n_trades"] >= 10 for m in out.values()))
        self.assertEqual(RG.performance_by_regime(trades[:3], h, min_trades=10), {})


class RegimeRunTests(unittest.TestCase):
    def rows(self, n=400):
        closes, volumes = series(n=n)
        return [[c[0] * 1000, 0, 0, 0, c[1], v[1]] for c, v in zip(closes, volumes)]

    def test_run_uses_fresh_exchange_candles_and_says_so(self):
        import json
        import tempfile
        rows = self.rows()
        with tempfile.TemporaryDirectory() as tmp:
            state = RG.run(out_dir=tmp, fetcher=lambda pair, since: rows)
            data = json.load(open(os.path.join(tmp, "regime.json")))
        self.assertEqual(data["data_source"], "exchange (live)")
        self.assertEqual(data["asof_day"], int(rows[-1][0] // 1000 // DAY))
        self.assertIn("label", state)

    def test_unavailable_or_short_data_returns_none_so_the_caller_can_fall_back(self):
        def offline(pair, since):
            raise RuntimeError("offline")
        self.assertIsNone(RG.fetch_btc_daily(offline))
        self.assertIsNone(RG.fetch_btc_daily(lambda pair, since: self.rows(50)))


if __name__ == "__main__":
    unittest.main()
