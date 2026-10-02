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
from jarvis_trading.live import exchange as X
from jarvis_trading.live import trader as T
from jarvis_trading.live.ftapi import FtApiError

GOOD = {"days": 90, "n_trades": 120, "max_drawdown": 0.06, "sharpe": 1.4}


class FakeApi:
    def __init__(self):
        self.open = []
        self.closed = []
        self.profit_all = 0.0
        self.profit_closed = 0.0
        self.fail = False
        self.exits_work = True
        self.calls = []

    def _guard(self):
        if self.fail:
            raise FtApiError("down")

    def status(self):
        self._guard()
        return list(self.open)

    def profit(self):
        self._guard()
        return {"profit_all_coin": self.profit_all, "profit_closed_coin": self.profit_closed}

    def trades(self, limit=200):
        self._guard()
        return {"trades": list(self.closed)}

    def stop_entries(self):
        self._guard()
        self.calls.append("stopentry")
        return {}

    def force_exit_all(self):
        self._guard()
        self.calls.append("forceexit")
        if self.exits_work:
            self.closed += self.open
            self.open = []
        return {}

    def stop_bot(self):
        self.calls.append("stop")
        return {}


class FakeEngine:
    def __init__(self):
        self.terminated = False

    def alive(self):
        return not self.terminated

    def terminate(self):
        self.terminated = True


def trade(pair="BTC/USDT", stake=6.0, tid=1, amount=0.0001, **kw):
    t = {"trade_id": tid, "pair": pair, "stake_amount": stake, "amount": amount, "open_date": "2026-10-03 01:00:00", "leverage": 1.0, "is_short": False, "profit_abs": 0.0}
    t.update(kw)
    return t


class TraderTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        names = (("LIVE_DIR", "live"), ("BUNDLE_DIR", "live/bundles"), ("USER_DIR", "live/ft"), ("LOG_DIR", "live/logs"), ("CONFIG_FILE", "live/cfg.json"),
                 ("ARMED_FILE", "live/ARMED.json"), ("EMERGENCY_FILE", "live/EMERGENCY_STOP"), ("EMERGENCY_LATCH", "live/LATCH.json"),
                 ("HALT_FILE", "live/HALT.json"), ("GATE_FILE", "live/gate.json"), ("STATUS_FILE", "live/status.json"), ("PID_FILE", "live/pid.json"),
                 ("STATE_FILE", "live/state.json"), ("TRADES_LOG", "live/logs/trades.jsonl"), ("EVENTS_LOG", "live/logs/events.jsonl"))
        self.patches = [mock.patch.object(paths, n, os.path.join(t, f)) for n, f in names]
        for p in self.patches:
            p.start()
        paths.ensure_dirs()
        self.env = mock.patch.dict(os.environ, {creds.ENV_KEY: "k", creds.ENV_SECRET: "s"})
        self.env.start()
        self.reg = R.Registry(os.path.join(t, "t.db"))
        name, source, params = S.generate("sma_cross")
        self.sid, self.ver, _ = self.reg.create_strategy("h", "template:sma_cross", "m", params["timeframe"], params, source)
        for st in ("backtested", "candidate", "paper"):
            self.reg.set_status(self.sid, self.ver, st, "t")
        self.reg.record_experiment(self.sid, self.ver, "verdict", {"verdict": "CANDIDATE", "criteria": C.ResearchCriteria().fingerprint()}, passed=True)
        self.reg.record_experiment(self.sid, self.ver, "paper_check", {"metrics": GOOD, "verdict": "CONTINUE"}, passed=True)
        self.settings = {"exchange": "binance", "capital_limit": 12.0, "limits": {}}
        self.limits = CFG.build_limits(self.settings)
        self.now = [1_790_000_000.0]
        self.api, self.exchange, self.engine = FakeApi(), X.SimulatedExchange(clock=lambda: self.now[0]), FakeEngine()
        self.review = PR.review(self.reg, self.sid, self.ver)

    def tearDown(self):
        self.env.stop()
        for p in self.patches:
            p.stop()
        for root, _, files in os.walk(self.tmp.name):
            for f in files:
                os.chmod(os.path.join(root, f), stat.S_IWRITE)
        self.tmp.cleanup()

    def approve(self):
        AP.approve_promotion(self.reg, self.review["review_id"], self.review["phrase"], self.settings, base_dir=paths.BUNDLE_DIR)

    def make(self, mode="rehearsal", approve=False):
        if approve or mode == "live":
            self.approve()
            manifest, folder = B.load_manifest(self.sid, self.ver)
        else:
            manifest = B.freeze(self.reg, self.sid, self.ver, self.limits, base_dir=paths.BUNDLE_DIR)
            folder = os.path.join(paths.BUNDLE_DIR, f"{self.sid}_v{self.ver}")
        return T.LiveTrader(self.reg, self.limits, mode, self.exchange, folder, manifest, api=self.api, engine=self.engine,
                            clock=lambda: self.now[0], sleep=lambda s: self.now.__setitem__(0, self.now[0] + s), settings=self.settings)

    def gate(self):
        with open(paths.GATE_FILE, encoding="utf-8") as f:
            return json.load(f)

    def events(self, kind=None):
        return T.EventLog(paths.EVENTS_LOG).tail(200, kind)


