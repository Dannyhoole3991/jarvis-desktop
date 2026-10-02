# Jarvis Trading Research Laboratory

**Purpose:** a rigorous, autonomous research lab that discovers, tests, rejects and paper-trades
strategies, and measures whether any genuine edge survives contact with live markets.

**It does not assume profitability.** No part of it is built around a 3-10% daily return. That claim is
treated as a hypothesis to test (`jarvis_trading/claim.py`), and so far the data rejects it.

## Safety posture (enforced in code, covered by tests)

| Rule | Where enforced |
|---|---|
| Research/backtest first, then paper (dry-run) only. **No live mode exists.** | `risk.MODES_ALLOWED`, `freqtrade_adapter.assert_safe_config` (dry_run must be True), `paper.start` (refuses without `--dry-run`) |
| No real-money credentials | `assert_safe_config` rejects any non-empty exchange key/secret/password/uid; public candle data needs none |
| No leverage, spot only | `assert_safe_config`; generated strategies are long-only with no leverage hooks; Risk Engine max_leverage 1.0 |
| Risk Engine independent of the strategy generator and can reject any trade | `risk.py` knows nothing about strategies. Strategies can only *read* its gate file (`confirm_trade_entry`), and fail closed if it is missing or unreadable. Halts persist until a human clears them |
| Strategy results cannot be silently overwritten | `registry.py` is append-only (SQLite triggers abort UPDATE/DELETE); a changed strategy is a new version; rejected is terminal |
| Pass/fail criteria fixed before results are seen | `criteria.ResearchCriteria` is frozen and fingerprinted; the fingerprint is stored with every verdict |
| Live-trading requests via chat | `service.chat_reply` refuses ("real money", "go live", leverage, exchange keys) |

## Architecture

```
Jarvis (chat / HUD "Trading" tab / engine endpoints)
  |-- service.py            chat phrases, /trading/summary, /trading/run_research
  |-- lab.py                the research pipeline (orchestrator)
  |     |-- strategies.py   Strategy Generator: deterministic template -> Freqtrade strategy source
  |     |-- freqtrade_adapter.py   Backtest Manager (runs Freqtrade as a subprocess)
  |     |-- metrics.py, stats.py   Statistical Analysis (metrics, Monte Carlo, sign-flip test, regimes)
  |     |-- criteria.py     predefined accept/reject/kill rules
  |     '-- registry.py     Strategy Database + experiment/decision log (append-only)
  |-- paper.py              Paper Trading Manager (Freqtrade dry-run, paper-vs-backtest comparison)
  |-- risk.py               Risk Engine (independent veto, writes the gate file)
  |-- claim.py / run_claim.py   the "3-10% per day" experiments
  '-- report.py             dashboard (trading_lab/reports/dashboard.html) + chat summary
                |
                v
          Freqtrade (separate process, own venv)  ->  Binance public data (no keys)
```

## Pipeline per hypothesis (`Lab.research`)

1. Hypothesis -> deterministic strategy code (same inputs give byte-identical source; content hash is its identity)
2. Registered (never overwritten; identical content returns the existing entry)
3. **Lookahead / leakage check** (Freqtrade `lookahead-analysis`). Biased or *inconclusive* = rejected
4. Backtest **training** (2019-2022) and **validation** (2023 to mid-2024) with fees **and slippage**
   (per side 0.10% fee + 0.05% slippage; Freqtrade itself assumes zero slippage, so it is added to `--fee`)
5. Early rejection if it loses money in training or validation - **the held-out test period is not touched**
6. **Parameter robustness**: each parameter nudged +/-25%; too many losing neighbours = rejected
7. **Market regimes** (bull/bear/chop from trailing data only, never the future): must not depend on one regime
8. **Held-out test** (mid-2024 to Sep 2026): run **once** per version; a second run raises an error
9. **Statistics** on held-out trades: sign-flip significance, bootstrap Monte Carlo (ruin probability)
10. **Judgement** by the predefined criteria. The significance bar is divided by the number of parameter sets ever
    tried (multiple-testing correction; neighbours count). Missing evidence = INCOMPLETE, never a pass
11. Survivors become `candidate`; only candidates may be paper traded; every step is logged with a reason

