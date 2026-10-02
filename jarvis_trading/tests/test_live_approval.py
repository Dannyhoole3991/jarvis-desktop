import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import criteria as C
from jarvis_trading import promotion as PR
from jarvis_trading import registry as R
from jarvis_trading import strategies as S
from jarvis_trading.live import approval as AP
from jarvis_trading.live import bundle as B
from jarvis_trading.live import config as CFG
from jarvis_trading.live import creds, paths

GOOD = {"days": 90, "n_trades": 120, "max_drawdown": 0.06, "sharpe": 1.4}


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.patches = [mock.patch.object(paths, name, os.path.join(t, fname)) for name, fname in (
            ("LIVE_DIR", "live"), ("BUNDLE_DIR", "live/bundles"), ("USER_DIR", "live/ft"), ("LOG_DIR", "live/logs"),
            ("CONFIG_FILE", "live/live_config.json"), ("ARMED_FILE", "live/ARMED.json"), ("EMERGENCY_FILE", "live/EMERGENCY_STOP"),
            ("EMERGENCY_LATCH", "live/LATCH.json"), ("HALT_FILE", "live/HALT.json"))]
        for p in self.patches:
            p.start()
        paths.ensure_dirs()
        self.env = mock.patch.dict(os.environ, {creds.ENV_KEY: "k", creds.ENV_SECRET: "s"})
        self.env.start()
        self.reg = R.Registry(os.path.join(t, "a.db"))
        self.n = 0
        self.settings = {"exchange": "binance", "capital_limit": 12.0, "limits": {}}

    def tearDown(self):
        self.env.stop()
        for p in self.patches:
            p.stop()
        for root, _, files in os.walk(self.tmp.name):
            for f in files:
                os.chmod(os.path.join(root, f), stat.S_IWRITE)
        self.tmp.cleanup()

    def challenger(self, sharpe=1.4):
        self.n += 1
        name, source, params = S.generate("sma_cross", {"fast": 5 + self.n})
        sid, ver, _ = self.reg.create_strategy("h", "template:sma_cross", "m", params["timeframe"], params, source)
        for st in ("backtested", "candidate", "paper"):
            self.reg.set_status(sid, ver, st, "t")
        self.reg.record_experiment(sid, ver, "verdict", {"verdict": "CANDIDATE", "criteria": C.ResearchCriteria().fingerprint()}, passed=True)
        self.reg.record_experiment(sid, ver, "paper_check", {"metrics": dict(GOOD, sharpe=sharpe), "verdict": "CONTINUE"}, passed=True)
        return sid, ver

    def approved(self, sharpe=1.4):
        sid, ver = self.challenger(sharpe)
        r = PR.review(self.reg, sid, ver)
        AP.approve_promotion(self.reg, r["review_id"], r["phrase"], self.settings, base_dir=paths.BUNDLE_DIR)
        return sid, ver

    # --- promotion approval
    def test_wrong_or_missing_confirmation_is_refused(self):
        sid, ver = self.challenger()
        r = PR.review(self.reg, sid, ver)
        for typed in ("", "yes", "PROMOTE", r["phrase"] + " now", f"PROMOTE {sid} v{ver} REVIEW 9999"):
            with self.assertRaises(AP.ApprovalError):
                AP.approve_promotion(self.reg, r["review_id"], typed, self.settings, base_dir=paths.BUNDLE_DIR)
        self.assertIsNone(self.reg.current_live())

    def test_ineligible_or_unknown_review_cannot_be_approved(self):
        sid, ver = self.challenger()
        self.reg.record_experiment(sid, ver, "paper_check", {"metrics": dict(GOOD, days=5), "verdict": "CONTINUE"}, passed=True)
        r = PR.review(self.reg, sid, ver)
        self.assertFalse(r["eligible"])
        with self.assertRaises(AP.ApprovalError):
            AP.approve_promotion(self.reg, r["review_id"], PR.approval_phrase(r["review_id"], sid, ver), self.settings, base_dir=paths.BUNDLE_DIR)
        with self.assertRaises(AP.ApprovalError):
            AP.approve_promotion(self.reg, 424242, "x", self.settings)

    def test_approval_freezes_a_bundle_sets_live_and_does_not_arm(self):
        sid, ver = self.approved()
        self.assertEqual(self.reg.current_live(), (sid, ver))
        self.assertTrue(B.verify(os.path.join(paths.BUNDLE_DIR, f"{sid}_v{ver}"))[0])
        self.assertIsNone(AP.read_arming())
        self.assertEqual(self.reg.promotion_events("approval")[0]["actor"], "human")

    def test_phrase_is_case_and_spacing_tolerant_but_exact_otherwise(self):
        sid, ver = self.challenger()
        r = PR.review(self.reg, sid, ver)
        AP.approve_promotion(self.reg, r["review_id"], "  " + r["phrase"].lower().replace(" ", "  ") + " ", self.settings, base_dir=paths.BUNDLE_DIR)
        self.assertEqual(self.reg.current_live(), (sid, ver))

    # --- arming
    def test_arming_requires_a_live_strategy_capital_credentials_and_the_exact_phrase(self):
        with self.assertRaises(AP.ApprovalError):
            AP.arm(self.reg, "anything", settings=self.settings)                       # nothing live yet
        sid, ver = self.approved()
        with self.assertRaises(AP.ApprovalError):
            AP.arm(self.reg, "ENABLE LIVE TRADING", settings=self.settings)            # wrong phrase
        with self.assertRaises(AP.ApprovalError):
            AP.arm(self.reg, AP.arming_phrase(sid, ver, 0), settings=dict(self.settings, capital_limit=0))   # no capital
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(creds.ENV_KEY), os.environ.pop(creds.ENV_SECRET)
            with mock.patch.object(creds, "DEFAULT_PATH", os.path.join(os.path.expanduser("~"), "no_such_jarvis_creds.bin")):
                with self.assertRaises(AP.ApprovalError):
                    AP.arm(self.reg, AP.arming_phrase(sid, ver, 12.0), settings=self.settings)   # no credentials
        self.assertIsNone(AP.read_arming())

    def test_a_valid_arming_passes_the_trader_preconditions(self):
        sid, ver = self.approved()
        armed = AP.arm(self.reg, AP.arming_phrase(sid, ver, 12.0), settings=self.settings)
        self.assertEqual(armed["capital_limit"], 12.0)
        self.assertEqual(AP.arming_problems(self.reg, self.settings), [])

    def test_changing_limits_or_capital_after_arming_blocks_trading(self):
        sid, ver = self.approved()
        AP.arm(self.reg, AP.arming_phrase(sid, ver, 12.0), settings=self.settings)
        problems = AP.arming_problems(self.reg, dict(self.settings, capital_limit=500.0))
        self.assertTrue(any("changed since arming" in p for p in problems))
        problems = AP.arming_problems(self.reg, dict(self.settings, limits={"max_position_pct": 0.9}))
        self.assertTrue(any("changed since arming" in p for p in problems))

    def test_tampered_bundle_blocks_trading(self):
        sid, ver = self.approved()
        AP.arm(self.reg, AP.arming_phrase(sid, ver, 12.0), settings=self.settings)
        f = os.path.join(paths.BUNDLE_DIR, f"{sid}_v{ver}", "strategy.py")
        os.chmod(f, stat.S_IWRITE)
        with open(f, "a") as fh:
            fh.write("\n# edit\n")
        self.assertTrue(any("modified" in p for p in AP.arming_problems(self.reg, self.settings)))

    def test_expired_arming_blocks_trading(self):
        sid, ver = self.approved()
        AP.arm(self.reg, AP.arming_phrase(sid, ver, 12.0), days=1, settings=self.settings)
        import time
        self.assertTrue(any("expired" in p for p in AP.arming_problems(self.reg, self.settings, now=time.time() + 3 * 86400)))

    def test_promoting_a_new_strategy_disarms_automatically(self):
        sid, ver = self.approved()
        AP.arm(self.reg, AP.arming_phrase(sid, ver, 12.0), settings=self.settings)
        self.approved(sharpe=2.5)
        self.assertIsNone(AP.read_arming())
        self.assertEqual(AP.arming_problems(self.reg, self.settings), ["live trading is not armed"])

    def test_disarm_needs_no_confirmation_and_is_logged(self):
        sid, ver = self.approved()
        AP.arm(self.reg, AP.arming_phrase(sid, ver, 12.0), settings=self.settings)
        self.assertTrue(AP.disarm(self.reg, "just because"))
        self.assertIsNone(AP.read_arming())
        self.assertEqual(self.reg.promotion_events("disarm")[0]["details"]["reason"], "just because")

    def test_emergency_latch_blocks_arming_and_needs_the_phrase_to_clear(self):
        sid, ver = self.approved()
        open(paths.EMERGENCY_LATCH, "w").write("{}")
        with self.assertRaises(AP.ApprovalError):
            AP.arm(self.reg, AP.arming_phrase(sid, ver, 12.0), settings=self.settings)
        with self.assertRaises(AP.ApprovalError):
            AP.clear_emergency(self.reg, "clear")
        AP.clear_emergency(self.reg, "clear emergency stop")
        self.assertFalse(os.path.exists(paths.EMERGENCY_LATCH))
        AP.arm(self.reg, AP.arming_phrase(sid, ver, 12.0), settings=self.settings)

    # --- rollback / retire
    def test_rollback_restores_the_previous_version_and_disarms(self):
        a = self.approved()
        AP.arm(self.reg, AP.arming_phrase(*a, 12.0), settings=self.settings)
        b = self.approved(sharpe=2.5)
        self.assertEqual(self.reg.current_live(), b)
        with self.assertRaises(AP.ApprovalError):
            AP.rollback(self.reg, a[0], a[1], "rollback please", base_dir=paths.BUNDLE_DIR)
        AP.rollback(self.reg, a[0], a[1], f"ROLLBACK TO {a[0]} v{a[1]}", base_dir=paths.BUNDLE_DIR)
        self.assertEqual(self.reg.current_live(), a)
        self.assertIsNone(AP.read_arming())
        self.assertEqual([h["event"] for h in self.reg.live_history()][0], "rolled_back")

    def test_retire_live(self):
        a = self.approved()
        with self.assertRaises(AP.ApprovalError):
            AP.retire_live_strategy(self.reg, "no")
        AP.retire_live_strategy(self.reg, "RETIRE LIVE STRATEGY")
        self.assertIsNone(self.reg.current_live())


