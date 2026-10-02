"""
Execution layer: one pipeline for every mode.

    strategy signal -> OrderRequest -> RiskEngine.evaluate -> ExecutionVenue.place

Research and paper trading use `PaperVenue`. A real-money venue would plug into
the SAME gateway and the SAME Risk Engine veto, so nothing about the strategy
or risk pipeline changes when (if) a live module is ever added. That live
module does NOT exist here:

  - `LiveVenue` is a placeholder that refuses to be constructed.
  - The Risk Engine rejects mode "live" unconditionally.
  - Enabling either is a code change that needs a separate human review,
    not a configuration flag. live/readiness.py lists what must be true first.

Nothing in this module talks to an exchange.
"""

import time
from dataclasses import dataclass, field

from . import risk as RK


class LiveTradingDisabled(Exception):
    pass


@dataclass(frozen=True)
class Fill:
    pair: str
    side: str
    value: float          # quote-currency value requested
    price: float          # simulated fill price (includes slippage)
    fee: float
    ts: float
    venue: str


@dataclass
class Outcome:
    approved: bool
    rule: str
    reason: str
    fill: Fill = None


class ExecutionVenue:
    """Interface every venue implements. `mode` must be a mode the Risk Engine understands."""
    name = "abstract"
    mode = "paper"

    def place(self, order, reference_price):
        raise NotImplementedError


class PaperVenue(ExecutionVenue):
    """Simulated fills against a supplied market price, with fee and slippage; keeps a ledger."""
    name = "paper"
    mode = "paper"

    def __init__(self, fee=0.001, slippage=0.0005):
        self.fee = fee
        self.slippage = slippage
        self.ledger = []

    def place(self, order, reference_price):
        direction = 1.0 if order.side == "buy" else -1.0
        price = reference_price * (1.0 + direction * self.slippage)
        fill = Fill(order.pair, order.side, order.value, price, order.value * self.fee, time.time(), self.name)
        self.ledger.append(fill)
        return fill


class LiveVenue(ExecutionVenue):
    """
    Placeholder for a future, separately reviewed real-money venue. It cannot be
    instantiated: there is deliberately no configuration that enables it.
    """
    name = "live"
    mode = "live"

    def __init__(self, *args, **kwargs):
        raise LiveTradingDisabled(
            "Live trading is not implemented or enabled. A real-money venue requires a separately reviewed module "
            "and explicit human authorisation; see jarvis_trading/live (the live trader runs Freqtrade under the live Risk Engine).")


class ExecutionGateway:
    """The single door orders pass through. The Risk Engine can reject anything; a rejection is final."""

    def __init__(self, risk_engine, venue, audit=None):
        if venue.mode not in risk_engine.ALLOWED_MODES:
            raise LiveTradingDisabled(f"Venue mode '{venue.mode}' is not permitted by this Risk Engine (allowed: {', '.join(risk_engine.ALLOWED_MODES)}).")
        self.risk = risk_engine
        self.venue = venue
        self.audit = audit  # optional callable(category, detail)

    def submit(self, order, state, reference_price):
        order.mode = self.venue.mode  # the venue, not the strategy, decides the mode
        decision = self.risk.evaluate(order, state)
        if self.audit:
            self.audit("order_" + ("approved" if decision.approved else "rejected"),
                       f"{order.strategy_id} {order.side} {order.pair} {order.value:.2f}: {decision.rule} - {decision.reason}")
        if not decision.approved:
            return Outcome(False, decision.rule, decision.reason)
        return Outcome(True, decision.rule, decision.reason, self.venue.place(order, reference_price))
