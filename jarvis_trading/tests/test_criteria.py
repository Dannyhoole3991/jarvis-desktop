import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading.criteria import ResearchCriteria, evaluate_backtest, evaluate_paper


def good_evidence():
    return {
        "in_sample": {"n_trades": 300, "max_drawdown": 0.15, "sharpe": 1.4, "profit_factor": 1.5,
                      "annualised_return": 0.40, "win_rate": 0.55, "fees_pct_of_gross_profit": 0.2},
        "out_of_sample": {"n_trades": 90, "max_drawdown": 0.12, "sharpe": 1.0, "profit_factor": 1.3,
                          "annualised_return": 0.25, "win_rate": 0.53, "fees_pct_of_gross_profit": 0.25},
        "lookahead_clean": True,
        "robustness": {"neighbour_fail_fraction": 0.1},
        "regimes": {"bull": {"total_return": 0.2}, "bear": {"total_return": 0.01}, "chop": {"total_return": -0.02}},
        "monte_carlo": {"p_value": 0.0004, "ruin_probability": 0.0},
        "n_strategies_tested": 20,
        "leverage": 1.0,
    }


class BacktestVerdictTests(unittest.TestCase):
    def verdict(self, mutate=None):
        e = good_evidence()
        if mutate:
            mutate(e)
        return evaluate_backtest(e)

    def test_clean_strategy_becomes_candidate(self):
        v = self.verdict()
        self.assertEqual(v.status, "CANDIDATE", v.summary())

    def test_missing_evidence_is_never_a_pass(self):
        for key in ("lookahead_clean", "robustness", "regimes", "monte_carlo", "out_of_sample", "in_sample"):
            v = self.verdict(lambda e, k=key: e.pop(k))
            self.assertEqual(v.status, "INCOMPLETE", key)
            self.assertIn(f"MISSING_{key.upper()}", v.codes())

    def test_lookahead_rejects(self):
        v = self.verdict(lambda e: e.update(lookahead_clean=False))
        self.assertEqual(v.status, "REJECTED")
        self.assertIn("LOOKAHEAD_BIAS", v.codes())

    def test_leverage_rejects(self):
        self.assertIn("LEVERAGE", self.verdict(lambda e: e.update(leverage=3)).codes())

    def test_drawdown_rejects(self):
        v = self.verdict(lambda e: e["in_sample"].update(max_drawdown=0.4))
        self.assertIn("EXCESSIVE_DRAWDOWN", v.codes())

    def test_too_few_trades_rejects(self):
        self.assertIn("TOO_FEW_TRADES", self.verdict(lambda e: e["in_sample"].update(n_trades=20)).codes())
        self.assertIn("TOO_FEW_OOS_TRADES", self.verdict(lambda e: e["out_of_sample"].update(n_trades=5)).codes())

    def test_poor_oos_performance_rejects(self):
        v = self.verdict(lambda e: e["out_of_sample"].update(sharpe=-0.2))
        self.assertIn("POOR_OOS_PERFORMANCE", v.codes())

    def test_oos_degradation_rejects(self):
        def mutate(e):
            e["in_sample"]["sharpe"] = 2.5
            e["out_of_sample"]["sharpe"] = 0.8
        self.assertIn("OOS_DEGRADATION", self.verdict(mutate).codes())

    def test_excessive_costs_rejects(self):
        v = self.verdict(lambda e: e["in_sample"].update(fees_pct_of_gross_profit=0.8))
        self.assertIn("EXCESSIVE_COSTS", v.codes())

    def test_too_good_to_be_true_rejects(self):
        for field, value in (("sharpe", 6.0), ("annualised_return", 50.0), ("win_rate", 0.97)):
            v = self.verdict(lambda e, f=field, val=value: e["in_sample"].update({f: val}))
            self.assertIn("SUSPICIOUSLY_HIGH_PERFORMANCE", v.codes(), field)

    def test_unstable_parameters_reject(self):
        v = self.verdict(lambda e: e["robustness"].update(neighbour_fail_fraction=0.7))
        self.assertIn("UNSTABLE_PARAMETERS", v.codes())

    def test_regime_dependence_rejects(self):
        v = self.verdict(lambda e: e.update(regimes={"bull": {"total_return": 0.3}, "bear": {"total_return": -0.1},
                                                     "chop": {"total_return": -0.1}}))
        self.assertIn("REGIME_DEPENDENT", v.codes())

    def test_multiple_testing_correction_tightens_threshold(self):
        v = self.verdict(lambda e: (e["monte_carlo"].update(p_value=0.01), e.update(n_strategies_tested=1)))
        self.assertEqual(v.status, "CANDIDATE", v.summary())          # 0.01 < 0.05 / 1
        v = self.verdict(lambda e: (e["monte_carlo"].update(p_value=0.01), e.update(n_strategies_tested=50)))
        self.assertIn("NOT_SIGNIFICANT", v.codes())                    # 0.01 > 0.05 / 50

    def test_ruin_risk_rejects(self):
        v = self.verdict(lambda e: e["monte_carlo"].update(ruin_probability=0.2))
        self.assertIn("RUIN_RISK", v.codes())

    def test_summary_text_names_the_reason(self):
        v = self.verdict(lambda e: e.update(lookahead_clean=False))
        self.assertTrue(v.summary().startswith("REJECTED -- "))
        self.assertIn("lookahead", v.summary())

    def test_rejection_beats_incomplete(self):
        def mutate(e):
            e.pop("regimes")
            e["in_sample"]["max_drawdown"] = 0.9
        self.assertEqual(self.verdict(mutate).status, "REJECTED")

    def test_verdict_records_criteria_fingerprint(self):
        self.assertEqual(self.verdict().criteria_fingerprint, ResearchCriteria().fingerprint())
        self.assertNotEqual(ResearchCriteria().fingerprint(), ResearchCriteria(max_drawdown=0.5).fingerprint())