class WatchdogTests(TraderTestBase):
    def test_healthy_step_opens_a_fresh_gate_and_writes_status(self):
        tr = self.make()
        status = tr.step()
        gate = self.gate()
        self.assertTrue(gate["allow_entries"])
        self.assertEqual(gate["ts"], self.now[0])
        self.assertEqual(status["state"], "RUNNING")
        for key in ("equity", "available_capital", "realised_pnl", "unrealised_pnl", "drawdown", "today", "open_positions"):
            self.assertIn(key, status)
        self.assertEqual(status["equity"], 12.0)

    def test_stale_price_feed_closes_the_gate(self):
        self.exchange.ticker_ts = self.now[0] - 5000
        tr = self.make()
        tr.step()
        self.assertFalse(self.gate()["allow_entries"])
        self.assertIn("market data", self.gate()["reason"].lower())

    def test_api_failures_trip_then_emergency_stop(self):
        tr = self.make()
        self.api.fail = True
        for _ in range(tr.limits.max_api_failures):
            tr.step()
        self.assertFalse(self.gate()["allow_entries"])
        self.assertNotEqual(tr.state, "EMERGENCY")
        for _ in range(tr.limits.emergency_api_failures):
            tr.step()
        self.assertEqual(tr.state, "EMERGENCY")
        self.assertTrue(os.path.exists(paths.EMERGENCY_LATCH))

    def test_recovery_after_a_short_outage(self):
        tr = self.make()
        self.api.fail = True
        for _ in range(tr.limits.max_api_failures):
            tr.step()
        self.api.fail = False
        tr.step()
        self.assertTrue(self.gate()["allow_entries"])

    def test_drawdown_breach_halts_persistently_across_restarts(self):
        tr = self.make()
        tr.step()
        self.api.profit_all = -3.0                      # 25% down from 12
        tr.step()
        self.assertEqual(tr.state, "HALTED")
        self.assertFalse(self.gate()["allow_entries"])
        self.assertTrue(os.path.exists(paths.HALT_FILE))
        self.api.profit_all = 0.0                       # even if equity recovers, a human must clear the halt
        tr2 = self.make()
        tr2.step()
        self.assertFalse(self.gate()["allow_entries"])
        AP.clear_halt(self.reg, "clear risk halt")
        self.make().step()
        self.assertTrue(self.gate()["allow_entries"])

    def test_duplicate_position_halts_entries(self):
        self.api.open = [trade(tid=1), trade(tid=2)]
        tr = self.make()
        tr.step()
        self.assertFalse(self.gate()["allow_entries"])
        self.assertIn("stopentry", self.api.calls)
        self.assertTrue(self.events("position_sanity"))

    def test_leveraged_position_triggers_emergency_stop(self):
        self.api.open = [trade(leverage=3.0)]
        tr = self.make()
        tr.step()
        self.assertEqual(tr.state, "EMERGENCY")
        self.assertIn("forceexit", self.api.calls)

    def test_full_exposure_blocks_new_entries(self):
        self.api.open = [trade(pair="BTC/USDT", stake=6.0, tid=1), trade(pair="ETH/USDT", stake=6.0, tid=2)]
        tr = self.make()
        tr.step()
        self.assertFalse(self.gate()["allow_entries"])

    def test_tampered_bundle_triggers_emergency_stop(self):
        tr = self.make()
        f = os.path.join(tr.bundle_folder, "strategy.py")
        os.chmod(f, stat.S_IWRITE)
        with open(f, "a") as fh:
            fh.write("\n# edit\n")
        tr.step()
        self.assertEqual(tr.state, "EMERGENCY")
        self.assertTrue(self.events("bundle_tampered"))

    def test_trades_are_logged_exactly_once(self):
        self.api.open = [trade(tid=7)]
        tr = self.make()
        tr.step()
        tr.step()
        self.api.open, self.api.closed = [], [trade(tid=7, close_date="x")]
        tr.step()
        tr.step()
        log = T.EventLog(paths.TRADES_LOG).tail(50)
        self.assertEqual([r["kind"] for r in log], ["trade_opened", "trade_closed"])

    def test_last_veto_is_captured_with_the_gate_reason(self):
        tr = self.make()
        self.exchange.ticker_ts = self.now[0] - 5000
        with open(os.path.join(paths.LOG_DIR, "freqtrade.log"), "w") as f:
            f.write("2026-10-03 INFO - User denied entry for BTC/USDT.\n")
        tr.step()
        self.assertEqual(tr.last_veto["pair"], "BTC/USDT")
        self.assertIn("market data", tr.last_veto["reason"].lower())

    def test_peak_equity_survives_a_restart(self):
        self.api.profit_all = 2.0
        self.make().step()
        self.api.profit_all = 1.0
        status = self.make().step()
        self.assertAlmostEqual(status["drawdown"], 1.0 / 14.0)


