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

## Stage 2: expanded research, continuous operation (2026-10-02)

### Strategy families (all deterministic, long-only, spot, unleveraged, every entry passes the Risk Engine gate)
| Family | Idea | Engine |
|---|---|---|
| sma_cross, rsi_reversion, bollinger_reversion, donchian_breakout, momentum_roc | the original five | Freqtrade |
| mtf_pullback | daily-trend filter (informative 1d) + RSI dip on 1h/4h | Freqtrade |
| mtf_trend_breakout | Donchian breakout gated by daily trend and daily momentum | Freqtrade |
| vol_regime_trend | MA cross only in a low- or high-volatility regime (ATR percentile) | Freqtrade |
| trend_momentum | price > trend MA, ROC > threshold, ADX > threshold | Freqtrade |
| meanrev_combo | RSI oversold + below lower Bollinger band + above long MA | Freqtrade |
| breakout_volume | breakout with abnormal volume (volume z-score) | Freqtrade |
| obv_trend | on-balance-volume trend confirmation | Freqtrade |
| signal_vote | k-of-n agreement of trend / momentum / breakout / volume / calm-volatility signals | Freqtrade |
| ml_walkforward | gradient-boosted classifier, retrained monthly on rolling/expanding windows | own simulator (`mlrunner`) |

Adaptive sizing (`sizing = inv_vol`) can only *shrink* a position (inverse volatility, encoded in the entry tag so it is
vectorised from past candles) and is further capped by the Risk Engine's `max_stake`. Controls that deliberately cheat
(`lookahead_control`, `mtf_leak_control`, `sizing_leak_control`, `ml_leak_control`) exist to prove each leakage channel is policed.

**Not tested, and why:** true order-flow features (taker buy/sell volume, order book) are not available in the public OHLCV
data used here, so only volume-derived features are tested. FreqAI was not used: its leakage handling is not covered by
Freqtrade's lookahead detector; the ML family instead runs in a lab-owned walk-forward simulator with its own causality tests.
GPU: the machine has an AMD GPU without CUDA, so ML runs on CPU (scikit-learn, BSD-3).

### Universe
9 pairs fixed by an ex-ante rule (largest early-2019 coins with a Binance USDT pair): BTC, ETH, XRP, BCH, LTC, BNB, ADA, XLM, TRX.
**Survivorship bias:** coins large in 2019 that were since delisted or collapsed (e.g. EOS) cannot be tested, which flatters
results. It can only increase false positives, so no gate was relaxed because of it.

### Pipeline additions (gates are unchanged or stricter)
- Static leakage lint (`lint.py`) runs before the dynamic detector. **Found:** Freqtrade's lookahead-analysis cannot see a
  one-day leak through an informative timeframe (the daily candle on the truncation boundary is loaded complete). Since all code
  is template-generated, the source is also checked for any forward-looking construct; both checks must pass.
- Inconclusive leakage checks are retried on 3 progressively longer windows, then treated as failure.
- Near-duplicate detection: strategies whose (pair, entry day, size tag) overlap an earlier one by >= 80% are rejected as NEAR_DUPLICATE.
- Early gates: in-sample trade count / drawdown / cost / plausibility are checked before the expensive robustness sweep;
  robustness + regime failure rejects **before the held-out period is touched**. The sweep is a deterministic sample of at most 8
  neighbours with fail-fast; every neighbour run still counts as a trial.
- Significance: p-value is the larger of the per-trade and per-day-aggregated sign-flip tests (trades across pairs are correlated).
  Beyond Monte Carlo resolution a rigorous Hoeffding upper bound is used. The bar is Bonferroni: alpha / (all parameter sets ever tried).
- Resume safety: an interrupted run restarts from the beginning, **except** if it had already consumed the held-out period, in which
  case the version is retired (INTERRUPTED_AFTER_TEST) rather than re-tested on the same data.

