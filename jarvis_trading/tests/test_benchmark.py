import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import benchmark as B


class BenchmarkMathTests(unittest.TestCase):
    def test_equal_weight_averages_pairs_present_each_day(self):
        out = B.equal_weight_daily_returns({"A": {1: 0.10, 2: 0.0}, "B": {1: 0.0, 2: 0.04, 3: 0.02}})
        self.assertEqual(out, [0.05, 0.02, 0.02])

    def test_summary_matches_compounding_and_drawdown(self):
        s = B.summarise([0.10, -0.10, 0.10])
        self.assertAlmostEqual(s["total_return"], 1.1 * 0.9 * 1.1 - 1)
        self.assertAlmostEqual(s["max_drawdown"], 0.10)

    def test_too_little_data(self):
        self.assertIsNone(B.summarise([0.01])["sharpe"])


if __name__ == "__main__":
    unittest.main()
