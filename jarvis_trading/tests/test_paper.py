import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import freqtrade_adapter as A
from jarvis_trading import paper as P
from jarvis_trading import registry as R
from jarvis_trading import strategies as S


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_trades_db(path, closed=(), open_positions=(), leverage=1.0, is_short=0):
    """Builds a SQLite file with the columns of Freqtrade's real trades table (verified against a live dry-run database)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open INTEGER, fee_open_cost REAL, fee_close_cost REAL, "
                 "close_profit REAL, close_profit_abs REAL, stake_amount REAL, open_date TEXT, close_date TEXT, leverage REAL, is_short INTEGER)")
    for pair, opened, closed_at, stake, ratio in closed:
        conn.execute("INSERT INTO trades (pair, is_open, fee_open_cost, fee_close_cost, close_profit, close_profit_abs, stake_amount, open_date, close_date, leverage, is_short) "
                     "VALUES (?,0,0.1,0.1,?,?,?,?,?,?,?)", (pair, ratio, ratio * stake, stake, opened, closed_at, leverage, is_short))
    for pair, stake in open_positions:
        conn.execute("INSERT INTO trades (pair, is_open, fee_open_cost, fee_close_cost, close_profit, close_profit_abs, stake_amount, open_date, close_date, leverage, is_short) "
                     "VALUES (?,1,0.1,0,NULL,NULL,?,'2026-10-01 00:00:00',NULL,1.0,0)", (pair, stake))
    conn.commit()
    conn.close()


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = P.PAPER_DIR
        P.PAPER_DIR = os.path.join(self.tmp.name, "paper")
        self.reg = R.Registry(os.path.join(self.tmp.name, "lab.db"))
        name, source, params = S.generate("sma_cross")
        self.sid, self.ver, _ = self.reg.create_strategy("h", "template:sma_cross", "BTC/USDT/ETH/USDT", params["timeframe"], params, source)
        self.mgr = P.PaperManager(self.reg)

    def tearDown(self):
        P.PAPER_DIR = self.old_dir
        self.tmp.cleanup()

    def make_candidate(self):
        self.reg.set_status(self.sid, self.ver, "backtested", "bt")
        self.reg.set_status(self.sid, self.ver, "candidate", "passed")
        self.reg.record_experiment(self.sid, self.ver, "backtest_test", {"avg_daily_return": 0.002, "n_trades": 60, "days": 30})

    def test_only_candidates_may_be_paper_traded(self):
        for status_path in ([], ["backtested"], ["rejected"]):
            tmp = R.Registry(os.path.join(self.tmp.name, f"x{len(status_path)}{''.join(status_path)}.db"))
            name, source, params = S.generate("sma_cross")
            sid, ver, _ = tmp.create_strategy("h", "template:sma_cross", "m", "1h", params, source)
            for s in status_path:
                tmp.set_status(sid, ver, s, "r")
            with self.assertRaises(P.PaperError):
                P.PaperManager(tmp).prepare(sid, ver)

    def test_prepare_writes_safe_config_gate_and_sets_paper_status(self):
        self.make_candidate()
        paths = self.mgr.prepare(self.sid, self.ver)
        cfg = _load(paths["config"])
        A.assert_safe_config(cfg)
        self.assertTrue(cfg["dry_run"])
        self.assertEqual(cfg["initial_state"], "running")  # Freqtrade would otherwise boot STOPPED and never trade
        self.assertEqual(cfg["exchange"]["key"], "")
        self.assertTrue(_load(paths["gate"])["allow_entries"])
        self.assertEqual(self.reg.status(self.sid, self.ver), "paper")

    def test_command_is_dry_run_only(self):
        self.make_candidate()
        self.mgr.prepare(self.sid, self.ver)
        args = self.mgr.command(self.sid, self.ver)
        self.assertIn("--dry-run", args)
        self.assertIn("trade", args)
        self.assertNotIn("--live", args)

    def test_reads_closed_trades_and_open_positions(self):
        db = os.path.join(self.tmp.name, "t.sqlite")
        make_trades_db(db, closed=[("BTC/USDT", "2026-10-01 00:00:00", "2026-10-01 12:00:00", 100.0, 0.02)],
                       open_positions=[("ETH/USDT", 100.0)])
        trades = P.read_paper_trades(db)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["profit_ratio"], 0.02)
        self.assertEqual(P.read_open_positions(db), [{"pair": "ETH/USDT", "value": 100.0}])
        self.assertEqual(P.read_paper_trades(os.path.join(self.tmp.name, "missing.sqlite")), [])

    def test_gate_closes_when_paper_account_is_fully_exposed(self):
        self.make_candidate()
        paths = self.mgr.prepare(self.sid, self.ver)
        make_trades_db(paths["db"], open_positions=[("BTC/USDT", 250.0), ("ETH/USDT", 250.0)])
        snap = self.mgr.write_gate(self.sid, self.ver)
        self.assertFalse(snap["allow_entries"])
        self.assertFalse(_load(paths["gate"])["allow_entries"])

    def test_drawdown_breach_rejects_and_logs(self):
        self.make_candidate()
        paths = self.mgr.prepare(self.sid, self.ver)
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        make_trades_db(paths["db"], closed=[("BTC/USDT", now, now, 300.0, -0.30), ("ETH/USDT", now, now, 300.0, -0.30),
                                            ("BTC/USDT", now, now, 300.0, -0.30), ("ETH/USDT", now, now, 300.0, -0.30)])
        verdict, paper = self.mgr.evaluate(self.sid, self.ver)
        self.assertEqual(verdict.status, "REJECTED")
        self.assertEqual(self.reg.status(self.sid, self.ver), "rejected")
        self.assertIn("Paper trading:", self.reg.status_history(self.sid, self.ver)[-1]["reason"])

    def test_leverage_or_short_in_a_paper_run_is_rejected(self):
        for kw in ({"leverage": 3.0}, {"is_short": 1}):
            self.setUp()
            self.make_candidate()
            paths = self.mgr.prepare(self.sid, self.ver)
            now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            make_trades_db(paths["db"], closed=[("BTC/USDT", now, now, 100.0, 0.01)], **kw)
            verdict, _ = self.mgr.evaluate(self.sid, self.ver)
            self.assertEqual(verdict.status, "REJECTED")
            self.assertIn("PAPER_LEVERAGE_OR_SHORT", verdict.codes())
            self.tearDown()

    def test_too_little_paper_time_is_incomplete_not_a_pass(self):
        self.make_candidate()
        self.mgr.prepare(self.sid, self.ver)
        verdict, _ = self.mgr.evaluate(self.sid, self.ver)
        self.assertEqual(verdict.status, "INCOMPLETE")
        self.assertEqual(self.reg.status(self.sid, self.ver), "paper")

    def test_expected_values_come_from_the_heldout_backtest(self):
        self.make_candidate()
        exp = self.mgr.expected_from_backtest(self.sid, self.ver)
        self.assertAlmostEqual(exp["avg_daily_return"], 0.002)
        self.assertAlmostEqual(exp["trades_per_day"], 2.0)


if __name__ == "__main__":
    unittest.main()