### Parallel research
`batch.py` runs strategies in worker processes (default 6 of 16 threads, below-normal priority). Every Freqtrade run has a private
config/results directory; the registry serialises id allocation (`BEGIN IMMEDIATE`); each concurrent worker adds an assumed trial.
`daemon.py` loops batches with new seeds, supervises paper trading in a thread, and stops at a **trial budget** (default 6000),
because every extra trial makes the next significance bar stricter. A `STOP_RESEARCH` file stops it gracefully.

### Experiment tracking (`tracking.py`, dashboard, HUD Trading tab, chat)
Funnel (registered -> leakage-clean -> backtested -> early gates -> development gates -> candidate -> paper), rejection reasons
by code and family, leaderboard ranked by **out-of-sample Sharpe minus the Sharpe luck alone would produce as the best of N tries**
(never raw profit), per-strategy scorecards (development vs out-of-sample vs paper, drawdown, fee share, parameter instability,
p-value vs required p), and a statistical-power readout: the smallest true Sharpe the held-out window can confirm at the current trial count.
Strategies that fail *only* the significance test are listed as near-misses; they are **not** candidates and are not paper traded.

### Paper trading (`paper.py`, `supervisor.py`)
Candidates are started automatically in Freqtrade dry-run against live Binance data (public endpoints, no keys), up to 3 at once.
The supervisor restarts dead runs (storm-limited), rewrites each run's Risk Engine gate file every minute from real paper-account state,
and hourly compares paper vs backtest expectation (daily return, drawdown, trade rate, activity), rejecting and stopping degraded runs.
Any leveraged or short paper trade rejects the run. The ML family cannot reach paper trading yet (it does not run in Freqtrade).

### Execution layer and the live pathway (`execution.py`, `live_readiness.py`)
`strategy -> OrderRequest -> RiskEngine.evaluate -> ExecutionVenue.place` is the single pipeline. `PaperVenue` simulates fills with fee
and slippage. `LiveVenue` is a placeholder that **cannot be constructed**; `ExecutionGateway` refuses any venue whose mode the Risk
Engine does not allow, and the strategy cannot choose its own mode. A future, separately reviewed live module would implement the
same `ExecutionVenue` interface and go through the same gateway and veto. `live_readiness.assess()` is an executable checklist
(60+ paper days, 50+ paper trades, paper consistent with backtest, drawdown <= 15%, no risk halts, written human sign-off, reviewed
live module, restricted exchange key, capital cap). It only reports; it cannot enable anything.

### Bugs found and fixed in this stage
1. Significance test could not return p-values below 1/(n_perm+1), yet the Bonferroni bar falls below that after ~200 trials: nothing could ever pass.
2. `gate_snapshot` allowed entries when exposure was already full (fail-open).
3. Freqtrade's lookahead detector misses informative-timeframe leaks (static lint added).
4. Parallel runs shared one config/results directory (race); registry id allocation was racy across processes.
5. Batch runner crashed with `CancelledError` when the stop flag cancelled pending jobs.
6. Inconclusive leakage verdicts for low-frequency strategies (window too short).
7. Paper reader used column names that do not exist in Freqtrade's real trades table (found only by running a live dry-run wiring test).
8. Freqtrade `trade` boots STOPPED with no API/Telegram, so a paper run would never have traded (`initial_state: running`).
9. A per-trade significance test overstated evidence for correlated cross-pair trades (per-day test added).

## Stage 3: the finished product - LIVE trader + continuous RESEARCH, separated (2026-10-03)

```
                    JARVIS (chat / HUD Trading section / engine endpoints)
                       |
          +------------+-------------+
          |                          |
     LIVE TRADER                RESEARCH LAB (daemon, always running)
  jarvis_trading/live/          lab, batch, daemon, supervisor, paper
  frozen bundle, 1 strategy     RESEARCH -> gates -> PAPER/CHALLENGER
          |                          |
          |                    promotion.review()  (read-only: flags, never promotes)
          |                          |
          +--------- PROMOTION GATE -+
                     live/approval.py  (human-typed confirmation, logged, reversible)
```

