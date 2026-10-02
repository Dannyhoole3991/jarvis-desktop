import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading.risk import OrderRequest, PortfolioState, RiskEngine, RiskLimits


def state(equity=1000.0, peak=1000.0, start_day=1000.0, positions=None):
    return PortfolioState(equity=equity, peak_equity=peak, start_of_day_equity=start_day,
                          open_positions=positions or [])


def buy(value, pair="BTC/USDT", **kw):
    return OrderRequest(pair=pair, side="buy", value=value, **kw)


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = RiskEngine()

    def test_normal_order_approved(self):
        self.assertTrue(self.engine.evaluate(buy(50), state()).approved)

    def test_live_mode_is_always_rejected(self):
        for mode in ("live", "LIVE", "production", ""):
            d = self.engine.evaluate(buy(10, mode=mode), state())
            self.assertFalse(d.approved, mode)
            self.assertEqual(d.rule, "mode")

    def test_live_rejected_even_for_reducing_sell(self):
        order = OrderRequest("BTC/USDT", "sell", 10, mode="live")
        self.assertFalse(self.engine.evaluate(order, state(positions=[{"pair": "BTC/USDT", "value": 50}])).approved)

    def test_leverage_rejected(self):
        d = self.engine.evaluate(buy(10, leverage=2.0), state())
        self.assertFalse(d.approved)
        self.assertEqual(d.rule, "leverage")

    def test_unknown_pair_rejected(self):
        self.assertFalse(self.engine.evaluate(buy(10, pair="DOGE/USDT"), state()).approved)

    def test_position_size_limit(self):
        self.assertFalse(self.engine.evaluate(buy(101), state()).approved)   # 10% of 1000 = 100
        self.assertTrue(self.engine.evaluate(buy(100), state()).approved)

    def test_position_size_counts_existing_holding(self):
        s = state(positions=[{"pair": "BTC/USDT", "value": 80}])
        self.assertFalse(self.engine.evaluate(buy(30), s).approved)
        self.assertTrue(self.engine.evaluate(buy(20), s).approved)

    def test_total_exposure_limit(self):
        s = state(positions=[{"pair": "ETH/USDT", "value": 95}, {"pair": "BTC/USDT", "value": 95}])
        engine = RiskEngine(RiskLimits(max_total_exposure_pct=0.20, max_position_pct=0.5))
        self.assertFalse(engine.evaluate(buy(20, pair="ETH/USDT"), s).approved)

    def test_max_open_positions(self):
        engine = RiskEngine(RiskLimits(max_open_positions=1, allowed_pairs=("BTC/USDT", "ETH/USDT")))
        s = state(positions=[{"pair": "BTC/USDT", "value": 10}])
        d = engine.evaluate(buy(10, pair="ETH/USDT"), s)
        self.assertFalse(d.approved)
        self.assertEqual(d.rule, "max_positions")
        self.assertTrue(engine.evaluate(buy(10, pair="BTC/USDT"), s).approved)  # adding to an existing one is fine

    def test_daily_loss_limit(self):
        d = self.engine.evaluate(buy(10), state(equity=960.0, start_day=1000.0))
        self.assertFalse(d.approved)
        self.assertEqual(d.rule, "daily_loss")

    def test_drawdown_triggers_persistent_halt(self):
        d = self.engine.evaluate(buy(10), state(equity=840.0, peak=1000.0, start_day=840.0))
        self.assertFalse(d.approved)
        self.assertTrue(self.engine.halted)
        # recovered equity does NOT lift the halt: only a human can
        d2 = self.engine.evaluate(buy(10), state(equity=1000.0, peak=1000.0))
        self.assertFalse(d2.approved)
        self.assertEqual(d2.rule, "halted")

    def test_halt_still_allows_reducing_exposure(self):
        self.engine.halt("test")
        order = OrderRequest("BTC/USDT", "sell", 40)
        d = self.engine.evaluate(order, state(positions=[{"pair": "BTC/USDT", "value": 50}]))
        self.assertTrue(d.approved)
        self.assertEqual(d.rule, "reduce_only")

    def test_short_selling_rejected(self):
        d = self.engine.evaluate(OrderRequest("BTC/USDT", "sell", 40), state())
        self.assertFalse(d.approved)
        self.assertEqual(d.rule, "short")

    def test_halt_persists_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "risk_state.json")
            first = RiskEngine(state_path=path)
            first.halt("drawdown")
            second = RiskEngine(state_path=path)
            self.assertTrue(second.halted)
            second.clear_halt()
            self.assertFalse(RiskEngine(state_path=path).halted)

    def test_bad_inputs_rejected(self):
        self.assertFalse(self.engine.evaluate(buy(0), state()).approved)
        self.assertFalse(self.engine.evaluate(buy(-5), state()).approved)
        self.assertFalse(self.engine.evaluate(OrderRequest("BTC/USDT", "hold", 5), state()).approved)

    def test_limits_fingerprint_changes_when_limits_change(self):
        self.assertNotEqual(RiskLimits().fingerprint(), RiskLimits(max_position_pct=0.5).fingerprint())

    def test_order_cannot_loosen_limits(self):
        # Nothing on OrderRequest can influence the limits; the dataclass is frozen.
        with self.assertRaises(Exception):
            RiskLimits().max_leverage = 10

    def test_gate_snapshot_reflects_halt(self):
        self.engine.halt("x")
        snap = self.engine.gate_snapshot(state())
        self.assertFalse(snap["allow_entries"])
        self.assertTrue(snap["halted"])

    def test_gate_denies_when_total_exposure_is_full(self):
        full = PortfolioState(equity=1000.0, peak_equity=1000.0, start_of_day_equity=1000.0,
                              open_positions=[{"pair": "BTC/USDT", "value": 250.0}, {"pair": "ETH/USDT", "value": 250.0}])
        snap = self.engine.gate_snapshot(full)
        self.assertFalse(snap["allow_entries"])
        self.assertEqual(snap["max_stake"], 0.0)

    def test_gate_denies_when_every_pair_is_at_its_position_cap(self):
        capped = PortfolioState(equity=1000.0, peak_equity=1000.0, start_of_day_equity=1000.0,
                                open_positions=[{"pair": "BTC/USDT", "value": 100.0}, {"pair": "ETH/USDT", "value": 100.0}])
        two = RiskEngine(RiskLimits(allowed_pairs=("BTC/USDT", "ETH/USDT")))
        self.assertFalse(two.gate_snapshot(capped)["allow_entries"])

    def test_gate_allows_a_pair_that_still_has_room(self):
        partial = PortfolioState(equity=1000.0, peak_equity=1000.0, start_of_day_equity=1000.0,
                                 open_positions=[{"pair": "BTC/USDT", "value": 100.0}])
        self.assertTrue(self.engine.gate_snapshot(partial)["allow_entries"])

    def test_gate_snapshot_max_stake(self):
        snap = self.engine.gate_snapshot(state(equity=1000.0))
        self.assertTrue(snap["allow_entries"])
        self.assertAlmostEqual(snap["max_stake"], 100.0)


if __name__ == "__main__":
    unittest.main()
