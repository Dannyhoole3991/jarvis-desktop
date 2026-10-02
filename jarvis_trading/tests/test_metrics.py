import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import metrics as m

DAY = 86400


def trade(day, ratio, stake=100.0, fee=0.0):
    return {"open_ts": day * DAY, "close_ts": day * DAY + 3600, "profit_ratio": ratio,
            "stake": stake, "profit_abs": stake * ratio, "fee_paid": fee}


class MetricsTests(unittest.TestCase):
    def test_empty(self):
        r = m.compute_metrics([], 1000)
        self.assertEqual(r["n_trades"], 0)
        self.assertEqual(r["total_return"], 0.0)

    def test_basic_two_trades(self):
        r = m.compute_metrics([trade(1, 0.10), trade(2, -0.05)], 1000)
        self.assertAlmostEqual(r["total_return"], 0.005)
        self.assertAlmostEqual(r["win_rate"], 0.5)
        self.assertAlmostEqual(r["profit_factor"], 2.0)
        self.assertAlmostEqual(r["max_drawdown"], 5 / 1010)
        self.assertAlmostEqual(r["avg_trade"], 0.025)

    def test_max_drawdown_exact(self):
        curve = [(1, 1200.0), (2, 900.0), (3, 1000.0)]
        self.assertAlmostEqual(m.max_drawdown(curve, 1000), 0.25)

    def test_drawdown_from_start_balance(self):
        self.assertAlmostEqual(m.max_drawdown([(1, 800.0)], 1000), 0.20)

    def test_losing_streak(self):
        trades = [trade(1, -0.01), trade(2, -0.02), trade(3, 0.03), trade(4, -0.01)]
        self.assertEqual(m.longest_losing_streak(trades), 2)

    def test_constant_returns_sharpe_none(self):
        self.assertIsNone(m.sharpe([0.01, 0.01, 0.01]))

    def test_sortino_none_without_downside(self):
        self.assertIsNone(m.sortino([0.01, 0.02, 0.0]))

    def test_sharpe_known_value(self):
        daily = [0.01, -0.01, 0.02, 0.0]
        mean = sum(daily) / 4
        sd = math.sqrt(sum((d - mean) ** 2 for d in daily) / 3)
        self.assertAlmostEqual(m.sharpe(daily), mean / sd * math.sqrt(365))

    def test_quiet_days_count_as_zero_returns(self):
        curve = m.equity_curve([trade(1, 0.10), trade(10, 0.10)], 1000)
        daily = m.daily_returns(curve, 1000, 1 * DAY, 10 * DAY + 3600)
        self.assertEqual(len(daily), 10)
        self.assertEqual(sum(1 for d in daily if d == 0.0), 8)

    def test_iso_timestamps(self):
        self.assertEqual(m.to_epoch("1970-01-02T00:00:00Z"), 86400.0)
        self.assertEqual(m.to_epoch("1970-01-02T00:00:00"), 86400.0)

    def test_fees_share_of_profit(self):
        r = m.compute_metrics([trade(1, 0.10, fee=2.0), trade(2, 0.10, fee=2.0)], 1000)
        self.assertAlmostEqual(r["fees_total"], 4.0)
        self.assertAlmostEqual(r["fees_pct_of_gross_profit"], 4.0 / 20.0)

    def test_turnover(self):
        r = m.compute_metrics([trade(1, 0.01), trade(2, 0.01)], 1000)
        self.assertAlmostEqual(r["turnover"], (200.0 * 2) / 1000)

    def test_total_loss_annualised_is_minus_one(self):
        r = m.compute_metrics([trade(1, -1.0, stake=1000)], 1000)
        self.assertEqual(r["annualised_return"], -1.0)


if __name__ == "__main__":
    unittest.main()