### Three strategy states (registry statuses)
- **RESEARCH**: hypothesis / generated / backtested / candidate. No access to execution of any kind.
- **PAPER / CHALLENGER**: `paper`. Freqtrade dry-run on live market data with simulated money; supervised, compared with the backtest and (via `promotion.review`) with the live strategy.
- **LIVE**: `live`. At most one version. Unreachable through `set_status`; only `promote_to_live` / `rollback_to`, which need an eligible, recent review (or a prior live period) and write `live_history` in the same transaction. `retired` is terminal except through a recorded rollback.

### Promotion (explicit, logged, reversible)
1. `promotion.review` (PromotionCriteria, frozen + fingerprinted, on top of the research gates): still a CANDIDATE under the current research criteria, code regenerates byte-for-byte, >= 60 paper days, >= 50 paper trades, paper verdict CONTINUE, paper drawdown <= 15%, no paper breach ever. To REPLACE a live strategy it must also have >= 60 days of overlapping evidence, a Sharpe margin >= 0.30 over the live strategy's own record (`live_check`, else its paper record) and no materially worse drawdown. It logs "Strategy B is a candidate to replace Strategy A" and the exact phrase to approve. It never changes status.
2. `live/approval.approve_promotion`: you type `PROMOTE <id> v<n> REVIEW <review id>`. The version is frozen into a read-only bundle (`live/bundle.py`), becomes LIVE, any arming is cancelled. **This does not enable trading.**
3. `live/approval.arm`: you type `ENABLE LIVE TRADING <id> v<n> WITH <capital> USDT`. Requires a live strategy, a capital limit, stored credentials, a verified bundle, no emergency latch. Time-limited (default 30 days) and bound to the bundle hash and the limits fingerprint.
4. `start the live trader` (chat/CLI): refuses unless armed and every precondition holds.
5. Rollback (`ROLLBACK TO <id> v<n>`), retire (`RETIRE LIVE STRATEGY`), disarm and emergency stop are always available; disarm and emergency stop need no confirmation. The full history is in `live_history` and `promotion_log` (append-only).

### What stops an experimental strategy touching real money
1. **Import separation (tests/test_separation.py):** no research module imports, even transitively, anything under `jarvis_trading.live`; only `service.py` (which carries your typed commands) does. Only `live/approval.py` may call `promote_to_live` / `rollback_to` / `retire_live`; no non-live module references live paths or credentials.
2. **The research Risk Engine refuses mode "live"**; the live engine (a separate subclass built only inside the live package) refuses everything except "live" (or "rehearsal").
3. **Frozen bundle:** the live strategy is a read-only file whose hash is re-verified at start and every minute; a mismatch triggers an emergency stop. It has no code path to modify itself. The research daemon writes only to the research registry and `trading_lab/` research folders.
4. **Mandatory, fresh gate:** the bundle's live wrapper makes the Risk Engine gate file required and time-stamped; missing, unreadable, halted or older than 180 s (watchdog dead) means no entries.
5. **Limits are frozen data with hard ceilings in code** (leverage 1x, position <= 100%, <= 5 positions, ...). Their fingerprint is bound to the arming; changing capital/limits requires disarm + re-arm. Research code cannot construct `LiveLimits` or touch the arming file.
6. **No arming file => no real order path.** Default state is disarmed; promotion alone never enables trading.

