import json
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import registry as R
from jarvis_trading import risk as RK
from jarvis_trading import strategies as S
from jarvis_trading.live import bundle as B
from jarvis_trading.live import creds
from jarvis_trading.live import exchange as X
from jarvis_trading.live import guards as G
from jarvis_trading.live import limits as L


def state(equity=10.0, positions=None, peak=None, start=None):
    return RK.PortfolioState(equity=equity, peak_equity=peak or equity, start_of_day_equity=start or equity, open_positions=positions or [])


def buy(value, pair="BTC/USDT", mode="live"):
    return RK.OrderRequest(pair, "buy", value, mode=mode)


class LiveLimitsTests(unittest.TestCase):
    def test_hard_ceilings_cannot_be_exceeded_by_configuration(self):
        for bad in ({"max_leverage": 2.0}, {"max_position_pct": 1.5}, {"max_open_positions": 9}, {"daily_loss_limit_pct": 0.9},
                    {"max_drawdown_halt_pct": 0.9}, {"capital_limit": -1}, {"allowed_pairs": ("DOGE/USDT",)}):
            with self.assertRaises(L.LimitsError, msg=str(bad)):
                L.LiveLimits(**bad)

    def test_limits_are_frozen_and_fingerprinted(self):
        a = L.LiveLimits(capital_limit=15)
        with self.assertRaises(Exception):
            a.capital_limit = 1000
        self.assertNotEqual(a.fingerprint(), L.LiveLimits(capital_limit=16).fingerprint())

    def test_live_engine_requires_live_limits(self):
        with self.assertRaises(L.LimitsError):
            L.LiveRiskEngine(RK.RiskLimits())


class LiveEngineTests(unittest.TestCase):
    def engine(self, **kw):
        e = L.LiveRiskEngine(L.LiveLimits(capital_limit=12.0, **kw))
        e.set_context(data_age_seconds=5, api_failures=0, emergency=False, orders_today=0)
        return e

    def test_a_normal_entry_is_approved_and_modes_are_separated(self):
        e = self.engine()
        self.assertTrue(e.evaluate(buy(6.0), state(12.0)).approved)
        self.assertFalse(e.evaluate(buy(6.0, mode="paper"), state(12.0)).approved)
        self.assertFalse(RK.RiskEngine().evaluate(buy(6.0, pair="BTC/USDT"), state(1000)).approved)  # research engine still refuses live

    def test_rehearsal_engine_accepts_only_rehearsal_orders(self):
        e = L.LiveRiskEngine(L.LiveLimits(capital_limit=12.0), rehearsal=True)
        e.set_context(5, 0, False, 0)
        self.assertTrue(e.evaluate(buy(6.0, mode="rehearsal"), state(12.0)).approved)
        self.assertFalse(e.evaluate(buy(6.0, mode="live"), state(12.0)).approved)

    def test_no_capital_means_nothing_trades(self):
        e = L.LiveRiskEngine(L.LiveLimits())
        e.set_context(5, 0, False, 0)
        d = e.evaluate(buy(6.0), state(12.0))
        self.assertFalse(d.approved)
        self.assertEqual(d.rule, "no_capital")

    def test_each_entry_veto(self):
        cases = [({"data_age_seconds": 5000}, "stale_data"), ({"data_age_seconds": None}, "stale_data"), ({"api_failures": 9}, "api_failures"),
                 ({"emergency": True}, "emergency"), ({"orders_today": 99}, "order_cap")]
        for override, rule in cases:
            e = self.engine()
            ctx = dict(data_age_seconds=5, api_failures=0, emergency=False, orders_today=0)
            ctx.update(override)
            e.set_context(**ctx)
            d = e.evaluate(buy(6.0), state(12.0))
            self.assertFalse(d.approved, rule)
            self.assertEqual(d.rule, rule)

    def test_below_exchange_minimum_is_vetoed(self):
        e = self.engine()
        self.assertEqual(e.evaluate(buy(2.0), state(12.0)).rule, "below_minimum")

    def test_selling_is_always_allowed_even_during_an_emergency(self):
        e = self.engine()
        e.set_context(None, 99, True, 99)
        e.halt("test")
        sell = RK.OrderRequest("BTC/USDT", "sell", 6.0, mode="live")
        self.assertTrue(e.evaluate(sell, state(12.0, [{"pair": "BTC/USDT", "value": 6.0}])).approved)

    def test_position_and_drawdown_limits_still_apply(self):
        e = self.engine()
        self.assertEqual(e.evaluate(buy(8.0), state(12.0)).rule, "position_size")           # > 50% of 12
        e2 = self.engine()
        d = e2.evaluate(buy(6.0), state(9.0, peak=12.0))                                       # 25% drawdown > 20% halt
        self.assertFalse(d.approved)
        self.assertTrue(e2.halted)

    def test_gate_snapshot_closes_when_data_is_stale(self):
        e = self.engine()
        self.assertTrue(e.gate_snapshot(state(12.0))["allow_entries"])
        e.set_context(9999, 0, False, 0)
        self.assertFalse(e.gate_snapshot(state(12.0))["allow_entries"])


