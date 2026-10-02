import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import stats as S

DAY = 86400


class MonteCarloTests(unittest.TestCase):
    def test_deterministic_with_seed(self):
        ratios = [0.02, -0.01, 0.03, -0.02, 0.01] * 10
        self.assertEqual(S.monte_carlo(ratios, n_sims=300, seed=7), S.monte_carlo(ratios, n_sims=300, seed=7))
        self.assertNotEqual(S.monte_carlo(ratios, n_sims=300, seed=7)["final_return_p50"],
                            S.monte_carlo(ratios, n_sims=300, seed=8)["final_return_p50"])

    def test_all_winners_never_ruined(self):
        r = S.monte_carlo([0.01] * 50, n_sims=200)
        self.assertEqual(r["ruin_probability"], 0.0)
        self.assertEqual(r["probability_of_loss"], 0.0)
        self.assertGreater(r["final_return_p5"], 0)

    def test_heavy_losers_have_high_loss_probability_and_ruin(self):
        r = S.monte_carlo([-0.30, 0.05] * 30, stake_fraction=1.0, n_sims=300)
        self.assertGreater(r["probability_of_loss"], 0.9)
        self.assertGreater(r["ruin_probability"], 0.9)

    def test_empty_returns_none(self):
        self.assertIsNone(S.monte_carlo([]))

    def test_percentiles_are_ordered(self):
        rng = random.Random(3)
        r = S.monte_carlo([rng.gauss(0.001, 0.02) for _ in range(120)], n_sims=500)
        self.assertLessEqual(r["final_return_p5"], r["final_return_p50"])
        self.assertLessEqual(r["final_return_p50"], r["final_return_p95"])
        self.assertLessEqual(r["max_drawdown_p50"], r["max_drawdown_p95"])


class SignFlipTests(unittest.TestCase):
    def test_clearly_profitable_has_tiny_p(self):
        p = S.sign_flip_p_value([0.01 + 0.002 * (i % 3) for i in range(100)], n_perm=2000)
        self.assertLess(p, 0.01)

    def test_clearly_losing_has_large_p(self):
        p = S.sign_flip_p_value([-0.01] * 80 + [0.002] * 20, n_perm=2000)
        self.assertGreater(p, 0.9)

    def test_pure_noise_is_usually_not_significant(self):
        significant = 0
        for seed in range(40):
            rng = random.Random(seed)
            noise = [rng.gauss(0, 0.02) for _ in range(150)]
            if S.sign_flip_p_value(noise, n_perm=400, seed=seed) < 0.05:
                significant += 1
        # about 5% expected by construction; allow generous slack, but nowhere near "most"
        self.assertLessEqual(significant, 8)

    def test_can_report_p_values_below_monte_carlo_resolution(self):
        # strong edge: the permutation test alone could never go below 1/(n_perm+1)
        data = [0.01 + 0.002 * (i % 3) for i in range(200)]
        p = S.sign_flip_p_value(data, n_perm=500)
        self.assertLess(p, 1.0 / 501)

    def test_bound_is_a_valid_upper_bound_on_the_permutation_estimate(self):
        rng = random.Random(4)
        for mean in (0.0, 0.003, 0.006):
            data = [rng.gauss(mean, 0.03) for _ in range(150)]
            mc = S.sign_flip_p_value(data, n_perm=20000, seed=2)
            self.assertGreaterEqual(S.sign_flip_p_bound(data) + 1e-9, mc * 0.9)

    def test_bound_is_one_without_positive_evidence(self):
        self.assertEqual(S.sign_flip_p_bound([0.0] * 50), 1.0)
        self.assertEqual(S.sign_flip_p_bound([-0.01] * 50), 1.0)

    def test_too_few_trades(self):
        self.assertIsNone(S.sign_flip_p_value([0.01]))

    def test_reproducible(self):
        data = [0.01, -0.005, 0.02, -0.01, 0.015] * 8
        self.assertEqual(S.sign_flip_p_value(data, n_perm=500, seed=5), S.sign_flip_p_value(data, n_perm=500, seed=5))