### Live safety features (spec -> implementation)
| Requirement | Where |
|---|---|
| exchange connection | Freqtrade (GPL, separate process) + independent ccxt client (`live/exchange.py`) |
| secure API credentials | Windows DPAPI, outside the repo, never in a config file or log (`live/creds.py`, `setup_credentials`); keys reach Freqtrade via environment only |
| spot, no leverage | `assert_safe_live_config`, `LiveLimits` ceilings, position sanity check (leverage/short => emergency stop) |
| no withdrawals | the key must have withdrawals disabled, spot only, IP-restricted; the trader verifies via the exchange (Binance `apiRestrictions`) and **fails closed**; no withdraw method exists in the code |
| capital limit | `capital_limit`, Freqtrade `available_capital`, fixed stake, Risk Engine sizing |
| position / daily loss / drawdown limits | live Risk Engine; drawdown halt persists across restarts until a human clears it |
| emergency stop | file / HUD / chat / CLI: stop entries, market force-exit all, verify flat, fall back to direct exchange sells, stop engine, latch, disarm |
| stale data | independent ticker age vs `stale_data_seconds` vetoes entries |
| API failure | circuit breaker: entries vetoed after 5 consecutive failures, emergency stop after 20 |
| duplicate orders | one-position-per-pair sanity check, `DuplicateOrderGuard` client ids for emergency sells, Freqtrade's own trade DB |
| order reconciliation | engine's open trades vs real exchange balances every 5 min; shortfall halts entries |
| restart recovery | Freqtrade trade DB + persisted peak/day baseline + persistent halt/latch files; wind-down on disarm |
| exchange-side protection | `stoploss_on_exchange` (Binance spot: limit stops only) keeps positions protected if the PC goes offline |
| complete trade logging | append-only `live_trades.jsonl` and `live_events.jsonl` (credentials redacted), plus Freqtrade DB |

### Rehearsal mode (the whole path without real money)
`python -m jarvis_trading.live.cli rehearse --test-emergency` runs the **same** trader and watchdog code with `dry_run` on and no keys. It passed against the real Freqtrade REST API (15 healthy loops, fresh gate, real Binance price feed, emergency stop executed, no orphan processes) and writes `rehearsal_ok.json`, which the readiness checklist requires.

### Small-account feasibility (public Binance data, 2026-10-03)
Binance spot minimum order: 5 USDT (BTC/USDT, ETH/USDT). GBP 10 = about 11.8 USDT, so at most two minimum-size positions (about 45% of the account each); `max_position_pct` is 50% in the live limits for that reason. Fees are 0.10% per side, a larger share of a tiny order. Stop-loss orders must also meet the minimum. Run `cli feasibility --capital 10 --currency GBP` for current numbers.

### Market regime tracking (`regime.py`)
Trend (bull/bear/chop), volatility class, volume class, sideways detection and regime-change events with hysteresis (major = trend flip or low<->high volatility). Labels use past data only (tested: future data never changes an earlier label), are logged by the daemon, and are **informational only**: never used to tune, filter or switch the live strategy.

