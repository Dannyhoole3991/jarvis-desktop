"""
Performance metrics for the trading research lab.

Pure standard library on purpose: deterministic, dependency-free, and
runnable under any Python Jarvis or the Freqtrade environment uses.

A "trade" is a dict with at least:
    close_ts      epoch seconds or ISO-8601 string (when the trade closed)
    profit_ratio  NET return of the trade as a fraction of its stake
                  (fees already deducted, e.g. 0.012 = +1.2%)
and optionally:
    open_ts, stake (quote currency amount), profit_abs, fee_paid (abs).

Equity is built by applying closed trades in close-time order to a starting
balance. All annualisation uses 365 days (crypto trades every day).
"""

import datetime
import math

DAYS_PER_YEAR = 365


def to_epoch(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _day(epoch):
    return int(epoch // 86400)


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _std(values):
    if len(values) < 2:
        return 0.0
    m = _mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def equity_curve(trades, starting_balance):
    """Returns [(close_epoch, equity)] after each closed trade, in time order."""
    ordered = sorted(trades, key=lambda t: to_epoch(t["close_ts"]))
    equity = float(starting_balance)
    curve = []
    for trade in ordered:
        if trade.get("profit_abs") is not None:
            equity += float(trade["profit_abs"])
        else:
            stake = float(trade.get("stake") or (equity * 0.1))
            equity += stake * float(trade["profit_ratio"])
        curve.append((to_epoch(trade["close_ts"]), equity))
    return curve


def max_drawdown(curve, starting_balance):
    """Largest peak-to-trough fall of the equity curve, as a positive fraction."""
    peak = float(starting_balance)
    worst = 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def longest_losing_streak(trades):
    ordered = sorted(trades, key=lambda t: to_epoch(t["close_ts"]))
    best = run = 0
    for trade in ordered:
        if float(trade["profit_ratio"]) < 0:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def daily_returns(curve, starting_balance, start_epoch=None, end_epoch=None):
    """Equity-based daily returns, with zero-return days filled in so quiet days count."""
    if not curve:
        return []
    by_day = {}
    for epoch, equity in curve:
        by_day[_day(epoch)] = equity
    first = _day(start_epoch) if start_epoch is not None else min(by_day)
    last = _day(end_epoch) if end_epoch is not None else max(by_day)
    returns = []
    previous = float(starting_balance)
    equity = previous
    for day in range(first, last + 1):
        equity = by_day.get(day, equity)
        returns.append((equity - previous) / previous if previous > 0 else 0.0)
        previous = equity
    return returns


def sharpe(daily):
    sd = _std(daily)
    return (_mean(daily) / sd) * math.sqrt(DAYS_PER_YEAR) if sd > 0 else None


def sortino(daily):
    if not daily:
        return None
    downside = [min(0.0, r) for r in daily]
    dd = math.sqrt(sum(d * d for d in downside) / len(daily))
    return (_mean(daily) / dd) * math.sqrt(DAYS_PER_YEAR) if dd > 0 else None


def compute_metrics(trades, starting_balance=1000.0, start_epoch=None, end_epoch=None):
    """Full metric set for one list of closed trades. All ratios are fractions (0.05 = 5%)."""
    starting_balance = float(starting_balance)
    if not trades:
        return {"n_trades": 0, "total_return": 0.0, "win_rate": None, "profit_factor": None,
                "max_drawdown": 0.0, "sharpe": None, "sortino": None, "calmar": None,
                "avg_trade": None, "longest_losing_streak": 0, "fees_total": 0.0,
                "fees_pct_of_gross_profit": None, "turnover": 0.0, "days": 0,
                "avg_daily_return": 0.0, "annualised_return": 0.0}

    curve = equity_curve(trades, starting_balance)
    final = curve[-1][1]
    total_return = final / starting_balance - 1.0

    ratios = [float(t["profit_ratio"]) for t in trades]
    wins = [r for r in ratios if r > 0]
    losses = [r for r in ratios if r < 0]
    stakes = [float(t.get("stake") or 0.0) for t in trades]
    pnl_abs = []
    for t, stake in zip(trades, stakes):
        pnl_abs.append(float(t["profit_abs"]) if t.get("profit_abs") is not None else stake * float(t["profit_ratio"]))
    gross_win = sum(p for p in pnl_abs if p > 0)
    gross_loss = -sum(p for p in pnl_abs if p < 0)
    fees_total = sum(float(t.get("fee_paid") or 0.0) for t in trades)

    first = start_epoch if start_epoch is not None else min(
        to_epoch(t.get("open_ts") or t["close_ts"]) for t in trades)
    last = end_epoch if end_epoch is not None else max(to_epoch(t["close_ts"]) for t in trades)
    days = max(1.0, (last - first) / 86400.0)
    daily = daily_returns(curve, starting_balance, first, last)
    mdd = max_drawdown(curve, starting_balance)
    annualised = (final / starting_balance) ** (DAYS_PER_YEAR / days) - 1.0 if final > 0 else -1.0

    return {
        "n_trades": len(trades),
        "total_return": total_return,
        "annualised_return": annualised,
        "avg_daily_return": _mean(daily),
        "win_rate": len(wins) / len(ratios),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (None if gross_win == 0 else float("inf")),
        "avg_trade": _mean(ratios),
        "max_drawdown": mdd,
        "sharpe": sharpe(daily),
        "sortino": sortino(daily),
        "calmar": (annualised / mdd) if mdd > 0 else None,
        "longest_losing_streak": longest_losing_streak(trades),
        "fees_total": fees_total,
        "fees_pct_of_gross_profit": (fees_total / gross_win) if gross_win > 0 else None,
        "turnover": (sum(stakes) * 2.0) / starting_balance if any(stakes) else 0.0,
        "days": days,
    }