Reproducibility: each experiment stores the data hash, exact command, parameters, random seed and tool versions.

### Kill conditions (all in `criteria.py`, none tuned after seeing results)
leakage; leverage > 1x; too few trades; drawdown > 25%; out-of-sample Sharpe < 0.5 or profit factor < 1.15;
out-of-sample Sharpe falling > 50% from in-sample; fees > 50% of gross profit; unstable parameters;
regime dependence; not significant after correction; ruin probability > 5%; "suspiciously high" results
(Sharpe > 3, annualised return > 500%, win rate > 90%) are treated as bugs/leaks, not wins.
Paper stage: drawdown > 20%, daily return > 50% below the backtest's, no activity, abnormal trade rate.

## What has actually been found so far

Six strategy versions run on BTC/USDT + ETH/USDT, 2019-2026, 1h/4h/1d candles:

- `lookahead_control` (deliberately cheating): **caught** by the leakage check, rejected (proves the detector works)
- `sma_cross`, `donchian_breakout`, `momentum_roc`: positive in sample, **failed out of sample** (Sharpe 0.3 to 0.5, not significant)
- `rsi_reversion`, `bollinger_reversion`: **lost money in training and validation**; rejected without ever touching test data
- **No candidates. Nothing is being paper traded. No edge has been found.**

The 3-10%/day claim (`claim_experiment.json`): 3%/day compounds to roughly 48,000x in a year (doubling every 23 days);
the tested strategies averaged 0.015% to 0.02% per day. BTC/ETH buy-and-hold had a 3%+ day on 12 to 19% of days
but averaged ~0.2%/day. Reaching 3%/day from these return streams needs ~15x to ~200x leverage, which liquidates
the account on the historical path and in 94 to 100% of resampled years.

## Open source reviewed

| Project | Licence | Decision |
|---|---|---|
| **Freqtrade** 2026.9 | **GPL-3.0** | **Used as the engine**, as a separate process in its own venv (`trading_lab/ft_venv`), driven by CLI only. Jarvis never imports it, so no GPL code is linked into Jarvis. Generated strategy files import it (they must) and live under `trading_lab/ft_user_data`. If Jarvis were ever distributed, the GPL implications of those strategy files need a fresh look. |
| Jesse | MIT | Reviewed, not used: a second engine adds no capability here; documented as the alternative if Freqtrade proves limiting. |
| Hummingbot | Apache-2.0 | Reviewed, not used: built around market making/arbitrage and live connectors, a poor fit for a research-first lab. |
| "NewWorldTrading" | unknown | **Unresolved.** The only match found was a game-market OCR tool, not a trading engine. Needs a link from Danny to evaluate. |

Other dependencies: `ccxt` (MIT, via Freqtrade), `pandas`, `numpy`, `TA-Lib` (BSD, inside the venv only). The Jarvis-side
`jarvis_trading` package uses the Python standard library only.

## Conventions worth knowing

- Sharpe/Sortino/Calmar use the lab's own **equity-based daily returns** (quiet days count as zero), not Freqtrade's
  per-trade versions. Total return, profit factor and drawdown agree with Freqtrade's to rounding (cross-checked).
- Candle backtests cannot see intra-candle order; stoploss fills are optimistic in fast moves. Treat results as upper bounds.
- Held-out period was used by the earlier six strategies. With each further strategy the multiple-testing bar rises.
  Do not re-tune on the held-out period; if it ever must be reused, record that in the decision log.

## Running it

```
trading_lab\ft_venv\Scripts\python.exe -m jarvis_trading.run_lab --templates sma_cross rsi_reversion
trading_lab\ft_venv\Scripts\python.exe -m jarvis_trading.run_claim
python -m unittest discover -s jarvis_trading/tests -t .
```
Jarvis: say "trading lab status" or "run trading research"; the HUD has a Trading tab; report at
`trading_lab/reports/dashboard.html` (`python -c "from jarvis_trading import registry, report; print(report.write_report(registry.Registry()))"`).

## Before any step beyond paper trading
Not implemented, deliberately. It would need: a sustained paper run matching the backtest (>= 14 days and many more
trades), a human decision, a separate reviewed live module with its own hard limits, and exchange keys that Danny
supplies and Jarvis never stores in the repo. None of that is built or enabled.
