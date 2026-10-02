import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

try:
    import numpy as np
    import pandas as pd
    import sklearn  # noqa: F401
    HAVE_ML = True
except Exception:  # the Jarvis interpreter has no pandas/sklearn; these tests run under the Freqtrade venv
    HAVE_ML = False

from jarvis_trading import strategies as S
from jarvis_trading import strategies_ml as SML

if HAVE_ML:
    from jarvis_trading import mlrunner as ML


def synthetic_frames(pairs=("AAA/USDT", "BBB/USDT", "CCC/USDT"), bars=3200, seed=5, freq="4h", start="2020-01-01"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=bars, freq=freq, tz="UTC")
    frames = {}
    for pair in pairs:
        ret = rng.normal(0.0001, 0.01, bars)
        close = 100 * np.exp(np.cumsum(ret))
        open_ = np.concatenate([[100.0], close[:-1]])
        high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, bars)))
        low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, bars)))
        frames[pair] = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                                     "volume": rng.uniform(100, 200, bars)}, index=idx)
    return frames


class MLStrategyDefinitionTests(unittest.TestCase):
    def test_generation_is_deterministic_and_text_only(self):
        a, b = S.generate("ml_walkforward"), S.generate("ml_walkforward")
        self.assertEqual(a, b)
        self.assertTrue(a[0].startswith("ML_"))
        self.assertNotIn("import", a[1])

    def test_leak_option_is_reserved_for_the_control(self):
        with self.assertRaises(ValueError):
            S.generate("ml_walkforward", {"leak": True})
        self.assertTrue(S.generate("ml_leak_control")[2]["leak"])
        self.assertIn("ml_leak_control", S.CONTROL_TEMPLATES)

    def test_invalid_configs_rejected(self):
        for bad in ({"timeframe": "1h"}, {"threshold": 0.3}, {"features": "magic"}, {"horizon": 0}):
            with self.assertRaises(ValueError):
                S.generate("ml_walkforward", bad)

    def test_neighbours_stay_valid_and_sample_is_reproducible(self):
        params = S.generate("ml_walkforward")[2]
        for n in S.neighbours("ml_walkforward", params):
            SML.canonical_params("ml_walkforward", n)
        self.assertEqual(S.sample("ml_walkforward", 6, seed=3), S.sample("ml_walkforward", 6, seed=3))


@unittest.skipUnless(HAVE_ML, "needs pandas + scikit-learn (Freqtrade venv)")
class MLCausalityTests(unittest.TestCase):
    def test_honest_features_are_causal(self):
        frames = synthetic_frames(bars=1200)
        cuts = [frames["AAA/USDT"].index[600], frames["AAA/USDT"].index[900]]
        for kind in ("basic", "extended"):
            self.assertEqual(ML.check_causality(frames, {"features": kind, "leak": False}, cuts), [])

    def test_leaked_feature_is_detected(self):
        frames = synthetic_frames(bars=1200)
        cuts = [frames["AAA/USDT"].index[600]]
        problems = ML.check_causality(frames, {"features": "basic", "leak": True}, cuts)
        self.assertTrue(problems)
        self.assertIn("leak_next", problems[0])


@unittest.skipUnless(HAVE_ML, "needs pandas + scikit-learn (Freqtrade venv)")
class WalkForwardTests(unittest.TestCase):
    params = {"timeframe": "4h", "horizon": 6, "threshold": 0.5, "window_days": 0, "features": "basic", "stoploss": 0.08, "leak": False}

    def test_predictions_before_a_cut_do_not_change_when_the_future_is_corrupted(self):
        frames = synthetic_frames()
        base = ML.walk_forward_predictions(frames, self.params, seed=1)
        self.assertGreater(len(base), 100)
        cut = frames["AAA/USDT"].index[2800]
        corrupted = {}
        rng = np.random.default_rng(99)
        for pair, df in frames.items():
            d = df.copy()
            after = d.index > cut
            for col in ("open", "high", "low", "close"):
                d.loc[after, col] = d.loc[after, col].to_numpy() * rng.uniform(0.2, 5.0, after.sum())
            corrupted[pair] = d
        again = ML.walk_forward_predictions(corrupted, self.params, seed=1)
        a = base[base["ts"] <= cut].sort_values(["ts", "pair"]).reset_index(drop=True)
        b = again[again["ts"] <= cut].sort_values(["ts", "pair"]).reset_index(drop=True)
        self.assertGreater(len(a), 50)
        pd.testing.assert_frame_equal(a, b)

    def test_deterministic_for_a_seed(self):
        frames = synthetic_frames(bars=2600)
        a = ML.walk_forward_predictions(frames, self.params, seed=1)
        b = ML.walk_forward_predictions(frames, self.params, seed=1)
        pd.testing.assert_frame_equal(a, b)

    def test_no_predictions_before_the_minimum_training_history(self):
        frames = synthetic_frames()
        preds = ML.walk_forward_predictions(frames, self.params, seed=1)
        first_allowed = frames["AAA/USDT"].index[SML.MIN_TRAIN_BARS["4h"]]
        self.assertGreaterEqual(preds["ts"].min(), first_allowed)


