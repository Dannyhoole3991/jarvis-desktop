"""
Live risk limits and the live Risk Engine.

LiveLimits are frozen data with HARD CEILINGS enforced in code: whatever a
config file says, leverage cannot exceed 1x, a single position cannot exceed
the whole capital, and so on. Their fingerprint is recorded when live trading
is armed, and the trader refuses to run if the limits it loads differ.

The live engine is a RiskEngine subclass that:
  - accepts only mode "live" (or "rehearsal" for the dry-run rehearsal), never paper/backtest orders by mistake;
  - vetoes any entry when capital is not configured, the order is below the exchange minimum, the market data is
    stale, the exchange API is failing, an emergency stop is latched, or the daily order cap is reached;
  - always allows reducing orders (so an emergency stop can always sell).
"""

import hashlib
import json
from dataclasses import asdict, dataclass

from ..risk import Decision, RiskEngine, RiskLimits
from ..universe import UNIVERSE

# Hard ceilings: no configuration can exceed these.
HARD_MAX_LEVERAGE = 1.0
HARD_MAX_POSITION_PCT = 1.0
HARD_MAX_EXPOSURE_PCT = 1.0
HARD_MAX_OPEN_POSITIONS = 5
HARD_MAX_DAILY_LOSS = 0.25
HARD_MAX_DRAWDOWN_HALT = 0.50


class LimitsError(ValueError):
    pass


@dataclass(frozen=True)
class LiveLimits(RiskLimits):
    # overrides of the research defaults, sized for a small real account (an exchange minimum order is a large share of it)
    max_position_pct: float = 0.50
    max_total_exposure_pct: float = 0.95
    max_open_positions: int = 2
    max_leverage: float = 1.0
    daily_loss_limit_pct: float = 0.05
    max_drawdown_halt_pct: float = 0.20
    allowed_pairs: tuple = ("BTC/USDT", "ETH/USDT")
    # live-only
    capital_limit: float = 0.0            # quote currency (USDT) the strategy may use; 0 = not configured = nothing may trade
    min_order_value: float = 5.25         # Binance spot minimum is 5 USDT (+5% buffer); preflight re-checks the real value
    stale_data_seconds: int = 900         # price feed older than this vetoes entries
    max_api_failures: int = 5             # consecutive exchange/API failures before entries are vetoed
    emergency_api_failures: int = 20      # consecutive failures before an emergency stop
    max_orders_per_day: int = 6           # runaway / duplicate protection
    gate_max_age_seconds: int = 180       # the strategy ignores a gate file older than this (watchdog death => no entries)

    def __post_init__(self):
        problems = []
        if self.max_leverage > HARD_MAX_LEVERAGE:
            problems.append(f"leverage above {HARD_MAX_LEVERAGE}x is not allowed")
        if not 0 < self.max_position_pct <= HARD_MAX_POSITION_PCT:
            problems.append("max_position_pct must be in (0, 1]")
        if not 0 < self.max_total_exposure_pct <= HARD_MAX_EXPOSURE_PCT:
            problems.append("max_total_exposure_pct must be in (0, 1]")
        if not 1 <= self.max_open_positions <= HARD_MAX_OPEN_POSITIONS:
            problems.append(f"max_open_positions must be 1..{HARD_MAX_OPEN_POSITIONS}")
        if not 0 < self.daily_loss_limit_pct <= HARD_MAX_DAILY_LOSS:
            problems.append(f"daily_loss_limit_pct must be in (0, {HARD_MAX_DAILY_LOSS}]")
        if not 0 < self.max_drawdown_halt_pct <= HARD_MAX_DRAWDOWN_HALT:
            problems.append(f"max_drawdown_halt_pct must be in (0, {HARD_MAX_DRAWDOWN_HALT}]")
        if self.capital_limit < 0:
            problems.append("capital_limit cannot be negative")
        unknown = [p for p in self.allowed_pairs if p not in UNIVERSE]
        if unknown:
            problems.append(f"pairs outside the researched universe are not allowed: {unknown}")
        if problems:
            raise LimitsError("; ".join(problems))

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


class LiveRiskEngine(RiskEngine):
    def __init__(self, limits, state_path=None, rehearsal=False):
        if not isinstance(limits, LiveLimits):
            raise LimitsError("The live engine requires LiveLimits.")
        super().__init__(limits, state_path)
        self.ALLOWED_MODES = ("rehearsal",) if rehearsal else ("live",)
        self.probe_order = max(limits.min_order_value, 1.0)
        self.context = {"data_age_seconds": None, "api_failures": 0, "emergency": False, "orders_today": 0}

    def set_context(self, data_age_seconds, api_failures, emergency, orders_today):
        self.context = {"data_age_seconds": data_age_seconds, "api_failures": api_failures, "emergency": bool(emergency),
                        "orders_today": orders_today}

    def _extra_entry_checks(self, order, state):
        lim, ctx = self.limits, self.context
        if lim.capital_limit <= 0:
            return Decision(False, "no_capital", "No capital limit is configured, so nothing may trade.")
        if ctx["emergency"]:
            return Decision(False, "emergency", "An emergency stop is latched; a human must clear it.")
        if order.value < lim.min_order_value:
            return Decision(False, "below_minimum", f"Order {order.value:.2f} is below the exchange minimum {lim.min_order_value:.2f}.")
        age = ctx["data_age_seconds"]
        if age is None or age > lim.stale_data_seconds:
            return Decision(False, "stale_data", f"Market data is {'unavailable' if age is None else f'{age:.0f}s old'} (limit {lim.stale_data_seconds}s).")
        if ctx["api_failures"] >= lim.max_api_failures:
            return Decision(False, "api_failures", f"{ctx['api_failures']} consecutive exchange/API failures (limit {lim.max_api_failures}).")
        if ctx["orders_today"] >= lim.max_orders_per_day:
            return Decision(False, "order_cap", f"{ctx['orders_today']} orders today (daily cap {lim.max_orders_per_day}).")
        return None

    def gate_snapshot(self, state):
        snap = super().gate_snapshot(state)
        snap["capital_limit"] = self.limits.capital_limit
        snap["ts"] = None  # the trader stamps the time when it writes the file
        return snap
