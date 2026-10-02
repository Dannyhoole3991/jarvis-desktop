"""
Risk Engine for the trading research lab.

Deliberately independent of the strategy generator: it imports nothing
from it, its limits are fixed data (hashed so any change is visible), and
nothing a strategy emits can loosen them. Its only job is to look at a
proposed order plus the current portfolio and answer approve/reject with
a reason. It can reject ANY order. Rejections are final for that order.

Hard rules that are code, not configuration:
  - mode "live" is ALWAYS rejected. No live-trading path exists.
  - leverage above 1.0 is rejected unless a limit explicitly allows it
    (the default does not, and the lab starts with leverage forbidden).
  - once halted (drawdown breach), only orders that reduce exposure are
    approved until a human clears the halt.
"""

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field

from .universe import UNIVERSE

MIN_PROBE_ORDER = 10.0  # quote units; roughly an exchange minimum order, used by gate_snapshot
MODES_ALLOWED = ("backtest", "paper")


@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: float = 0.10        # of current equity, per position
    max_total_exposure_pct: float = 0.50  # sum of open position values / equity
    max_open_positions: int = 5
    max_leverage: float = 1.0
    daily_loss_limit_pct: float = 0.03    # entries stop after losing this much in a day
    max_drawdown_halt_pct: float = 0.15   # from peak equity; triggers a persistent halt
    allowed_pairs: tuple = tuple(UNIVERSE)  # the fixed research universe; per-position and total-exposure limits are unchanged

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class PortfolioState:
    equity: float
    peak_equity: float
    start_of_day_equity: float
    open_positions: list = field(default_factory=list)  # [{"pair": str, "value": float}]

    def exposure(self):
        return sum(float(p["value"]) for p in self.open_positions)


@dataclass
class OrderRequest:
    pair: str
    side: str            # "buy" opens/adds exposure, "sell" reduces it
    value: float         # quote-currency value of the order
    mode: str = "paper"
    leverage: float = 1.0
    strategy_id: str = ""


@dataclass(frozen=True)
class Decision:
    approved: bool
    rule: str
    reason: str


class RiskEngine:
    def __init__(self, limits=None, state_path=None):
        self.limits = limits or RiskLimits()
        self.state_path = state_path
        self.halted = False
        self.halt_reason = ""
        if state_path and os.path.exists(state_path):
            with open(state_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.halted = bool(saved.get("halted"))
            self.halt_reason = saved.get("halt_reason", "")

    def _persist(self):
        if self.state_path:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump({"halted": self.halted, "halt_reason": self.halt_reason,
                           "limits": self.limits.fingerprint()}, f)

    def halt(self, reason):
        self.halted = True
        self.halt_reason = reason
        self._persist()

    def clear_halt(self):
        """Human-only action; nothing in the lab calls this automatically."""
        self.halted = False
        self.halt_reason = ""
        self._persist()

    def evaluate(self, order, state):
        lim = self.limits

        if order.mode not in MODES_ALLOWED:
            return Decision(False, "mode", f"Mode '{order.mode}' is not permitted. Live trading is disabled in this system.")
        if order.side not in ("buy", "sell"):
            return Decision(False, "side", f"Unknown order side '{order.side}'.")
        if order.value <= 0:
            return Decision(False, "value", "Order value must be positive.")
        if order.leverage > lim.max_leverage:
            return Decision(False, "leverage", f"Leverage {order.leverage}x exceeds the {lim.max_leverage}x limit.")
        if order.pair not in lim.allowed_pairs:
            return Decision(False, "pair", f"{order.pair} is not on the allowed list.")

        # Orders that only reduce exposure are always safe to approve, even when halted.
        if order.side == "sell":
            held = sum(float(p["value"]) for p in state.open_positions if p["pair"] == order.pair)
            if order.value <= held * 1.0000001:
                return Decision(True, "reduce_only", "Reduces existing exposure.")
            return Decision(False, "short", "Selling more than is held would open a short, which is not permitted.")

        # From here on the order adds exposure.
        if state.peak_equity > 0:
            drawdown = (state.peak_equity - state.equity) / state.peak_equity
            if drawdown >= lim.max_drawdown_halt_pct and not self.halted:
                self.halt(f"Drawdown {drawdown:.1%} reached the {lim.max_drawdown_halt_pct:.0%} halt limit.")
        if self.halted:
            return Decision(False, "halted", f"Trading halted: {self.halt_reason or 'manual halt'}.")

        if state.start_of_day_equity > 0:
            day_loss = (state.start_of_day_equity - state.equity) / state.start_of_day_equity
            if day_loss >= lim.daily_loss_limit_pct:
                return Decision(False, "daily_loss", f"Down {day_loss:.1%} today, at or past the {lim.daily_loss_limit_pct:.0%} daily limit.")

        if len(state.open_positions) >= lim.max_open_positions and not any(p["pair"] == order.pair for p in state.open_positions):
            return Decision(False, "max_positions", f"Already {len(state.open_positions)} open positions (limit {lim.max_open_positions}).")

        current_in_pair = sum(float(p["value"]) for p in state.open_positions if p["pair"] == order.pair)
        if current_in_pair + order.value > lim.max_position_pct * state.equity + 1e-9:
            return Decision(False, "position_size", f"Position in {order.pair} would be {(current_in_pair + order.value) / state.equity:.1%} of equity (limit {lim.max_position_pct:.0%}).")

        if state.exposure() + order.value > lim.max_total_exposure_pct * state.equity + 1e-9:
            return Decision(False, "exposure", f"Total exposure would be {(state.exposure() + order.value) / state.equity:.1%} of equity (limit {lim.max_total_exposure_pct:.0%}).")

        return Decision(True, "ok", "Within all risk limits.")

    def gate_snapshot(self, state):
        """
        What a trading engine's strategy callback may read before entering a
        trade: a plain allow/deny plus the largest stake the limits permit.
        Written to a file the engine reads; the engine cannot change it.
        """
        lim = self.limits
        # Entries are allowed only if the engine would approve a (tiny) buy in at least one allowed pair.
        approved, reason = False, ""
        for pair in lim.allowed_pairs:
            verdict = self.evaluate(OrderRequest(pair=pair, side="buy", value=MIN_PROBE_ORDER, mode="paper"), state)
            if verdict.approved:
                approved = True
                break
            reason = reason or verdict.reason
        room_position = lim.max_position_pct * state.equity
        room_total = lim.max_total_exposure_pct * state.equity - state.exposure()
        return {
            "allow_entries": approved,
            "reason": "ok" if approved else reason,
            "max_stake": max(0.0, min(room_position, room_total)),
            "halted": self.halted,
            "limits": self.limits.fingerprint(),
        }