@unittest.skipUnless(HAVE_ML, "needs pandas + scikit-learn (Freqtrade venv)")
class SimulationTests(unittest.TestCase):
    def flat_frames(self, bars=60):
        idx = pd.date_range("2022-01-01", periods=bars, freq="4h", tz="UTC")
        df = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0}, index=idx)
        return {"AAA/USDT": df, "BBB/USDT": df.copy()}

    def params(self, **kw):
        base = {"timeframe": "4h", "horizon": 5, "threshold": 0.6, "stoploss": 0.08}
        base.update(kw)
        return base

    def test_flat_market_loses_exactly_round_trip_costs_and_enters_next_bar(self):
        frames = self.flat_frames()
        preds = pd.DataFrame({"ts": [frames["AAA/USDT"].index[10]], "pair": ["AAA/USDT"], "prob": [0.9]})
        trades = ML.simulate(frames, preds, self.params())
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["open_ts"], frames["AAA/USDT"].index[11].timestamp())  # next bar, not the signal bar
        self.assertAlmostEqual(trades[0]["profit_ratio"], (1 - ML.COST_PER_SIDE) / (1 + ML.COST_PER_SIDE) - 1, places=9)
        self.assertLess(trades[0]["profit_ratio"], 0)

    def test_below_threshold_is_ignored(self):
        frames = self.flat_frames()
        preds = pd.DataFrame({"ts": [frames["AAA/USDT"].index[10]], "pair": ["AAA/USDT"], "prob": [0.55]})
        self.assertEqual(ML.simulate(frames, preds, self.params()), [])

    def test_stoploss_exits_at_the_stop_price(self):
        frames = self.flat_frames()
        df = frames["AAA/USDT"].copy()
        df.iloc[13, df.columns.get_loc("low")] = 80.0
        frames["AAA/USDT"] = df
        preds = pd.DataFrame({"ts": [df.index[10]], "pair": ["AAA/USDT"], "prob": [0.9]})
        trade = ML.simulate(frames, preds, self.params())[0]
        self.assertEqual(trade["exit_reason"], "stoploss")
        self.assertAlmostEqual(trade["profit_ratio"], 0.92 * (1 - ML.COST_PER_SIDE) / (1 + ML.COST_PER_SIDE) - 1, places=9)

    def test_one_position_per_pair_and_global_cap(self):
        frames = self.flat_frames()
        idx = frames["AAA/USDT"].index
        preds = pd.DataFrame({"ts": [idx[10], idx[11]], "pair": ["AAA/USDT", "AAA/USDT"], "prob": [0.9, 0.9]})
        self.assertEqual(len(ML.simulate(frames, preds, self.params())), 1)  # second signal ignored while in the trade

    def test_timerange_filter_uses_entry_time(self):
        frames = self.flat_frames()
        idx = frames["AAA/USDT"].index
        preds = pd.DataFrame({"ts": [idx[10]], "pair": ["AAA/USDT"], "prob": [0.9]})
        self.assertEqual(ML.simulate(frames, preds, self.params(), start_epoch=idx[30].timestamp()), [])


@unittest.skipUnless(HAVE_ML, "needs pandas + scikit-learn (Freqtrade venv)")
class MLRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        frames = synthetic_frames(bars=1500, start="2022-11-01")
        for pair, df in frames.items():
            out = df.reset_index().rename(columns={"index": "date"})
            out.to_feather(os.path.join(self.tmp.name, f"{pair.replace('/', '_')}-4h.feather"))
        self.pairs = list(frames)
        self.runner = ML.MLRunner(data_dir=self.tmp.name, cache_dir=os.path.join(self.tmp.name, "cache"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_leakage_check_clears_honest_and_catches_the_control(self):
        for family, expected in (("ml_walkforward", True), ("ml_leak_control", False)):
            name, source, params = S.generate(family)
            self.runner.write_strategy(name, source)
            result = self.runner.run_lookahead_analysis(name, "20230101-20230228", "4h", pairs=self.pairs)
            self.assertIs(result["clean"], expected, family)


if __name__ == "__main__":
    unittest.main()
