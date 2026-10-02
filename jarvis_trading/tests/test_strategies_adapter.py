import ast
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import freqtrade_adapter as A
from jarvis_trading import strategies as S


class StrategyGenerationTests(unittest.TestCase):
    def test_deterministic_byte_identical(self):
        for template in S.TEMPLATES:
            self.assertEqual(S.generate(template), S.generate(template))

    def test_parameters_change_name_and_source(self):
        a = S.generate("sma_cross")
        b = S.generate("sma_cross", {"fast": 21})
        self.assertNotEqual(a[0], b[0])
        self.assertNotEqual(a[1], b[1])

    def test_every_template_is_valid_python_with_matching_class(self):
        for template in S.TEMPLATES:
            name, source, _ = S.generate(template)
            tree = ast.parse(source)
            classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
            self.assertEqual(classes, [name])

    def test_long_only_unleveraged_no_roi_exit(self):
        for template in S.TEMPLATES:
            _, source, _ = S.generate(template)
            self.assertIn("can_short = False", source)
            self.assertNotIn("leverage", source.lower())
            self.assertIn('minimal_roi = {"0": 100}', source)

    def test_every_strategy_consults_the_risk_gate_and_fails_closed(self):
        for template in S.TEMPLATES:
            _, source, _ = S.generate(template)
            self.assertIn("JARVIS_RISK_GATE_FILE", source)
            self.assertIn("return False  # fail closed", source)
            self.assertIn("confirm_trade_entry", source)

    def test_unknown_template_rejected(self):
        with self.assertRaises(ValueError):
            S.generate("get_rich_quick")

    def test_only_the_control_peeks_at_the_future(self):
        for template in S.TEMPLATES:
            _, source, _ = S.generate(template)
            peeks = "shift(-" in source
            self.assertEqual(peeks, template in S.CONTROL_TEMPLATES, template)

    def test_gate_logic_blocks_when_file_missing_or_denies(self):
        _, source, _ = S.generate("sma_cross")
        # exercise only the gate function, without importing freqtrade
        start = source.index("def _risk_gate_allows")
        end = source.index("class JT_")
        namespace = {}
        exec("import json, os\n" + source[start:end], namespace)
        gate = namespace["_risk_gate_allows"]
        os.environ.pop("JARVIS_RISK_GATE_FILE", None)
        self.assertTrue(gate())  # no gate configured (backtests)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gate.json")
            os.environ["JARVIS_RISK_GATE_FILE"] = path
            try:
                self.assertFalse(gate())  # configured but missing -> closed
                with open(path, "w") as f:
                    f.write("not json")
                self.assertFalse(gate())  # unreadable -> closed
                with open(path, "w") as f:
                    f.write('{"allow_entries": false}')
                self.assertFalse(gate())
                with open(path, "w") as f:
                    f.write('{"allow_entries": true}')
                self.assertTrue(gate())
            finally:
                os.environ.pop("JARVIS_RISK_GATE_FILE", None)


class SafeConfigTests(unittest.TestCase):
    def test_default_config_is_safe(self):
        A.assert_safe_config(A.build_config())

    def test_refuses_live_mode(self):
        cfg = A.build_config()
        cfg["dry_run"] = False
        with self.assertRaises(A.UnsafeConfig):
            A.assert_safe_config(cfg)

    def test_refuses_any_credentials(self):
        for key in ("key", "secret", "password", "uid"):
            cfg = A.build_config()
            cfg["exchange"][key] = "abc"
            with self.assertRaises(A.UnsafeConfig):
                A.assert_safe_config(cfg)

    def test_refuses_leverage_futures_and_margin(self):
        for change in ({"trading_mode": "futures"}, {"leverage": 3}, {"margin_mode": "isolated"}):
            cfg = A.build_config()
            cfg.update(change)
            with self.assertRaises(A.UnsafeConfig):
                A.assert_safe_config(cfg)

    def test_refuses_api_server_exposed_beyond_localhost(self):
        cfg = A.build_config()
        cfg["api_server"] = {"listen_ip_address": "0.0.0.0"}
        with self.assertRaises(A.UnsafeConfig):
            A.assert_safe_config(cfg)

    def test_write_config_enforces_safety(self):
        cfg = A.build_config()
        cfg["dry_run"] = False
        with self.assertRaises(A.UnsafeConfig):
            A.write_config("should_not_exist", cfg)
        self.assertFalse(os.path.exists(os.path.join(A.USER_DIR, "should_not_exist.json")))

    def test_slippage_is_included_in_per_side_cost(self):
        self.assertGreater(A.EXCHANGE_FEE + A.SLIPPAGE_ASSUMPTION, A.EXCHANGE_FEE)


class WriteStrategyTests(unittest.TestCase):
    def test_refuses_to_overwrite_different_content(self):
        name = "JT_unittest_tmp"
        path = os.path.join(A.STRATEGY_DIR, f"{name}.py")
        try:
            A.write_strategy(name, "# one")
            A.write_strategy(name, "# one")  # identical: fine
            with self.assertRaises(RuntimeError):
                A.write_strategy(name, "# two")
        finally:
            if os.path.exists(path):
                os.remove(path)


@unittest.skipUnless(os.path.exists(A.FT_EXE), "Freqtrade venv not installed")
class FreqtradeIntegrationTests(unittest.TestCase):
    def test_backtest_runs_and_returns_trades(self):
        name, source, params = S.generate("sma_cross")
        A.write_strategy(name, source)
        res = A.run_backtest(name, "20230101-20230331", params["timeframe"])
        self.assertIn("trades", res)
        self.assertAlmostEqual(res["per_side_cost"], A.EXCHANGE_FEE + A.SLIPPAGE_ASSUMPTION)

    def test_lookahead_detector_catches_the_cheating_control_and_clears_the_honest_one(self):
        control, csrc, cparams = S.generate("lookahead_control")
        honest, hsrc, hparams = S.generate("sma_cross")
        A.write_strategy(control, csrc)
        A.write_strategy(honest, hsrc)
        self.assertIs(A.run_lookahead_analysis(control, "20230101-20230331", "1h")["clean"], False)
        self.assertIs(A.run_lookahead_analysis(honest, "20230101-20230331", "1h")["clean"], True)


if __name__ == "__main__":
    unittest.main()
