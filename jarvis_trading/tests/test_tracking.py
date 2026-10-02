import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import lab as L
from jarvis_trading import registry as R
from jarvis_trading import tracking as T
from jarvis_trading.tests.test_lab import FakeRunner, synthetic_closes


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = R.Registry(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_lab(self):
        good = L.Lab(self.reg, FakeRunner(mean=0.008, sd=0.03, trades_per_period=200), daily_closes=synthetic_closes(), mc_sims=100)
        good.research("sma_cross", {"fast": 10})                     # candidate
        bad = L.Lab(self.reg, FakeRunner(mean=-0.01, trades_per_period=100), daily_closes=synthetic_closes(), mc_sims=100)
        bad.research("rsi_reversion")                                # rejected in training
        leak = L.Lab(self.reg, FakeRunner(lookahead=False), daily_closes=synthetic_closes(), mc_sims=100)
        leak.research("lookahead_control")                           # rejected for leakage

    def test_funnel_is_monotonic_and_counts_correctly(self):
        self.run_lab()
        t = T.build(self.reg)
        counts = [s["count"] for s in t["funnel"]]
        self.assertEqual(counts[0], 3)
        self.assertEqual(counts, sorted(counts, reverse=True))
        stage = {s["stage"]: s["count"] for s in t["funnel"]}
        self.assertEqual(stage["candidate"], 1)
        self.assertEqual(stage["paper"], 0)
        self.assertEqual(stage["leakage_clean"], 2)

    def test_rejection_reasons_are_counted(self):
        self.run_lab()
        reasons = dict(T.build(self.reg)["rejection_reasons"])
        self.assertIn("LOOKAHEAD_BIAS", reasons)
        self.assertIn("LOSING_IN_TRAINING", reasons)

    def test_leaderboard_ranks_by_luck_adjusted_risk_score_not_profit(self):
        self.run_lab()
        t = T.build(self.reg)
        self.assertTrue(t["leaderboard"])
        scores = [e["score"] for e in t["leaderboard"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIn("not by raw profit", t["ranking_note"])

    def test_scorecard_shows_dev_oos_stability_and_confidence(self):
        self.run_lab()
        row = next(r for r in T.build(self.reg)["rows"] if r["status"] == "candidate")
        for key in ("development", "out_of_sample", "stability", "confidence"):
            self.assertIsNotNone(row[key], key)
        self.assertIn("max_drawdown", row["out_of_sample"])
        self.assertIn("fees_pct_of_gross_profit", row["out_of_sample"])
        self.assertLess(row["confidence"]["p_value"], row["confidence"]["required_p"])

    def test_empty_registry(self):
        t = T.build(self.reg)
        self.assertEqual(t["n_versions"], 0)
        self.assertEqual(t["leaderboard"], [])


if __name__ == "__main__":
    unittest.main()