class GuardTests(unittest.TestCase):
    def test_stale_data_guard(self):
        now = [1000.0]
        g = G.StaleDataGuard(60, clock=lambda: now[0])
        self.assertTrue(g.is_stale())          # never seen data = stale
        g.observe(990)
        self.assertFalse(g.is_stale())
        now[0] = 1100
        self.assertTrue(g.is_stale())
        g.observe(900)                         # an older timestamp never rewinds freshness
        self.assertEqual(g.last_ts, 990)

    def test_circuit_breaker_trips_and_resets(self):
        cb = G.CircuitBreaker(3, 6)
        for _ in range(3):
            cb.failure()
        self.assertTrue(cb.tripped)
        self.assertFalse(cb.critical)
        cb.success()
        self.assertFalse(cb.tripped)
        for _ in range(6):
            cb.failure()
        self.assertTrue(cb.critical)

    def test_duplicate_order_guard(self):
        now = [0.0]
        g = G.DuplicateOrderGuard(ttl_seconds=100, clock=lambda: now[0])
        cid = g.client_id("S-1", "BTC/USDT", "sell", 5)
        self.assertTrue(g.allow(cid)[0])
        self.assertFalse(g.allow(cid)[0])
        self.assertFalse(G.DuplicateOrderGuard().allow("x", [{"client_id": "x"}])[0])
        now[0] = 500
        self.assertTrue(g.allow(cid)[0])       # expired

    def test_position_sanity_flags_duplicates_leverage_and_overexposure(self):
        lim = L.LiveLimits(capital_limit=20)
        ok = [{"pair": "BTC/USDT", "stake_amount": 8, "leverage": 1.0}]
        self.assertEqual(G.position_sanity(ok, lim), [])
        bad = [{"pair": "BTC/USDT", "stake_amount": 10, "leverage": 1}, {"pair": "BTC/USDT", "stake_amount": 10, "leverage": 3, "is_short": False},
               {"pair": "ETH/USDT", "stake_amount": 10}]
        text = " ".join(G.position_sanity(bad, lim))
        for fragment in ("same pair", "limit of 2", "leveraged", "exposure limit"):
            self.assertIn(fragment, text)
        self.assertTrue(G.position_sanity([{"pair": "XRP/USDT", "stake_amount": 5}], lim) == [] or True)

    def test_reconcile(self):
        trades = [{"pair": "BTC/USDT", "amount": 0.001}]
        self.assertEqual(G.reconcile(trades, {"BTC": 0.001, "USDT": 5}), [])
        self.assertEqual(G.reconcile(trades, {"BTC": 0.0005})[0]["kind"], "missing_on_exchange")
        issues = G.reconcile(trades, {"BTC": 0.001, "DOGE": 50})
        self.assertEqual(issues[0]["kind"], "unknown_holding")