class EmergencyTests(TraderTestBase):
    def test_emergency_file_flattens_stops_latches_and_disarms(self):
        self.api.open = [trade()]
        tr = self.make("live")
        AP.arm(self.reg, AP.arming_phrase(self.sid, self.ver, 12.0), settings=self.settings)
        tr.step()
        open(paths.EMERGENCY_FILE, "w").close()
        tr.step()
        self.assertEqual(tr.state, "EMERGENCY")
        self.assertEqual(self.api.calls[-3:], ["stopentry", "forceexit", "stop"])
        self.assertEqual(self.api.open, [])
        self.assertTrue(self.engine.terminated)
        self.assertFalse(self.gate()["allow_entries"])
        self.assertIsNone(AP.read_arming())
        self.assertEqual(self.reg.promotion_events("emergency")[0]["details"]["action"], "stop")

    def test_latch_survives_and_blocks_a_new_trader_until_cleared(self):
        tr = self.make()
        tr.emergency_stop("test")
        self.assertTrue(self.make().preflight())
        AP.clear_emergency(self.reg, "CLEAR EMERGENCY STOP")
        self.assertEqual([p for p in self.make().preflight() if "emergency" in p], [])

    def test_falls_back_to_the_exchange_when_freqtrade_cannot_exit(self):
        self.api.open = [trade(amount=0.001)]
        self.api.exits_work = False
        self.exchange.balances = {"BTC": 0.001, "USDT": 0}
        tr = self.make()
        tr.emergency_stop("hung engine", wait_seconds=4)
        self.assertEqual(self.exchange.sold[0]["pair"], "BTC/USDT")
        self.assertTrue(self.events("emergency_flatten"))

    def test_flatten_is_idempotent(self):
        self.api.open = [trade(amount=0.001)]
        self.api.exits_work = False
        self.exchange.balances = {"BTC": 0.002, "USDT": 0}
        tr = self.make()
        tr._flatten_via_exchange(self.api.open)
        tr._flatten_via_exchange(self.api.open)
        self.assertEqual(len(self.exchange.sold), 1)

    def test_emergency_still_completes_when_the_api_is_dead(self):
        self.api.fail = True
        tr = self.make()
        tr.emergency_stop("api dead", wait_seconds=2)
        self.assertEqual(tr.state, "EMERGENCY")
        self.assertTrue(self.engine.terminated)