class PaperVerdictTests(unittest.TestCase):
    expected = {"avg_daily_return": 0.002, "trades_per_day": 2.0}

    def test_consistent_paper_continues(self):
        v = evaluate_paper({"days": 30, "avg_daily_return": 0.0018, "max_drawdown": 0.05, "n_trades": 60}, self.expected)
        self.assertEqual(v.status, "CONTINUE", v.summary())

    def test_too_short_is_incomplete(self):
        v = evaluate_paper({"days": 5, "avg_daily_return": 0.01, "max_drawdown": 0.01, "n_trades": 10}, self.expected)
        self.assertEqual(v.status, "INCOMPLETE")

    def test_divergence_from_backtest_rejects(self):
        v = evaluate_paper({"days": 30, "avg_daily_return": 0.0002, "max_drawdown": 0.05, "n_trades": 60}, self.expected)
        self.assertIn("BACKTEST_PAPER_DIVERGENCE", v.codes())

    def test_paper_drawdown_rejects_even_early(self):
        v = evaluate_paper({"days": 3, "avg_daily_return": -0.05, "max_drawdown": 0.3, "n_trades": 4}, self.expected)
        self.assertEqual(v.status, "REJECTED")
        self.assertIn("PAPER_DRAWDOWN", v.codes())

    def test_no_activity_rejects(self):
        v = evaluate_paper({"days": 20, "avg_daily_return": 0.0, "max_drawdown": 0.0, "n_trades": 0}, self.expected)
        self.assertIn("NO_ACTIVITY", v.codes())

    def test_abnormal_trade_rate_rejects(self):
        v = evaluate_paper({"days": 20, "avg_daily_return": 0.002, "max_drawdown": 0.02, "n_trades": 400}, self.expected)
        self.assertIn("ABNORMAL_TRADE_RATE", v.codes())


if __name__ == "__main__":
    unittest.main()
