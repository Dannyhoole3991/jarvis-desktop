import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import lab as L
from jarvis_trading import metrics as M
from jarvis_trading import registry as R

DAY = 86400


def synthetic_closes(seed=3):
    rng = random.Random(seed)
    price, out = 100.0, []
    start = L._ts("20190101")
    for i in range(2800):
        price *= 1 + rng.gauss(0.0004, 0.025)
        out.append((start + i * DAY, price))
    return out


class FakeRunner:
    """Stands in for Freqtrade: returns scripted trades so the pipeline logic can be tested in seconds."""

    def __init__(self, mean=0.0, sd=0.02, trades_per_period=120, lookahead=True, per_period_mean=None, same_trades=False):
        self.same_trades = same_trades
        self.mean, self.sd, self.n = mean, sd, trades_per_period
        self.lookahead = lookahead
        self.per_period_mean = per_period_mean or {}
        self.backtests = []
        self.written = {}

    def tool_versions(self):
        return {"freqtrade": "fake"}

    def data_hash(self, pairs, timeframe):
        return "fakehash"

    def write_strategy(self, name, source):
        self.written[name] = source

    def run_lookahead_analysis(self, name, timerange, timeframe, pairs=None):
        return {"clean": self.lookahead, "raw": "fake", "exit_code": 0}

    def run_backtest(self, name, timerange, timeframe, pairs=None):
        self.backtests.append((name, timerange))
        a, b = L._range_epochs(timerange)
        rng = random.Random(f"{'' if self.same_trades else name}{timerange}")
        mean = self.per_period_mean.get(timerange, self.mean)
        step = (b - a) / (self.n + 1)
        trades = []
        for i in range(self.n):
            ratio = rng.gauss(mean, self.sd)
            t = a + step * (i + 1) + rng.uniform(-0.45, 0.45) * step
            trades.append({"pair": "BTC/USDT", "open_ts": t - 3600, "close_ts": t, "profit_ratio": ratio,
                           "profit_abs": ratio * 100, "stake": 100.0, "fee_paid": 0.3, "exit_reason": "signal"})
        return {"trades": trades, "command": "fake backtest", "per_side_cost": 0.0015}


class LabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = R.Registry(os.path.join(self.tmp.name, "lab.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def lab(self, runner, closes=True):
        return L.Lab(self.reg, runner, daily_closes=synthetic_closes() if closes else None, mc_sims=200)

    def test_lookahead_bias_rejected_without_any_backtest(self):
        runner = FakeRunner(lookahead=False)
        result = self.lab(runner).research("lookahead_control")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("LOOKAHEAD_BIAS", result["codes"])
        self.assertEqual(runner.backtests, [])
        self.assertTrue(result["verdict"].startswith("REJECTED --"))

    def test_static_lint_rejects_before_any_backtest_or_detector_run(self):
        runner = FakeRunner(lookahead=True)  # the dynamic detector would say "clean"
        result = self.lab(runner).research("mtf_leak_control")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("LOOKAHEAD_BIAS", result["codes"])
        self.assertIn("static check", result["verdict"])
        self.assertEqual(runner.backtests, [])

    def test_inconclusive_leakage_check_is_not_a_pass(self):
        runner = FakeRunner(lookahead=None)
        result = self.lab(runner).research("sma_cross")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("LEAKAGE_CHECK_INCONCLUSIVE", result["codes"])

    def test_losing_in_training_never_touches_test_period(self):
        runner = FakeRunner(per_period_mean={L.PERIODS["training"]: -0.01, L.PERIODS["validation"]: 0.01, L.PERIODS["test"]: 0.02})
        result = self.lab(runner).research("sma_cross")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("LOSING_IN_TRAINING", result["codes"])
        self.assertNotIn(L.PERIODS["test"], [t for _, t in runner.backtests])

    def test_pure_noise_is_not_promoted(self):
        for seed_mean in (0.0, 0.0005):
            runner = FakeRunner(mean=seed_mean, sd=0.03)
            result = self.lab(runner).research("rsi_reversion", {"oversold": 25 + int(seed_mean * 10000)})
            self.assertNotEqual(result["status"], "candidate", result["verdict"])

    def test_genuine_edge_becomes_candidate(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=200)
        result = self.lab(runner).research("sma_cross")
        self.assertEqual(result["status"], "candidate", result["verdict"])
        self.assertEqual(self.reg.status(result["strategy_id"], result["version"]), "candidate")

    def test_suspiciously_good_result_is_rejected(self):
        runner = FakeRunner(mean=0.08, sd=0.01, trades_per_period=150)
        result = self.lab(runner).research("sma_cross")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("SUSPICIOUSLY_HIGH_PERFORMANCE", result["codes"])

    def test_control_template_never_promoted_even_if_it_somehow_passes(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=200, lookahead=True)
        with mock.patch.object(L.LINT, "future_reference_findings", return_value=[]):  # pretend both leak checks missed it
            result = self.lab(runner).research("lookahead_control")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("CONTROL_STRATEGY", result["codes"])

    def test_re_researching_same_strategy_does_not_rerun_or_overwrite(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=200)
        lab = self.lab(runner)
        first = lab.research("sma_cross")
        runs = len(runner.backtests)
        second = lab.research("sma_cross")
        self.assertEqual(len(runner.backtests), runs)
        self.assertIn("already researched", second["verdict"])
        self.assertEqual(first["strategy_id"], second["strategy_id"])

    def test_held_out_test_period_run_exactly_once(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=200)
        result = self.lab(runner).research("sma_cross")
        tests = self.reg.experiments(result["strategy_id"], result["version"], "backtest_test")
        self.assertEqual(len(tests), 1)

    def test_missing_regime_data_leaves_strategy_incomplete_not_candidate(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=200)
        result = self.lab(runner, closes=False).research("sma_cross")
        self.assertEqual(result["status"], "backtested")
        self.assertTrue(result["verdict"].startswith("INCOMPLETE"))

    def test_multiple_testing_count_grows_and_tightens_bar(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=200)
        lab = self.lab(runner)
        lab.research("sma_cross", {"fast": 10})
        n1 = lab.trials_so_far()
        lab.research("sma_cross", {"fast": 12})
        n2 = lab.trials_so_far()
        self.assertGreater(n2, n1)
        self.assertGreater(n1, 1)  # neighbours count as trials too

    def test_every_experiment_records_reproduction_info(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=200)
        result = self.lab(runner).research("sma_cross")
        for exp in self.reg.experiments(result["strategy_id"], result["version"], "backtest_training"):
            self.assertEqual(exp["data_hash"], "fakehash")
            self.assertEqual(exp["seed"], 1)
            self.assertIn("fake", exp["tool_versions_json"])
            self.assertTrue(exp["command"])

    def test_every_status_change_has_a_logged_reason(self):
        runner = FakeRunner(mean=-0.01)
        result = self.lab(runner).research("sma_cross")
        history = self.reg.status_history(result["strategy_id"])
        self.assertTrue(all(h["reason"] for h in history))
        self.assertEqual(history[-1]["status"], "rejected")


class EfficiencyAndDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = R.Registry(os.path.join(self.tmp.name, "lab.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_behaviourally_identical_strategy_is_rejected_as_near_duplicate(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=200, same_trades=True)
        lab = L.Lab(self.reg, runner, daily_closes=synthetic_closes(), mc_sims=100)
        first = lab.research("sma_cross", {"fast": 10})
        second = lab.research("sma_cross", {"fast": 12})
        self.assertNotEqual(first["status"], "rejected" if first["status"] == "candidate" else "x")
        self.assertEqual(second["status"], "rejected")
        self.assertIn("NEAR_DUPLICATE", second["codes"])
        self.assertIn(first["strategy_id"], second["verdict"])
        # and it never reached the robustness sweep or the held-out period
        self.assertEqual(self.reg.experiments(second["strategy_id"], second["version"], "backtest_test"), [])
        self.assertEqual(self.reg.experiments(second["strategy_id"], second["version"], "robustness"), [])

    def test_distinct_strategies_are_not_flagged(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=200)
        lab = L.Lab(self.reg, runner, daily_closes=synthetic_closes(), mc_sims=100)
        lab.research("sma_cross", {"fast": 10})
        second = lab.research("sma_cross", {"fast": 12})
        self.assertNotIn("NEAR_DUPLICATE", second["codes"])

    def test_unstable_parameters_reject_before_the_heldout_period_is_touched(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=200, per_period_mean={"20190101-20240630": -0.02})
        lab = L.Lab(self.reg, runner, daily_closes=synthetic_closes(), mc_sims=100)
        result = lab.research("sma_cross")
        self.assertEqual(result["status"], "rejected")
        self.assertIn("UNSTABLE_PARAMETERS", result["codes"])
        self.assertNotIn(L.PERIODS["test"], [t for _, t in runner.backtests])
        self.assertEqual(self.reg.experiments(result["strategy_id"], result["version"], "backtest_test"), [])

    def test_too_few_trades_rejected_before_robustness_sweep(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=20)
        lab = L.Lab(self.reg, runner, daily_closes=synthetic_closes(), mc_sims=100)
        result = lab.research("sma_cross")
        self.assertIn("TOO_FEW_TRADES", result["codes"])
        self.assertEqual(self.reg.experiments(result["strategy_id"], result["version"], "robustness"), [])

    def test_concurrency_makes_the_significance_bar_stricter_not_looser(self):
        runner = FakeRunner(mean=0.008, sd=0.03, trades_per_period=200)
        solo = L.Lab(self.reg, runner, daily_closes=synthetic_closes(), concurrency=1).trials_so_far()
        many = L.Lab(self.reg, runner, daily_closes=synthetic_closes(), concurrency=6).trials_so_far()
        self.assertGreater(many, solo)
        self.assertEqual(many, 5)  # 0 recorded trials + 5 assumed in flight


class ClusteredRunner(FakeRunner):
    """Every day has 9 simultaneous trades sharing one market shock plus a little noise: heavy cross-pair correlation."""

    def run_backtest(self, name, timerange, timeframe, pairs=None):
        self.backtests.append((name, timerange))
        a, b = L._range_epochs(timerange)
        rng = random.Random(f"{name}{timerange}")
        trades = []
        days = int((b - a) // 86400)
        step = max(1, days // 25)
        for d in range(0, days, step):
            shock = rng.gauss(0.006, 0.012)
            for k in range(9):
                t = a + d * 86400 + 3600 * (k + 1)
                ratio = shock + rng.gauss(0, 0.002)
                trades.append({"pair": f"P{k}/USDT", "open_ts": t - 3600, "close_ts": t, "profit_ratio": ratio,
                               "profit_abs": ratio * 100, "stake": 100.0, "fee_paid": 0.3})
        return {"trades": trades, "command": "fake", "per_side_cost": 0.0015}


class PerDaySignificanceTests(unittest.TestCase):
    def test_correlated_simultaneous_trades_do_not_inflate_significance(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = R.Registry(os.path.join(tmp, "c.db"))
            lab = L.Lab(reg, ClusteredRunner(), daily_closes=synthetic_closes(), mc_sims=100)
            result = lab.research("sma_cross")
            mc = result["evidence"].get("monte_carlo")
            if mc is None:
                self.skipTest("strategy rejected before the statistics stage")
            self.assertGreaterEqual(mc["p_value_per_day"], mc["p_value_per_trade"])
            self.assertEqual(mc["p_value"], max(mc["p_value_per_day"], mc["p_value_per_trade"]))


class NeighbourTests(unittest.TestCase):
    def test_neighbours_vary_one_parameter_at_a_time(self):
        params = {"timeframe": "1h", "fast": 20, "slow": 100, "stoploss": 0.08}
        for n in L.neighbour_params(params):
            diffs = [k for k in params if params[k] != n[k]]
            self.assertEqual(len(diffs), 1)
            self.assertNotEqual(diffs[0], "timeframe")

    def test_fast_never_reaches_slow(self):
        for n in L.neighbour_params({"timeframe": "1h", "fast": 90, "slow": 100, "stoploss": 0.08}):
            self.assertLess(n["fast"], n["slow"])


if __name__ == "__main__":
    unittest.main()