class ConfigTests(unittest.TestCase):
    def limits(self, **kw):
        return CFG.build_limits({"capital_limit": 12.0, "limits": kw})

    def build(self, rehearsal=True, limits=None):
        return CFG.build_freqtrade_config(limits or self.limits(), "4h", rehearsal, "sqlite:///x.sqlite", 8123, "u", "p")

    def test_live_and_rehearsal_differ_only_in_dry_run(self):
        live, rehearsal = self.build(False), self.build(True)
        self.assertFalse(live["dry_run"])
        self.assertTrue(rehearsal["dry_run"])
        self.assertEqual(live["exchange"]["key"], "")
        self.assertEqual(live["trading_mode"], "spot")
        self.assertEqual(live["api_server"]["listen_ip_address"], "127.0.0.1")
        self.assertTrue(live["order_types"]["stoploss_on_exchange"])
        self.assertEqual(live["order_types"]["stoploss"], "limit")   # Binance spot supports only limit stops on the exchange
        self.assertEqual(live["stake_amount"], 6.0)
        self.assertLessEqual(live["max_open_trades"], 2)

    def test_unsafe_configs_are_refused(self):
        lim = self.limits()
        base = self.build(False)
        for change in ({"dry_run": True}, {"trading_mode": "futures"}, {"leverage": 3}, {"force_entry_enable": True}, {"stake_amount": 99},
                       {"max_open_trades": 9}):
            cfg = json.loads(json.dumps(base))
            cfg.update(change)
            with self.assertRaises(CFG.LiveConfigError, msg=str(change)):
                CFG.assert_safe_live_config(cfg, lim, rehearsal=False)
        cfg = json.loads(json.dumps(base))
        cfg["exchange"]["key"] = "leak"
        with self.assertRaises(CFG.LiveConfigError):
            CFG.assert_safe_live_config(cfg, lim, rehearsal=False)
        cfg = json.loads(json.dumps(base))
        cfg["api_server"]["listen_ip_address"] = "0.0.0.0"
        with self.assertRaises(CFG.LiveConfigError):
            CFG.assert_safe_live_config(cfg, lim, rehearsal=False)
        cfg = json.loads(json.dumps(base))
        cfg["order_types"]["stoploss_on_exchange"] = False
        with self.assertRaises(CFG.LiveConfigError):
            CFG.assert_safe_live_config(cfg, lim, rehearsal=False)

    def test_no_capital_no_config(self):
        with self.assertRaises(CFG.LiveConfigError):
            CFG.build_freqtrade_config(CFG.build_limits({"capital_limit": 0}), "4h", True, "sqlite:///x", 1, "u", "p")

    def test_settings_cannot_exceed_hard_ceilings_or_use_unknown_keys(self):
        with self.assertRaises(Exception):
            CFG.build_limits({"capital_limit": 10, "limits": {"max_leverage": 3}})
        with self.assertRaises(CFG.LiveConfigError):
            CFG.build_limits({"capital_limit": 10, "limits": {"yolo": 1}})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.json")
            with self.assertRaises(Exception):
                CFG.save_settings({"capital_limit": 10, "limits": {"max_leverage": 5}}, path)
            self.assertFalse(os.path.exists(path))
            with open(path, "w") as f:
                json.dump({"mystery": 1}, f)
            with self.assertRaises(CFG.LiveConfigError):
                CFG.load_settings(path)


if __name__ == "__main__":
    unittest.main()