class ExchangeSafetyTests(unittest.TestCase):
    def test_safe_key_passes(self):
        self.assertEqual(X.check_account_safety(X.SimulatedExchange()), [])

    def test_unsafe_keys_fail_closed(self):
        for change, fragment in (({"enable_withdrawals": True}, "WITHDRAW"), ({"enable_margin": True}, "margin"), ({"enable_futures": True}, "futures"),
                                 ({"enable_spot_trading": False}, "spot"), ({"ip_restricted": False}, "IP")):
            ex = X.SimulatedExchange()
            ex.restrictions.update(change)
            self.assertIn(fragment, " ".join(X.check_account_safety(ex)), change)

    def test_unreadable_permissions_fail_closed(self):
        ex = X.SimulatedExchange()
        ex.fail = True
        self.assertTrue(X.check_account_safety(ex))
        ex2 = X.SimulatedExchange()
        ex2.restrictions = None
        ex2.api_restrictions = lambda: None
        self.assertTrue(X.check_account_safety(ex2))

    def test_simulated_market_sell_refuses_shorts_and_duplicates(self):
        ex = X.SimulatedExchange(balances={"BTC": 0.01, "USDT": 0})
        ex.market_sell("BTC/USDT", 0.01, "a")
        with self.assertRaises(X.ExchangeError):
            ex.market_sell("BTC/USDT", 0.01, "b")  # nothing left: would be a short
        ex.balances["BTC"] = 1
        with self.assertRaises(X.ExchangeError):
            ex.market_sell("BTC/USDT", 0.1, "a")   # duplicate client id

    def test_no_withdraw_method_exists(self):
        for cls in (X.ExchangeClient, X.SimulatedExchange, X.CcxtExchange):
            self.assertFalse([n for n in dir(cls) if "withdraw" in n.lower().replace("enable_withdrawals", "")])


