"""
Predefined research criteria and kill conditions.

The thresholds are fixed BEFORE any strategy is judged, live in one frozen,
fingerprinted object, and are stored with every verdict so a result can
always be traced to the exact rules that produced it. Nothing here is tuned
to make a particular strategy pass.

evaluate_backtest() returns a Verdict:
    REJECTED    at least one kill condition fired ("REJECTED -- reason X")
    INCOMPLETE  no kill condition fired, but required evidence is missing
                (a strategy can never pass by simply not being tested)
    CANDIDATE   every required test ran and every gate passed; eligible for
                paper trading, which proves nothing about profitability yet

evaluate_paper() compares paper results with what the backtest led us to
expect and can retire (REJECT) a strategy that degrades.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ResearchCriteria:
    version: str = "v1"
    # sample size
    min_trades_in_sample: int = 100
    min_trades_oos: int = 30
    # risk
    max_drawdown: float = 0.25
    max_leverage: float = 1.0
    # out-of-sample quality
    min_oos_sharpe: float = 0.5
    min_oos_profit_factor: float = 1.15
    max_oos_degradation: float = 0.50        # OOS Sharpe may not fall more than this fraction below in-sample
    # costs
    max_fee_share_of_gross_profit: float = 0.50
    # "too good to be true" tripwires -> reject pending investigation
    suspicious_sharpe: float = 3.0
    suspicious_annualised_return: float = 5.0   # +500% a year
    suspicious_win_rate: float = 0.90
    # robustness
    max_neighbour_fail_fraction: float = 0.40   # share of nearby parameter sets that lose money
    min_regimes_profitable_fraction: float = 0.50
    # statistics (family-wise error control across everything the lab tests)
    alpha: float = 0.05
    max_ruin_probability: float = 0.05
    # paper trading
    max_paper_degradation: float = 0.50
    max_paper_drawdown: float = 0.20
    min_paper_days: int = 14

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class Verdict:
    status: str                 # REJECTED | INCOMPLETE | CANDIDATE | CONTINUE (paper)
    reasons: list = field(default_factory=list)   # [(code, human text)]
    criteria_fingerprint: str = ""

    def codes(self):
        return [c for c, _ in self.reasons]

    def summary(self):
        if self.status == "REJECTED":
            return "REJECTED -- " + "; ".join(text for _, text in self.reasons)
        if self.status == "INCOMPLETE":
            return "INCOMPLETE -- missing: " + "; ".join(text for _, text in self.reasons)
        return self.status


def _get(d, *path, default=None):
    for key in path:
        if not isinstance(d, dict) or key not in d or d[key] is None:
            return default
        d = d[key]
    return d


def evaluate_backtest(evidence, criteria=None):
    c = criteria or ResearchCriteria()
    kills, missing = [], []
    ins = _get(evidence, "in_sample", default={})
    oos = _get(evidence, "out_of_sample", default={})

    # ---- required evidence: a missing test is never a pass
    for key, label in (("in_sample", "in-sample backtest"), ("out_of_sample", "out-of-sample backtest"),
                       ("lookahead_clean", "lookahead/data-leakage check"),
                       ("robustness", "parameter robustness test"), ("regimes", "market-regime test"),
                       ("monte_carlo", "Monte Carlo / statistical test")):
        if evidence.get(key) is None:
            missing.append((f"MISSING_{key.upper()}", label))

    # ---- hard kill conditions
    if evidence.get("lookahead_clean") is False:
        kills.append(("LOOKAHEAD_BIAS", "lookahead or data leakage detected"))
    if (evidence.get("leverage") or 1.0) > c.max_leverage:
        kills.append(("LEVERAGE", f"uses leverage {evidence['leverage']}x (limit {c.max_leverage}x)"))

    if ins:
        if (ins.get("n_trades") or 0) < c.min_trades_in_sample:
            kills.append(("TOO_FEW_TRADES", f"only {ins.get('n_trades', 0)} in-sample trades (need {c.min_trades_in_sample}); result is not statistically meaningful"))
        if (ins.get("max_drawdown") or 0) > c.max_drawdown:
            kills.append(("EXCESSIVE_DRAWDOWN", f"in-sample drawdown {ins['max_drawdown']:.0%} exceeds {c.max_drawdown:.0%}"))
    if oos:
        if (oos.get("n_trades") or 0) < c.min_trades_oos:
            kills.append(("TOO_FEW_OOS_TRADES", f"only {oos.get('n_trades', 0)} out-of-sample trades (need {c.min_trades_oos})"))
        if (oos.get("max_drawdown") or 0) > c.max_drawdown:
            kills.append(("EXCESSIVE_OOS_DRAWDOWN", f"out-of-sample drawdown {oos['max_drawdown']:.0%} exceeds {c.max_drawdown:.0%}"))
        oos_sharpe = oos.get("sharpe")
        if oos_sharpe is None or oos_sharpe < c.min_oos_sharpe:
            kills.append(("POOR_OOS_PERFORMANCE", f"out-of-sample Sharpe {oos_sharpe if oos_sharpe is None else round(oos_sharpe, 2)} below {c.min_oos_sharpe}"))
        pf = oos.get("profit_factor")
        if pf is None or pf < c.min_oos_profit_factor:
            kills.append(("LOW_OOS_PROFIT_FACTOR", f"out-of-sample profit factor {pf if pf is None else round(pf, 2)} below {c.min_oos_profit_factor}"))
        is_sharpe = ins.get("sharpe") if ins else None
        if is_sharpe and is_sharpe > 0 and oos_sharpe is not None:
            drop = (is_sharpe - oos_sharpe) / is_sharpe
            if drop > c.max_oos_degradation:
                kills.append(("OOS_DEGRADATION", f"out-of-sample Sharpe fell {drop:.0%} from in-sample (limit {c.max_oos_degradation:.0%}); likely overfit"))

    for name, m in (("in-sample", ins), ("out-of-sample", oos)):
        if not m:
            continue
        fee_share = m.get("fees_pct_of_gross_profit")
        if fee_share is not None and fee_share > c.max_fee_share_of_gross_profit:
            kills.append(("EXCESSIVE_COSTS", f"{name} fees consume {fee_share:.0%} of gross profit (limit {c.max_fee_share_of_gross_profit:.0%})"))
        if (m.get("sharpe") or 0) > c.suspicious_sharpe:
            kills.append(("SUSPICIOUSLY_HIGH_PERFORMANCE", f"{name} Sharpe {m['sharpe']:.1f} is implausibly high (> {c.suspicious_sharpe}); treat as a bug or leak until proven otherwise"))
        if (m.get("annualised_return") or 0) > c.suspicious_annualised_return:
            kills.append(("SUSPICIOUSLY_HIGH_PERFORMANCE", f"{name} annualised return {m['annualised_return']:.0%} is implausibly high"))
        if (m.get("win_rate") or 0) > c.suspicious_win_rate and (m.get("n_trades") or 0) >= 20:
            kills.append(("SUSPICIOUSLY_HIGH_PERFORMANCE", f"{name} win rate {m['win_rate']:.0%} is implausibly high"))

    rob = evidence.get("robustness")
    if rob is not None and (rob.get("neighbour_fail_fraction") or 0) > c.max_neighbour_fail_fraction:
        kills.append(("UNSTABLE_PARAMETERS", f"{rob['neighbour_fail_fraction']:.0%} of nearby parameter sets lose money (limit {c.max_neighbour_fail_fraction:.0%})"))

    regimes = evidence.get("regimes")
    if regimes:
        profitable = sum(1 for r in regimes.values() if (r.get("total_return") or 0) > 0)
        if profitable / len(regimes) < c.min_regimes_profitable_fraction:
            kills.append(("REGIME_DEPENDENT", f"profitable in only {profitable} of {len(regimes)} market regimes"))

    mc = evidence.get("monte_carlo")
    if mc is not None:
        tests = max(1, int(evidence.get("n_strategies_tested") or 1))
        adjusted_alpha = c.alpha / tests
        p = mc.get("p_value")
        if p is None or p > adjusted_alpha:
            kills.append(("NOT_SIGNIFICANT", f"p-value {p if p is None else round(p, 4)} fails the multiple-testing-adjusted threshold {adjusted_alpha:.4f} ({tests} strategies tested)"))
        if (mc.get("ruin_probability") or 0) > c.max_ruin_probability:
            kills.append(("RUIN_RISK", f"simulated ruin probability {mc['ruin_probability']:.1%} exceeds {c.max_ruin_probability:.0%}"))

    if kills:
        return Verdict("REJECTED", kills, c.fingerprint())
    if missing:
        return Verdict("INCOMPLETE", missing, c.fingerprint())
    return Verdict("CANDIDATE", [("PASSED_ALL_GATES", "passed every predefined research gate")], c.fingerprint())


def evaluate_paper(paper, expected, criteria=None):
    """
    paper / expected: metric dicts. `expected` is what the out-of-sample backtest
    implied (daily return, Sharpe). Returns CONTINUE, REJECTED, or INCOMPLETE
    (too little paper time to judge).
    """
    c = criteria or ResearchCriteria()
    kills = []
    if (paper.get("max_drawdown") or 0) > c.max_paper_drawdown:
        kills.append(("PAPER_DRAWDOWN", f"paper drawdown {paper['max_drawdown']:.0%} exceeds {c.max_paper_drawdown:.0%}"))
    if (paper.get("days") or 0) >= c.min_paper_days:
        exp = expected.get("avg_daily_return")
        got = paper.get("avg_daily_return")
        if exp and exp > 0 and got is not None:
            shortfall = (exp - got) / exp
            if shortfall > c.max_paper_degradation:
                kills.append(("BACKTEST_PAPER_DIVERGENCE", f"paper daily return is {shortfall:.0%} below what the backtest implied (limit {c.max_paper_degradation:.0%})"))
        if (paper.get("n_trades") or 0) == 0:
            kills.append(("NO_ACTIVITY", "no paper trades in the observation window; abnormal behaviour"))
        exp_trades = expected.get("trades_per_day")
        if exp_trades and paper.get("days"):
            ratio = (paper.get("n_trades", 0) / paper["days"]) / exp_trades
            if ratio > 3.0 or ratio < 0.2:
                kills.append(("ABNORMAL_TRADE_RATE", f"trading {ratio:.1f}x as often as the backtest implied"))
    if kills:
        return Verdict("REJECTED", kills, c.fingerprint())
    if (paper.get("days") or 0) < c.min_paper_days:
        return Verdict("INCOMPLETE", [("PAPER_TOO_SHORT", f"only {paper.get('days', 0):.0f} paper days (need {c.min_paper_days})")], c.fingerprint())
    return Verdict("CONTINUE", [("PAPER_OK", "paper results consistent with the backtest so far")], c.fingerprint())
