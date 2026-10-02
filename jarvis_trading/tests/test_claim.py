import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import claim as K


class ClaimTests(unittest.TestCase):
    def test_compounding_shows_why_the_claim_is_extraordinary(self):
        t = K.compounding_table((0.03, 0.10))
        self.assertGreater(t[0.03]["after_1_year"], 40_000_000 / 1000 * 1000 * 0.9)  # ~48,000x
        self.assertAlmostEqual(t[0.03]["doubling_time_days"], 23.45, places=1)
        self.assertGreater(t[0.10]["after_1_year"], 1e17)

    def test_frequency_counts_days_and_windows(self):
        daily = [0.04, 0.0, -0.01, 0.05] * 30
        f = K.claim_frequency(daily, 0.03, window=4)
        self.assertEqual(f["days_at_or_above_target"], 60)
        self.assertAlmostEqual(f["fraction_of_days"], 0.5)
        self.assertEqual(f["rolling_4d_windows_averaging_target"], 0)

    def test_leverage_required_and_none_for_non_positive_mean(self):
        self.assertAlmostEqual(K.leverage_required([0.001] * 10, 0.03), 30.0)
        self.assertIsNone(K.leverage_required([-0.001] * 10, 0.03))
        self.assertIsNone(K.leverage_required([], 0.03))

    def test_high_leverage_liquidates_on_a_normal_bad_day(self):
        daily = [0.002, -0.04, 0.003] * 10
        self.assertTrue(K.leveraged_path(daily, 30)["liquidated"])
        self.assertFalse(K.leveraged_path(daily, 1)["liquidated"])

    def test_one_x_leverage_matches_compounding(self):
        res = K.leveraged_path([0.01, 0.01], 1)
        self.assertAlmostEqual(res["final_multiple"], 1.01 ** 2)
        self.assertEqual(res["max_drawdown"], 0.0)

    def test_bootstrap_is_reproducible_and_flags_blowups(self):
        daily = [0.01, -0.03, 0.005, 0.02, -0.02] * 20
        a = K.block_bootstrap_liquidation(daily, 40, n_sims=100, block=5, seed=2)
        b = K.block_bootstrap_liquidation(daily, 40, n_sims=100, block=5, seed=2)
        self.assertEqual(a, b)
        self.assertGreater(a, 0.9)

    def test_assess_and_text(self):
        report = K.assess({"x": [0.001, -0.02, 0.002] * 40, "loser": [-0.001] * 50})
        text = " ".join(K.verdict_text(report))
        self.assertIn("loser: average daily return", text)
        self.assertIn("leverage", text)


if __name__ == "__main__":
    unittest.main()