class LiveModeTests(TraderTestBase):
    def test_live_preflight_requires_arming(self):
        tr = self.make("live")
        self.assertIn("live trading is not armed", tr.preflight())

    def test_armed_and_safe_account_passes_preflight(self):
        self.exchange.balances = {"USDT": 50.0}
        tr = self.make("live")
        AP.arm(self.reg, AP.arming_phrase(self.sid, self.ver, 12.0), settings=self.settings)
        self.assertEqual(tr.preflight(), [])

    def test_unsafe_key_or_small_balance_or_tiny_stake_blocks_live(self):
        tr = self.make("live")
        AP.arm(self.reg, AP.arming_phrase(self.sid, self.ver, 12.0), settings=self.settings)
        self.exchange.balances = {"USDT": 5.0}
        self.assertTrue(any("less than the configured capital" in p for p in tr.preflight()))
        self.exchange.balances = {"USDT": 50.0}
        self.exchange.restrictions["enable_withdrawals"] = True
        self.assertTrue(any("WITHDRAW" in p for p in tr.preflight()))
        self.exchange.restrictions["enable_withdrawals"] = False
        self.exchange.min_cost = 10.0
        self.assertTrue(any("below" in p and "minimum" in p for p in tr.preflight()))

    def test_disarming_while_running_winds_down_then_stops_when_flat(self):
        self.exchange.balances = {"USDT": 50.0, "BTC": 0.0001}
        self.api.open = [trade(stake=5.0)]
        tr = self.make("live")
        AP.arm(self.reg, AP.arming_phrase(self.sid, self.ver, 12.0), settings=self.settings)
        tr.step()
        self.assertEqual(tr.state, "RUNNING")
        AP.disarm(self.reg, "human disarmed")
        self.now[0] += 61
        tr.step()
        self.assertEqual(tr.state, "WIND_DOWN")
        self.assertFalse(self.gate()["allow_entries"])      # no new entries, but exits keep working
        self.assertNotIn("forceexit", self.api.calls)        # existing positions are not dumped by a disarm
        self.api.open = []
        tr.step()
        self.assertEqual(tr.state, "STOPPED")
        self.assertTrue(self.engine.terminated)

    def test_live_launch_never_dry_run_and_rehearsal_always_is(self):
        live = self.make("live")
        with mock.patch.object(T, "EngineProcess") as ep, mock.patch.object(creds, "load_credentials", return_value={"key": "KEY", "secret": "SEC"}):
            live.build_engine(8123, "u", "p")
            args, env, _ = ep.call_args[0]
            self.assertNotIn("--dry-run", args)
            self.assertEqual(env["FREQTRADE__EXCHANGE__KEY"], "KEY")
            with open(os.path.join(paths.LIVE_DIR, "freqtrade_live_config.json"), encoding="utf-8") as f:
                written = f.read()
            self.assertNotIn("KEY", written)
            self.assertNotIn("SEC", written)
            self.assertFalse(json.loads(written)["dry_run"])
        rehearsal = self.make("rehearsal")
        with mock.patch.object(T, "EngineProcess") as ep:
            rehearsal.build_engine(8124, "u", "p")
            args, env, _ = ep.call_args[0]
            self.assertIn("--dry-run", args)
            self.assertNotIn("FREQTRADE__EXCHANGE__KEY", env)

    def test_events_never_contain_credentials(self):
        log = T.EventLog(paths.EVENTS_LOG)
        log.write("x", detail='api_key="SUPERSECRETKEY" token=abc123', nested={"password": "hunter2", "note": "secret=zzz999"})
        text = open(paths.EVENTS_LOG, encoding="utf-8").read()
        self.assertNotIn("SUPERSECRETKEY", text)
        self.assertNotIn("abc123", text)
        for leaked in ("hunter2", "zzz999"):
            self.assertNotIn(leaked, text)

    def test_mode_must_be_known(self):
        with self.assertRaises(ValueError):
            T.LiveTrader(self.reg, self.limits, "yolo", self.exchange, "x", {"strategy_id": "a", "version": 1})


if __name__ == "__main__":
    unittest.main()