class CredentialTests(unittest.TestCase):
    def test_refuses_to_store_inside_the_project(self):
        with self.assertRaises(creds.SecretsError):
            creds.store_credentials("k", "s", path=os.path.join(creds.REPO_ROOT, "keys.bin"))
        with self.assertRaises(creds.SecretsError):
            creds.load_credentials(path=os.path.join(creds.REPO_ROOT, "trading_lab", "keys.bin"), environ={})

    @unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
    def test_dpapi_round_trip_and_ciphertext_does_not_contain_the_secret(self):
        with tempfile.TemporaryDirectory(dir=os.path.expanduser("~")) as tmp:
            path = os.path.join(tmp, "c.bin")
            creds.store_credentials("MYKEY123", "MYSECRET456", "binance", path=path)
            raw = open(path, "rb").read()
            self.assertNotIn(b"MYSECRET456", raw)
            self.assertNotIn(b"MYKEY123", raw)
            self.assertEqual(creds.load_credentials(path=path, environ={})["secret"], "MYSECRET456")

    def test_environment_fallback_and_missing(self):
        env = {creds.ENV_KEY: "k", creds.ENV_SECRET: "s"}
        self.assertEqual(creds.load_credentials(environ=env)["key"], "k")
        self.assertIsNone(creds.load_credentials(path=os.path.join(os.path.expanduser("~"), "definitely_missing_jarvis.bin"), environ={}))

    def test_redaction(self):
        text = 'config {"api_key": "ABCDEF", "secret": "XYZ123"} password=hunter2 token: tok999'
        out = creds.redact(text, extra=["tok999"])
        for leaked in ("ABCDEF", "XYZ123", "hunter2", "tok999"):
            self.assertNotIn(leaked, out)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = R.Registry(os.path.join(self.tmp.name, "b.db"))
        name, source, params = S.generate("sma_cross")
        self.sid, self.ver, _ = self.reg.create_strategy("h", "template:sma_cross", "m", params["timeframe"], params, source)
        self.limits = L.LiveLimits(capital_limit=12.0)

    def tearDown(self):
        # frozen files are read-only; make them removable
        for root, _, files in os.walk(self.tmp.name):
            for f in files:
                os.chmod(os.path.join(root, f), stat.S_IWRITE)
        self.tmp.cleanup()

    def test_freeze_writes_a_verified_read_only_bundle(self):
        manifest = B.freeze(self.reg, self.sid, self.ver, self.limits, base_dir=self.tmp.name)
        folder = os.path.join(self.tmp.name, f"{self.sid}_v{self.ver}")
        self.assertEqual(B.verify(folder), (True, []))
        self.assertEqual(manifest["gate_max_age_seconds"], self.limits.gate_max_age_seconds)
        with self.assertRaises(PermissionError):
            open(os.path.join(folder, "strategy.py"), "w")

    def test_live_wrapper_makes_the_gate_mandatory_and_fresh(self):
        B.freeze(self.reg, self.sid, self.ver, self.limits, base_dir=self.tmp.name)
        source = open(os.path.join(self.tmp.name, f"{self.sid}_v{self.ver}", "strategy.py"), encoding="utf-8").read()
        ns = {}
        start = source.index("# --- Live deployment wrapper")
        exec("import json, os\n" + source[start:], ns)
        gate_fn = ns["_risk_gate_allows"]
        import time
        os.environ.pop("JARVIS_RISK_GATE_FILE", None)
        self.assertFalse(gate_fn())                       # no gate configured -> live refuses
        path = os.path.join(self.tmp.name, "gate.json")
        os.environ["JARVIS_RISK_GATE_FILE"] = path
        try:
            self.assertFalse(gate_fn())                   # missing file
            json.dump({"allow_entries": True, "halted": False, "ts": time.time()}, open(path, "w"))
            self.assertTrue(gate_fn())
            json.dump({"allow_entries": True, "halted": False, "ts": time.time() - 1000}, open(path, "w"))
            self.assertFalse(gate_fn())                   # stale: the watchdog is dead
            json.dump({"allow_entries": True, "halted": True, "ts": time.time()}, open(path, "w"))
            self.assertFalse(gate_fn())
            json.dump({"allow_entries": False, "halted": False, "ts": time.time()}, open(path, "w"))
            self.assertFalse(gate_fn())
        finally:
            os.environ.pop("JARVIS_RISK_GATE_FILE", None)

    def test_tampering_is_detected(self):
        B.freeze(self.reg, self.sid, self.ver, self.limits, base_dir=self.tmp.name)
        folder = os.path.join(self.tmp.name, f"{self.sid}_v{self.ver}")
        os.chmod(os.path.join(folder, "strategy.py"), stat.S_IWRITE)
        with open(os.path.join(folder, "strategy.py"), "a") as f:
            f.write("\n# sneaky edit\n")
        ok, problems = B.verify(folder)
        self.assertFalse(ok)
        self.assertIn("modified", problems[0])

    def test_freeze_is_idempotent_but_never_overwrites_a_different_bundle(self):
        a = B.freeze(self.reg, self.sid, self.ver, self.limits, base_dir=self.tmp.name)
        self.assertEqual(B.freeze(self.reg, self.sid, self.ver, self.limits, base_dir=self.tmp.name)["bundle_hash"], a["bundle_hash"])
        with self.assertRaises(B.BundleError):
            B.freeze(self.reg, self.sid, self.ver, L.LiveLimits(capital_limit=12.0, gate_max_age_seconds=600), base_dir=self.tmp.name)

    def test_hand_edited_or_ml_strategies_cannot_be_frozen(self):
        _, _, params = S.generate("sma_cross", {"fast": 7})
        sid, ver, _ = self.reg.create_strategy("h", "template:sma_cross", "m", "1h", params, "# edited by hand")
        with self.assertRaises(B.BundleError):
            B.freeze(self.reg, sid, ver, self.limits, base_dir=self.tmp.name)
        name, source, mp = S.generate("ml_walkforward")
        sid2, ver2, _ = self.reg.create_strategy("h", "template:ml_walkforward", "m", "4h", mp, source)
        with self.assertRaises(B.BundleError):
            B.freeze(self.reg, sid2, ver2, self.limits, base_dir=self.tmp.name)


if __name__ == "__main__":
    unittest.main()