### Honest limits of what has been verified
- The real-money order path (Freqtrade with live keys, ccxt private calls such as `apiRestrictions`, exchange-side stop orders, fills) has **never been exercised against a real account**. It is covered by unit tests with simulated exchanges and by the dry-run rehearsal through the real Freqtrade API. One live-only defect (Binance supports only limit stop orders) was found by reading Freqtrade's source, not by running it; there may be others.
- Because it moves real money, `jarvis_trading/live` deserves an independent code review before it is armed (a checklist item).
- Exchange availability/regulation for your country is your decision; research data and fee assumptions are Binance-based.
- The ML family cannot be deployed live (it runs in the lab's own simulator).
- No strategy has yet earned promotion, so nothing is approved, armed or trading.

## Stage 4: forward-only paper trial of the near-misses (started 2026-10-02T23:16:21Z)

**Question it answers:** how do strategies that nearly passed the historical gates behave on live market data they have never seen?
**What it is not:** a second chance. `criteria.py` (research gates) is unchanged; the near-misses stay `rejected`; nothing about the trial can promote them.

### Selection (fixed rules, existing results only; `forward.select_near_misses`)
A near-miss is a strategy that is REJECTED with **NOT_SIGNIFICANT as its only reason**. From those: p-value <= 0.10, out-of-sample Sharpe >= 0.80, out-of-sample drawdown <= 25%, <= 25% of nearby parameter sets losing money, not ML (cannot run in Freqtrade). Ranked by the leaderboard's luck-adjusted score (equivalent to out-of-sample Sharpe at a fixed trial count), at most 2 per family, and no two selected strategies may enter more than 50% of the same trades (behavioural overlap). 26 qualified; 5 selected: S-0007 (trend_momentum), S-0052 (trend_momentum), S-0182 (mtf_trend_breakout), S-0272 (breakout_volume), S-0010 (mtf_trend_breakout). Every skipped candidate and its reason is stored.

### Freezing and the hard start
At creation, one append-only `forward_trials` row stores: the exact version and parameters, the registry content hash, the SHA-256 of the exact strategy file that runs, the evaluation criteria (`ForwardCriteria`, fingerprinted), the selection rationale, and the hard start epoch. One trial per strategy version, ever (UNIQUE), so there are no re-runs until something passes. The strategy file is a read-only copy (generated source + an inert signal recorder) re-hashed every supervision tick; any change INVALIDATES the trial. Only trades opened after the start timestamp count.

### Isolation: why a forward-trial strategy cannot trade real money or go live
1. Research status stays `rejected` (terminal): `set_status` cannot move it, and `promote_to_live` refuses any version that has a forward trial (checked inside the same transaction).
2. `promotion.review` fails it (`not_a_forward_trial_strategy`); `live.bundle.freeze` refuses it.
3. The trial runs Freqtrade **dry-run only**: `--dry-run` is asserted at launch, `assert_safe_config` forbids keys/leverage/live, exchange key environment variables are stripped. It uses the research Risk Engine, which refuses mode "live".
4. `forward.py` is research-side: the separation tests prove it (and everything it imports) cannot reach `jarvis_trading.live`.

### What is recorded (hash-chained ledger + registry log)
Every entry/exit **signal** (via the recorder, including signals that did not become trades), every hypothetical **entry** and **exit** (price, amount, stake, exchange fee, assumed 0.05% slippage per side, slippage-adjusted price and P&L, exit reason), the **market regime** at each event, and timestamps. Freqtrade dry-run models the exchange fee but no slippage, so all results are reported after the same slippage assumption as the backtests (raw figures are kept too). The ledger is hash-chained (editing/deleting any line breaks every later hash); its head is committed to the append-only `forward_log` at every check, so truncation or rewriting is detectable (`forward audit`).

### Evaluation (pre-registered; stored with the trial)
Runs to the horizon: at least 90 days AND 20 trades (hard end at 180 days; fewer than 20 trades then = INCONCLUSIVE). Outcome describes the *historical evidence only*:
- **SUPPORTS**: net return > 0 after fees and slippage, profit factor >= 1, forward Sharpe >= 50% of the out-of-sample Sharpe.
- **CONTRADICTS**: net return <= 0, or Sharpe < 25% of out-of-sample, or profit factor < 0.9.
- otherwise INCONCLUSIVE.
Compared with: the original backtest (training/validation), the out-of-sample results, and an equal-weight buy-and-hold of the universe over the *same* window. A forward p-value (max of per-trade and per-day sign-flip) is reported, Bonferroni-corrected for the 5 strategies in the set. Bad performance is **recorded, never fixed** (drawdown > 20% and losing after >= 10 trades are logged once as breaches; the strategy keeps running unchanged). Exceptional performance (Sharpe >= 2 with >= 30 days and >= 20 trades) is **flagged for human review as a possible bug**, not promoted. At the end `write_report` produces `trading_lab/reports/forward_trial_<set>.md`: does live forward performance support or contradict the historical evidence? Even a SUPPORTS outcome leaves the strategy a non-candidate: it would have to re-enter research as a new version under the unchanged gates.

### Operation
`ForwardSupervisor` runs inside the research daemon: starts/restarts the 5 dry-run processes (storm-limited), rewrites each gate, syncs the ledger every minute, checks hourly, finalises at the horizon. All state is in the registry and `trading_lab/forward/T###_*` folders, so it survives Jarvis and daemon restarts. HUD: "FORWARD PAPER TRIAL" card; chat: "forward trial status"; CLI: `python -m jarvis_trading.forward select|start|status|report|audit`.

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
