"""
Pure, testable safety logic for the live trader: stale data, API-failure circuit
breaker, duplicate-order protection, position sanity checks and order
reconciliation. No network access in this module.
"""

import time


class StaleDataGuard:
    """Tracks the age of the newest price the system has seen."""

    def __init__(self, max_age_seconds, clock=time.time):
        self.max_age = max_age_seconds
        self.clock = clock
        self.last_ts = None

    def observe(self, timestamp_seconds):
        if timestamp_seconds is not None and (self.last_ts is None or timestamp_seconds > self.last_ts):
            self.last_ts = timestamp_seconds

    def age(self):
        return None if self.last_ts is None else max(0.0, self.clock() - self.last_ts)

    def is_stale(self):
        age = self.age()
        return age is None or age > self.max_age


class CircuitBreaker:
    """Counts consecutive failures; `tripped` (entries vetoed) and `critical` (emergency stop) at two thresholds."""

    def __init__(self, trip_after, critical_after):
        self.trip_after, self.critical_after = trip_after, critical_after
        self.consecutive = 0
        self.total = 0

    def success(self):
        self.consecutive = 0

    def failure(self):
        self.consecutive += 1
        self.total += 1

    @property
    def tripped(self):
        return self.consecutive >= self.trip_after

    @property
    def critical(self):
        return self.consecutive >= self.critical_after


class DuplicateOrderGuard:
    """
    Refuses to submit the same logical order twice. An order is identified by a client id
    (derived from strategy, pair, side and a time bucket) and by an open order already existing
    for that pair and side.
    """

    def __init__(self, ttl_seconds=3600, clock=time.time):
        self.ttl = ttl_seconds
        self.clock = clock
        self._seen = {}

    @staticmethod
    def client_id(strategy_id, pair, side, bucket):
        return f"jv-{strategy_id}-{pair.replace('/', '')}-{side}-{bucket}"

    def allow(self, client_id, open_orders=()):
        now = self.clock()
        self._seen = {k: t for k, t in self._seen.items() if now - t < self.ttl}
        if client_id in self._seen:
            return False, f"order {client_id} was already submitted"
        for order in open_orders:
            if order.get("client_id") == client_id:
                return False, f"order {client_id} is already open on the exchange"
        self._seen[client_id] = now
        return True, "ok"


def position_sanity(open_trades, limits):
    """Independent re-check of what the engine is actually holding against the live limits. Returns problems (empty = fine)."""
    problems = []
    pairs = [t["pair"] for t in open_trades]
    if len(pairs) != len(set(pairs)):
        problems.append("more than one open trade in the same pair (possible duplicate entry)")
    if len(open_trades) > limits.max_open_positions:
        problems.append(f"{len(open_trades)} open trades exceeds the limit of {limits.max_open_positions}")
    for t in open_trades:
        if t["pair"] not in limits.allowed_pairs:
            problems.append(f"open trade in {t['pair']}, which is not an allowed pair")
        if float(t.get("leverage") or 1.0) > 1.0 or t.get("is_short"):
            problems.append(f"{t['pair']} is leveraged or short, which is forbidden")
    total = sum(float(t.get("stake_amount") or 0.0) for t in open_trades)
    if limits.capital_limit > 0 and total > limits.capital_limit * limits.max_total_exposure_pct * 1.02:
        problems.append(f"open stake {total:.2f} exceeds the exposure limit of {limits.capital_limit * limits.max_total_exposure_pct:.2f}")
    return problems


def reconcile(open_trades, balances, tolerance=0.02):
    """
    Compares what the engine believes it holds (open trades: pair + amount) with what the
    exchange says is in the account (balances: {asset: total}). Returns a list of discrepancies.
    Holdings the engine knows nothing about are reported too (a dedicated account is expected).
    """
    issues = []
    expected = {}
    for t in open_trades:
        base = t["pair"].split("/")[0]
        expected[base] = expected.get(base, 0.0) + float(t.get("amount") or 0.0)
    for base, amount in expected.items():
        held = float(balances.get(base, 0.0))
        if held < amount * (1 - tolerance):
            issues.append({"kind": "missing_on_exchange", "asset": base, "expected": amount, "held": held})
    for asset, held in balances.items():
        if asset in ("USDT", "BUSD", "USDC", "BNB") or float(held) <= 0:
            continue
        if asset not in expected and float(held) * 1.0 > 0:
            issues.append({"kind": "unknown_holding", "asset": asset, "held": float(held)})
    return issues
