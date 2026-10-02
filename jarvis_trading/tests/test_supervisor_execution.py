import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import criteria as C
from jarvis_trading import execution as E
from jarvis_trading import live_readiness as LR
from jarvis_trading import paper as P
from jarvis_trading import registry as R
from jarvis_trading import risk as RK
from jarvis_trading import strategies as S
from jarvis_trading import supervisor as SUP


class FakeManager:
    def __init__(self, registry):
        self.registry = registry
        self.started, self.gates, self.evals = [], [], []
        self.verdict = C.Verdict("CONTINUE", [("PAPER_OK", "ok")], "x")

    def start(self, sid, ver):
        if self.registry.status(sid, ver) == "candidate":
            self.registry.set_status(sid, ver, "paper", "test start")
        self.started.append((sid, ver))
        return 1234

    def write_gate(self, sid, ver):
        self.gates.append((sid, ver))

    def evaluate(self, sid, ver):
        self.evals.append((sid, ver))
        return self.verdict, {}


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = R.Registry(os.path.join(self.tmp.name, "s.db"))
        self.now = [1_800_000_000.0]
        self.alive_pids = set()
        self.old_status = SUP.STATUS_FILE
        SUP.STATUS_FILE = os.path.join(self.tmp.name, "status.json")
        self.old_paper = P.PAPER_DIR
        P.PAPER_DIR = os.path.join(self.tmp.name, "paper")

    def tearDown(self):
        SUP.STATUS_FILE = self.old_status
        P.PAPER_DIR = self.old_paper
        self.tmp.cleanup()

    def candidate(self, n):
        name, source, params = S.generate("sma_cross", {"fast": 10 + n})
        sid, ver, _ = self.reg.create_strategy("h", "template:sma_cross", "m", "1h", params, source)
        self.reg.set_status(sid, ver, "backtested", "bt")
        self.reg.set_status(sid, ver, "candidate", "passed")
        return sid, ver

    def sup(self, mgr, **kw):
        return SUP.Supervisor(self.reg, mgr, alive=lambda pid: pid in self.alive_pids, clock=lambda: self.now[0], **kw)

    def test_candidates_start_up_to_the_concurrency_cap(self):
        for n in range(4):
            self.candidate(n)
        mgr = FakeManager(self.reg)
        self.sup(mgr, max_concurrent=2).tick()
        self.assertEqual(len(mgr.started), 2)
        self.assertEqual(len(self.reg.list_versions(status="candidate")), 2)

    def test_dead_paper_run_is_restarted_but_not_in_a_storm(self):
        sid, ver = self.candidate(0)
        mgr = FakeManager(self.reg)
        sup = self.sup(mgr)
        sup.tick()                       # starts (candidate -> paper)
        before = len(mgr.started)
        for _ in range(SUP.MAX_RESTARTS_PER_HOUR + 3):
            sup.tick()                   # process never alive -> restart attempts
            self.now[0] += 10
        restarts = len(mgr.started) - before
        self.assertLessEqual(restarts, SUP.MAX_RESTARTS_PER_HOUR)
        self.now[0] += 4000              # an hour later restarts are allowed again
        n = len(mgr.started)
        sup.tick()
        self.assertGreater(len(mgr.started), n)

    def test_gate_rewritten_every_tick_and_evaluation_periodic(self):
        sid, ver = self.candidate(0)
        mgr = FakeManager(self.reg)
        sup = self.sup(mgr, evaluate_every=3600)
        sup.tick()
        sup.tick()
        self.assertGreaterEqual(len(mgr.gates), 2)
        self.assertEqual(len(mgr.evals), 1)
        self.now[0] += 3601
        sup.tick()
        self.assertEqual(len(mgr.evals), 2)

    def test_running_process_is_not_restarted(self):
        sid, ver = self.candidate(0)
        mgr = FakeManager(self.reg)
        sup = self.sup(mgr)
        sup.tick()
        os.makedirs(P._paths(sid, ver)["dir"], exist_ok=True)
        with open(P._paths(sid, ver)["pid"], "w") as f:
            f.write("4242")
        self.alive_pids.add(4242)
        n = len(mgr.started)
        sup.tick()
        self.assertEqual(len(mgr.started), n)

    def test_process_alive_helper_handles_garbage(self):
        self.assertFalse(SUP.process_alive(None))
        self.assertFalse(SUP.process_alive(0))
        self.assertTrue(SUP.process_alive(os.getpid()))