class NoiseBenchmarkTests(unittest.TestCase):
    def test_grows_with_number_of_strategies_tried(self):
        a = S.noise_sharpe_benchmark(10, 365)
        b = S.noise_sharpe_benchmark(1000, 365)
        self.assertGreater(b, a)
        self.assertGreater(a, 0)

    def test_shrinks_with_more_data(self):
        self.assertGreater(S.noise_sharpe_benchmark(100, 100), S.noise_sharpe_benchmark(100, 2000))

    def test_degenerate_inputs(self):
        self.assertEqual(S.noise_sharpe_benchmark(1, 365), 0.0)

    def test_matches_a_simulation_roughly(self):
        # best of 50 skill-less strategies over 365 days, simulated, vs the formula
        rng = random.Random(11)
        best = []
        for _ in range(40):
            sharpes = []
            for _ in range(50):
                r = [rng.gauss(0, 0.01) for _ in range(365)]
                m = sum(r) / len(r)
                sd = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
                sharpes.append(m / sd * math.sqrt(365))
            best.append(max(sharpes))
        simulated = sum(best) / len(best)
        self.assertAlmostEqual(S.noise_sharpe_benchmark(50, 365), simulated, delta=0.35)


class RegimeTests(unittest.TestCase):
    def closes(self, daily_growth, n=300):
        price, out = 100.0, []
        for i in range(n):
            price *= 1 + daily_growth(i)
            out.append((i * DAY, price))
        return out

    def test_uptrend_labelled_bull_and_downtrend_bear(self):
        up = S.classify_regimes(self.closes(lambda i: 0.005))
        down = S.classify_regimes(self.closes(lambda i: -0.005))
        self.assertTrue(all(v.startswith("bull") for v in up.values()))
        self.assertTrue(all(v.startswith("bear") for v in down.values()))

    def test_flat_is_chop(self):
        flat = S.classify_regimes(self.closes(lambda i: 0.0005 * (-1) ** i))
        self.assertTrue(all(v.startswith("chop") for v in flat.values()))

    def test_labels_use_only_past_data(self):
        # Changing prices AFTER day 150 must not change any label on or before day 150.
        base = self.closes(lambda i: 0.004)
        altered = list(base)
        for i in range(151, len(altered)):
            altered[i] = (altered[i][0], altered[i][1] * (3 if i % 2 else 0.2))
        a = S.classify_regimes(base)
        b = S.classify_regimes(altered)
        for day in range(0, 151):
            self.assertEqual(a.get(day), b.get(day), day)

    def test_no_labels_before_lookback(self):
        labels = S.classify_regimes(self.closes(lambda i: 0.001))
        self.assertTrue(all(day >= 90 for day in labels))

    def test_metrics_by_regime_groups_trades_by_open_day(self):
        labels = {d: ("bull" if d < 100 else "bear") for d in range(200)}
        trades = [{"open_ts": d * DAY, "close_ts": d * DAY + 3600, "profit_ratio": 0.01, "stake": 100, "profit_abs": 1.0}
                  for d in list(range(10, 20)) + list(range(110, 120))]
        out = S.metrics_by_regime(trades, labels, min_trades=5)
        self.assertEqual(set(out), {"bull", "bear"})
        self.assertEqual(out["bull"]["n_trades"], 10)

    def test_regimes_with_too_few_trades_omitted(self):
        labels = {d: "bull" for d in range(50)}
        trades = [{"open_ts": d * DAY, "close_ts": d * DAY + 3600, "profit_ratio": 0.01, "stake": 100, "profit_abs": 1.0}
                  for d in range(3)]
        self.assertEqual(S.metrics_by_regime(trades, labels, min_trades=5), {})


if __name__ == "__main__":
    unittest.main()
