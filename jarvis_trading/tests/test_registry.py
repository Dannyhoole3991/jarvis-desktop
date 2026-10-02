import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading.registry import Registry, RegistryError


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.db")
        self.reg = Registry(self.path)
        self.sid, self.ver, self.created = self.reg.create_strategy(
            "SMA cross trends", "template:sma_cross", "BTC/USDT", "1h", {"fast": 10, "slow": 50}, "CODE-A",
            "2021-01-01..2022-12-31", "2023-01-01..2023-12-31", "2024-01-01..2024-12-31")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_initial_state(self):
        self.assertTrue(self.created)
        self.assertEqual(self.sid, "S-0001")
        self.assertEqual(self.ver, 1)
        self.assertEqual(self.reg.status(self.sid, 1), "generated")
        self.assertEqual(self.reg.get_version(self.sid, 1)["parameters"], {"fast": 10, "slow": 50})

    def test_identical_strategy_not_duplicated(self):
        sid, ver, created = self.reg.create_strategy(
            "different words, same thing", "template:sma_cross", "BTC/USDT", "1h", {"slow": 50, "fast": 10}, "CODE-A",
            "2021-01-01..2022-12-31", "2023-01-01..2023-12-31", "2024-01-01..2024-12-31")
        self.assertEqual((sid, ver, created), (self.sid, 1, False))
        self.assertEqual(len(self.reg.list_versions()), 1)

    def test_changing_parameters_makes_new_version_not_an_edit(self):
        version, created = self.reg.new_version(self.sid, {"fast": 12, "slow": 50}, "CODE-A", "tune fast window")
        self.assertEqual((version, created), (2, True))
        self.assertEqual(self.reg.get_version(self.sid, 1)["parameters"], {"fast": 10, "slow": 50})
        self.assertEqual(self.reg.get_version(self.sid, 2)["parameters"], {"fast": 12, "slow": 50})
        self.assertEqual(len(self.reg.versions(self.sid)), 2)

    def test_new_version_requires_reason(self):
        with self.assertRaises(RegistryError):
            self.reg.new_version(self.sid, {"fast": 1}, "CODE-A", "")

    def test_database_blocks_direct_edits_and_deletes(self):
        conn = sqlite3.connect(self.path)
        for sql in (
            "UPDATE strategy_versions SET code='HACKED'",
            "DELETE FROM strategy_versions",
            "UPDATE status_log SET status='candidate'",
            "DELETE FROM status_log",
            "UPDATE strategies SET hypothesis='x'",
        ):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                conn.execute(sql)
        conn.close()

    def test_results_cannot_be_overwritten(self):
        self.reg.record_experiment(self.sid, 1, "backtest", {"total_return": 0.1})
        conn = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE experiments SET metrics_json='{}'")
        with self.assertRaises(sqlite3.DatabaseError):
            conn.execute("DELETE FROM experiments")
        conn.close()
        self.assertEqual(len(self.reg.experiments(self.sid)), 1)

    def test_status_flow_and_reasons(self):
        self.reg.set_status(self.sid, 1, "backtested", "Backtest complete.")
        self.reg.set_status(self.sid, 1, "candidate", "Passed all gates.")
        self.assertEqual(self.reg.status(self.sid, 1), "candidate")
        self.assertEqual([h["status"] for h in self.reg.status_history(self.sid, 1)],
                         ["generated", "backtested", "candidate"])

    def test_cannot_skip_stages(self):
        with self.assertRaises(RegistryError):
            self.reg.set_status(self.sid, 1, "paper", "skipping straight to paper")

    def test_rejected_is_terminal(self):
        self.reg.set_status(self.sid, 1, "rejected", "Out-of-sample Sharpe negative.")
        for target in ("generated", "backtested", "candidate", "paper"):
            with self.assertRaises(RegistryError):
                self.reg.set_status(self.sid, 1, target, "second thoughts")

    def test_new_version_after_rejection_keeps_the_failure_on_record(self):
        self.reg.set_status(self.sid, 1, "rejected", "Drawdown too deep.")
        version, _ = self.reg.new_version(self.sid, {"fast": 5, "slow": 20}, "CODE-A", "faster windows")
        self.assertEqual(self.reg.status(self.sid, 1), "rejected")
        self.assertEqual(self.reg.status(self.sid, version), "generated")

    def test_status_change_needs_reason(self):
        with self.assertRaises(RegistryError):
            self.reg.set_status(self.sid, 1, "backtested", "  ")

    def test_unknown_status_rejected(self):
        with self.assertRaises(RegistryError):
            self.reg.set_status(self.sid, 1, "profitable", "wishful")

    def test_experiment_stores_reproducibility_fields(self):
        self.reg.record_experiment(self.sid, 1, "backtest", {"sharpe": 1.2}, passed=True, data_hash="abc",
                                   command="freqtrade backtesting ...", params={"fee": 0.001}, seed=42,
                                   tool_versions={"freqtrade": "2026.9"})
        e = self.reg.experiments(self.sid, kind="backtest")[0]
        self.assertEqual((e["data_hash"], e["seed"], e["passed"]), ("abc", 42, 1))
        self.assertEqual(e["metrics"]["sharpe"], 1.2)

    def test_list_versions_shows_current_status_and_reason(self):
        self.reg.set_status(self.sid, 1, "rejected", "Lookahead bias detected.")
        row = self.reg.list_versions("rejected")[0]
        self.assertEqual((row["strategy_id"], row["reason"]), (self.sid, "Lookahead bias detected."))

    def test_unknown_strategy_errors(self):
        with self.assertRaises(RegistryError):
            self.reg.record_experiment("S-9999", 1, "backtest", {})
        with self.assertRaises(RegistryError):
            self.reg.new_version("S-9999", {}, "x", "why")

    def test_persists_across_instances(self):
        again = Registry(self.path)
        self.assertEqual(again.status(self.sid, 1), "generated")


if __name__ == "__main__":
    unittest.main()