class ExecutionTests(unittest.TestCase):
    def state(self, **kw):
        return RK.PortfolioState(equity=1000.0, peak_equity=1000.0, start_of_day_equity=1000.0, open_positions=kw.get("positions", []))

    def test_live_venue_cannot_be_constructed(self):
        with self.assertRaises(E.LiveTradingDisabled):
            E.LiveVenue()
        with self.assertRaises(E.LiveTradingDisabled):
            E.LiveVenue(api_key="x", secret="y")

    def test_gateway_refuses_a_live_mode_venue(self):
        class FakeLive(E.ExecutionVenue):
            name, mode = "fakelive", "live"
        with self.assertRaises(E.LiveTradingDisabled):
            E.ExecutionGateway(RK.RiskEngine(), FakeLive())

    def test_strategy_cannot_choose_its_own_mode(self):
        gw = E.ExecutionGateway(RK.RiskEngine(), E.PaperVenue())
        order = RK.OrderRequest("BTC/USDT", "buy", 50, mode="live")  # a strategy trying to claim live
        out = gw.submit(order, self.state(), 100.0)
        self.assertEqual(order.mode, "paper")
        self.assertTrue(out.approved)

    def test_risk_engine_veto_blocks_execution(self):
        audit = []
        gw = E.ExecutionGateway(RK.RiskEngine(), E.PaperVenue(), audit=lambda c, d: audit.append(c))
        out = gw.submit(RK.OrderRequest("BTC/USDT", "buy", 500, strategy_id="S-1"), self.state(), 100.0)  # 50% > 10% position cap
        self.assertFalse(out.approved)
        self.assertIsNone(out.fill)
        self.assertEqual(gw.venue.ledger, [])
        self.assertIn("order_rejected", audit)

    def test_halt_blocks_new_entries_through_the_gateway(self):
        engine = RK.RiskEngine()
        engine.halt("test")
        gw = E.ExecutionGateway(engine, E.PaperVenue())
        self.assertFalse(gw.submit(RK.OrderRequest("BTC/USDT", "buy", 20), self.state(), 100.0).approved)

    def test_paper_fill_includes_slippage_and_fee(self):
        venue = E.PaperVenue(fee=0.001, slippage=0.0005)
        gw = E.ExecutionGateway(RK.RiskEngine(), venue)
        buy = gw.submit(RK.OrderRequest("BTC/USDT", "buy", 50), self.state(), 100.0).fill
        sell = gw.submit(RK.OrderRequest("BTC/USDT", "sell", 50), self.state(positions=[{"pair": "BTC/USDT", "value": 50}]), 100.0).fill
        self.assertAlmostEqual(buy.price, 100.05)
        self.assertAlmostEqual(sell.price, 99.95)
        self.assertAlmostEqual(buy.fee, 0.05)


class LiveReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = R.Registry(os.path.join(self.tmp.name, "r.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_in_paper_is_not_ready_and_explains_why(self):
        report = LR.assess(self.reg)
        self.assertFalse(report["ready_for_review"])
        keys = {i.key: i for i in report["items"]}
        self.assertFalse(keys["strategy_in_paper"].ok)
        self.assertFalse(keys["exchange_key_restrictions"].ok)  # human steps can never be auto-passed
        self.assertIn("Nothing here enables trading", report["note"])

    def test_human_items_cannot_be_satisfied_by_the_registry_alone(self):
        name, source, params = S.generate("sma_cross")
        sid, ver, _ = self.reg.create_strategy("h", "template:sma_cross", "m", "1h", params, source)
        for st in ("backtested", "candidate", "paper"):
            self.reg.set_status(sid, ver, st, "x")
        self.reg.record_experiment(sid, ver, "paper_check", {"metrics": {"days": 90, "n_trades": 200, "max_drawdown": 0.05}, "verdict": "CONTINUE"})
        with mock.patch.object(LR, "SIGNOFF_FILE", os.path.join(self.tmp.name, "none.json")), \
                mock.patch.object(LR, "LIVE_MODULE_REVIEW_FILE", os.path.join(self.tmp.name, "none2.json")):
            report = LR.assess(self.reg)
        keys = {i.key: i for i in report["items"]}
        self.assertTrue(keys["paper_duration"].ok and keys["paper_trade_count"].ok and keys["paper_matches_backtest"].ok)
        self.assertFalse(report["ready_for_review"])
        self.assertFalse(keys["human_signoff"].ok)


if __name__ == "__main__":
    unittest.main()
