"""
The live trader: runs ONE frozen strategy bundle inside Freqtrade (GPL, separate process) and wraps it in an
independent watchdog that owns every safety decision.

Two modes through the SAME code path; they differ only in `dry_run` and credentials:
  rehearsal  - Freqtrade dry-run (simulated money, no keys). Used to prove the whole pipeline end-to-end.
  live       - real orders. Refused unless live trading is explicitly ARMED and every precondition holds.

What the watchdog does every loop (`step`):
  * checks the emergency file / latch, the bundle hash, the arming, the exchange API key's permissions (live);
  * reads Freqtrade's state over its localhost API, and the price feed over an independent exchange client;
  * rebuilds the portfolio state from real positions, runs the live Risk Engine, and writes a fresh,
    time-stamped gate file the strategy MUST read before any entry (stale gate => no entries);
  * enforces stale-data, API-failure, position-sanity, duplicate-position and reconciliation protections;
  * logs every trade, veto, halt and failure to append-only JSONL files;
  * writes status.json for the HUD.

Entries are only ever vetoed; the watchdog never opens a position. Emergency stop = stop entries, force-exit all,
verify flat (falling back to the independent exchange client), stop the bot, latch until a human clears it.
"""

import json
import os
import subprocess
import sys
import time

from .. import freqtrade_adapter as A
from .. import risk as RK
from . import approval, bundle, config, creds, guards, paths
from . import exchange as X
from . import limits as L
from .ftapi import FtApi, FtApiError


def _utc(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else time.time()))


def _atomic_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


class EventLog:
    """Append-only JSONL log. Credentials are redacted from anything written."""

    def __init__(self, path):
        self.path = path

    def write(self, kind, **fields):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        record = {"ts": _utc(), "kind": kind, **fields}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(creds.redact(json.dumps(record, default=str)) + "\n")
        return record

    def tail(self, n=50, kind=None):
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        return [r for r in rows if kind is None or r["kind"] == kind][-n:]


class EngineProcess:
    """The Freqtrade child process."""

    def __init__(self, args, env, log_path):
        self.args, self.env, self.log_path = args, env, log_path
        self.proc = None

    def start(self):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        self.log = open(self.log_path, "ab")
        self.proc = subprocess.Popen(self.args, env=self.env, stdout=self.log, stderr=self.log, cwd=A.LAB_DIR)
        return self.proc.pid

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def terminate(self):
        if self.proc is not None and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True) if sys.platform == "win32" else self.proc.terminate()


class LiveTrader:
    def __init__(self, registry, limits, mode, exchange_client, bundle_folder, manifest, api=None, engine=None,
                 clock=time.time, sleep=time.sleep, settings=None, events=None):
        if mode not in ("rehearsal", "live"):
            raise ValueError("mode must be 'rehearsal' or 'live'")
        self.registry, self.limits, self.mode = registry, limits, mode
        self.rehearsal = mode == "rehearsal"
        self.exchange, self.api, self.engine_proc = exchange_client, api, engine
        self.bundle_folder, self.manifest = bundle_folder, manifest
        self.clock, self.sleep, self.settings = clock, sleep, settings
        self.events = events or EventLog(paths.EVENTS_LOG)
        self.trades_log = EventLog(paths.TRADES_LOG)
        self.risk = L.LiveRiskEngine(limits, state_path=paths.HALT_FILE, rehearsal=self.rehearsal)
        self.stale = guards.StaleDataGuard(limits.stale_data_seconds, clock=clock)
        self.api_breaker = guards.CircuitBreaker(limits.max_api_failures, limits.emergency_api_failures)
        self.exch_breaker = guards.CircuitBreaker(limits.max_api_failures, limits.emergency_api_failures)
        self.dups = guards.DuplicateOrderGuard(clock=clock)
        self.state = "STARTING"
        self.problems = []
        self.last_veto = None
        self.last_trade = None
        self._seen = {}
        self._log_offset = 0
        self._last_reconcile = 0.0
        self._last_integrity = 0.0
        self._persist = self._load_state()
        self._loop = 0
        self.snapshot = {}

    # ------------------------------------------------------------ persistence
    def _load_state(self):
        try:
            with open(paths.STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"peak_equity": None, "day": None, "day_start_equity": None}

    def _save_state(self):
        _atomic_json(paths.STATE_FILE, self._persist)

    # ------------------------------------------------------------- preflight
    def preflight(self):
        """Everything that must hold before the engine is started. Returns problems (empty = go)."""
        problems = []
        ok, bad = bundle.verify(self.bundle_folder)
        if not ok:
            problems += bad
        if os.path.exists(paths.EMERGENCY_LATCH):
            problems.append("an emergency stop is latched")
        if self.limits.capital_limit <= 0:
            problems.append("no capital limit configured")
        if not self.rehearsal:
            problems += approval.arming_problems(self.registry, self.settings)
            problems += X.check_account_safety(self.exchange)
            try:
                balance = self.exchange.fetch_balance().get("USDT", 0.0)
                if balance + 1e-9 < self.limits.capital_limit:
                    problems.append(f"the account holds {balance:.2f} USDT, less than the configured capital {self.limits.capital_limit:.2f}")
                for pair in self.limits.allowed_pairs:
                    minimum = self.exchange.market_limits(pair)["min_cost"]
                    if config.stake_per_trade(self.limits) < minimum:
                        problems.append(f"a stake of {config.stake_per_trade(self.limits):.2f} USDT is below {pair}'s exchange minimum of {minimum:.2f}")
            except X.ExchangeError as error:
                problems.append(f"could not check the account: {error}")
        return problems

    # -------------------------------------------------------------- engine
    def build_engine(self, port, username, password):
        timeframe = self.manifest["parameters"]["timeframe"]
        db_url = "sqlite:///" + os.path.join(paths.LIVE_DIR, f"trades_{self.mode}.sqlite").replace("\\", "/")
        cfg = config.build_freqtrade_config(self.limits, timeframe, self.rehearsal, db_url, port, username, password,
                                            (self.settings or {}).get("exchange", "binance"))
        cfg_path = config.write_config(cfg)
        args = [A.FT_EXE, "trade", "--config", cfg_path, "--userdir", paths.USER_DIR, "--strategy", self.manifest["class_name"],
                "--strategy-path", self.bundle_folder]
        if self.rehearsal:
            args.append("--dry-run")
        elif "--dry-run" in args:
            raise RuntimeError("refusing: --dry-run in a live launch")
        env = dict(os.environ, JARVIS_RISK_GATE_FILE=paths.GATE_FILE)
        for k in ("FREQTRADE__EXCHANGE__KEY", "FREQTRADE__EXCHANGE__SECRET"):
            env.pop(k, None)
        if not self.rehearsal:
            c = creds.load_credentials()
            if not c:
                raise RuntimeError("no credentials")
            env["FREQTRADE__EXCHANGE__KEY"], env["FREQTRADE__EXCHANGE__SECRET"] = c["key"], c["secret"]
        self.engine_proc = EngineProcess(args, env, os.path.join(paths.LOG_DIR, "freqtrade.log"))
        self.api = FtApi(port, username, password)
        return args

    # --------------------------------------------------------------- one loop
    def _call(self, breaker, fn, *a):
        try:
            result = fn(*a)
            breaker.success()
            return result
        except (FtApiError, X.ExchangeError) as error:
            breaker.failure()
            self.events.write("api_failure", source=fn.__qualname__, error=str(error)[:200], consecutive=breaker.consecutive)
            return None

    def step(self):
        """One watchdog iteration. Returns the status dict that was written for the HUD."""
        self._loop += 1
        now = self.clock()
        self.problems = []

        # 1. emergency triggers
        if os.path.exists(paths.EMERGENCY_FILE) and self.state != "EMERGENCY":
            self.emergency_stop("emergency-stop file present")
        if self.state == "EMERGENCY":
            return self._write_status(None)

        # 2. integrity / arming (live) every ~60s
        if now - self._last_integrity >= 60:
            self._last_integrity = now
            ok, bad = bundle.verify(self.bundle_folder)
            if not ok:
                self.events.write("bundle_tampered", problems=bad)
                self.emergency_stop("frozen strategy bundle was modified: " + "; ".join(bad))
                return self._write_status(None)
            if not self.rehearsal:
                arming = approval.arming_problems(self.registry, self.settings)
                if arming and self.state not in ("WIND_DOWN",):
                    self.events.write("arming_invalid", problems=arming)
                    self.state = "WIND_DOWN"
                    self.problems += arming

        # 3. exchange feed (independent of Freqtrade)
        for pair in self.limits.allowed_pairs:
            ticker = self._call(self.exch_breaker, self.exchange.fetch_ticker, pair)
            if ticker:
                self.stale.observe(ticker["timestamp"])

        # 4. engine state
        trades = self._call(self.api_breaker, self.api.status) if self.api else None
        profit = self._call(self.api_breaker, self.api.profit) if self.api else None
        closed = self._call(self.api_breaker, self.api.trades, 200) if self.api else None
        open_trades = trades or []
        self._log_trades(open_trades, (closed or {}).get("trades", []) if isinstance(closed, dict) else (closed or []))

        # 5. portfolio state and Risk Engine context
        capital = self.limits.capital_limit
        pnl_all = float((profit or {}).get("profit_all_coin") or 0.0)
        pnl_closed = float((profit or {}).get("profit_closed_coin") or 0.0)
        equity = capital + pnl_all
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        if self._persist.get("day") != today:
            self._persist["day"], self._persist["day_start_equity"] = today, equity
        peak = max(self._persist.get("peak_equity") or equity, equity)
        self._persist["peak_equity"] = peak
        self._save_state()
        positions = [{"pair": t["pair"], "value": float(t.get("stake_amount") or 0.0)} for t in open_trades]
        portfolio = RK.PortfolioState(equity=equity, peak_equity=peak, start_of_day_equity=self._persist["day_start_equity"], open_positions=positions)
        entries_today = sum(1 for t in open_trades if str(t.get("open_date", ""))[:10] == today) + self._closed_opened_today(closed, today)
        failures = max(self.api_breaker.consecutive, self.exch_breaker.consecutive)
        self.risk.set_context(self.stale.age(), failures, os.path.exists(paths.EMERGENCY_LATCH), entries_today)

        # 6. sanity checks on what the engine actually holds
        issues = guards.position_sanity(open_trades, self.limits)
        if issues:
            self.events.write("position_sanity", issues=issues)
            self.problems += issues
            if any("leveraged" in i or "short" in i for i in issues):
                self.emergency_stop("leveraged/short position detected: " + "; ".join(issues))
                return self._write_status(None)
            if not self.risk.halted:
                self.risk.halt("position sanity: " + "; ".join(issues))
                self._safe(self.api.stop_entries if self.api else None)

        # 7. reconciliation against the real account (live only)
        if not self.rehearsal and now - self._last_reconcile >= 300:
            self._last_reconcile = now
            balances = self._call(self.exch_breaker, self.exchange.fetch_balance)
            if balances is not None:
                rec = guards.reconcile(open_trades, balances)
                missing = [r for r in rec if r["kind"] == "missing_on_exchange"]
                if rec:
                    self.events.write("reconciliation", issues=rec)
                if missing and not self.risk.halted:
                    self.risk.halt("reconciliation: the exchange holds less than the engine believes")
                    self._safe(self.api.stop_entries if self.api else None)

        # 8. critical failures => emergency stop
        if self.api_breaker.critical or self.exch_breaker.critical:
            self.emergency_stop(f"{max(self.api_breaker.consecutive, self.exch_breaker.consecutive)} consecutive API failures")
            return self._write_status(None)

        # 9. gate (the only thing the strategy trusts)
        if self.state == "WIND_DOWN":
            self.risk.set_context(self.stale.age(), failures, True, entries_today)  # no new entries while winding down
        gate = self.risk.gate_snapshot(portfolio)
        gate["ts"] = now
        _atomic_json(paths.GATE_FILE, gate)
        if gate["halted"] and not os.path.exists(paths.HALT_FILE):
            pass
        self._scan_vetoes(gate)

        # 9b. hourly: record the live strategy's own performance so the promotion gate and dashboards can compare it
        if not self.rehearsal and now - getattr(self, "_last_live_check", 0.0) >= 3600:
            self._last_live_check = now
            self._record_live_check(closed, capital, now)

        # 10. wind-down completes when flat
        if self.state == "WIND_DOWN" and trades is not None and not open_trades:
            self.events.write("wound_down", note="no open positions remain; stopping the engine")
            self._safe(self.api.stop_bot if self.api else None)
            if self.engine_proc:
                self.engine_proc.terminate()
            self.state = "STOPPED"
        elif self.state not in ("WIND_DOWN", "STOPPED"):
            self.state = "HALTED" if self.risk.halted else ("PAUSED" if not gate["allow_entries"] else "RUNNING")

        return self._write_status({"equity": equity, "peak": peak, "pnl_closed": pnl_closed, "pnl_all": pnl_all, "open": open_trades,
                                   "gate": gate, "entries_today": entries_today})

    # ------------------------------------------------------------- helpers
    def _record_live_check(self, closed, capital, now):
        from .. import metrics as M
        rows = (closed or {}).get("trades", []) if isinstance(closed, dict) else (closed or [])
        trades = []
        for t in rows:
            try:
                trades.append({"open_ts": M.to_epoch(str(t.get("open_date")).replace(" ", "T")), "close_ts": M.to_epoch(str(t.get("close_date")).replace(" ", "T")),
                               "profit_ratio": float(t.get("close_profit") or t.get("profit_ratio") or 0.0), "profit_abs": float(t.get("close_profit_abs") or t.get("profit_abs") or 0.0),
                               "stake": float(t.get("stake_amount") or 0.0), "fee_paid": 0.0})
            except Exception:
                continue
        try:
            armed = approval.read_arming() or {}
            start = M.to_epoch(armed.get("armed_at", _utc(now - 86400)))
            metrics = M.compute_metrics(trades, capital, start, now) if trades else {"n_trades": 0, "days": max(0.0, (now - start) / 86400)}
            metrics["days"] = max(metrics.get("days") or 0, (now - start) / 86400)
            self.registry.record_experiment(self.manifest["strategy_id"], self.manifest["version"], "live_check",
                                            {"metrics": metrics, "verdict": "n/a"}, notes="live performance snapshot")
        except Exception as error:
            self.events.write("action_failed", action="record_live_check", error=str(error)[:200])

    def _safe(self, fn, *a):
        if fn is None:
            return None
        try:
            return fn(*a)
        except Exception as error:
            self.events.write("action_failed", action=getattr(fn, "__name__", str(fn)), error=str(error)[:200])
            return None

    @staticmethod
    def _closed_opened_today(closed, today):
        rows = (closed or {}).get("trades", []) if isinstance(closed, dict) else (closed or [])
        return sum(1 for t in rows if str(t.get("open_date", ""))[:10] == today)

    def _log_trades(self, open_trades, closed_rows):
        for t in open_trades:
            key = ("open", t.get("trade_id"))
            if key not in self._seen:
                self._seen[key] = True
                self.last_trade = self.trades_log.write("trade_opened", trade=t)
        for t in closed_rows:
            key = ("closed", t.get("trade_id"))
            if key not in self._seen:
                self._seen[key] = True
                self.last_trade = self.trades_log.write("trade_closed", trade=t)

    def _scan_vetoes(self, gate):
        log = os.path.join(paths.LOG_DIR, "freqtrade.log")
        if not os.path.exists(log):
            return
        with open(log, "rb") as f:
            f.seek(self._log_offset)
            chunk = f.read().decode("utf-8", errors="ignore")
            self._log_offset = f.tell()
        for line in chunk.splitlines():
            if "denied entry" in line.lower():
                pair = line.split(" for ")[-1].strip(" .")
                self.last_veto = self.events.write("entry_vetoed", pair=pair, reason=gate.get("reason"), halted=gate.get("halted"))

    def emergency_stop(self, reason, wait_seconds=90):
        """Stop entries, force-exit everything, verify flat (fall back to the exchange), stop the bot, latch."""
        self.state = "EMERGENCY"
        report = {"reason": reason, "steps": []}
        self.events.write("emergency_stop", reason=reason)
        _atomic_json(paths.EMERGENCY_LATCH, {"ts": _utc(), "reason": reason})
        self.risk.set_context(self.stale.age(), 99, True, 99)
        gate = {"allow_entries": False, "halted": True, "reason": "emergency stop", "max_stake": 0.0, "ts": self.clock()}
        _atomic_json(paths.GATE_FILE, gate)
        if self.api:
            report["steps"].append(("stop_entries", self._safe(self.api.stop_entries) is not None))
            report["steps"].append(("force_exit_all", self._safe(self.api.force_exit_all) is not None))
            waited = 0
            remaining = None
            while waited <= wait_seconds:
                remaining = self._safe(self.api.status)
                if remaining is not None and len(remaining) == 0:
                    break
                self.sleep(2)
                waited += 2
            if remaining:
                report["steps"].append(("exchange_fallback", True))
                self._flatten_via_exchange(remaining)
            self._safe(self.api.stop_bot)
        if self.engine_proc:
            self.engine_proc.terminate()
        try:
            approval.disarm(self.registry, f"emergency stop: {reason}")
            self.registry.record_promotion_event("emergency", None, None, None, {"action": "stop", "reason": reason}, "system")
        except Exception as error:
            self.events.write("action_failed", action="record_emergency", error=str(error)[:200])
        return report

    def _flatten_via_exchange(self, open_trades):
        """If Freqtrade could not exit (hung/crashed), sell through the independent exchange client. Never opens positions."""
        for t in open_trades:
            pair = t["pair"]
            try:
                self.exchange.cancel_all(pair)
                cid = guards.DuplicateOrderGuard.client_id("EMERG", pair, "sell", int(self.clock() // 60))
                ok, why = self.dups.allow(cid)
                if ok:
                    self.exchange.market_sell(pair, float(t.get("amount") or 0.0), cid)
                    self.events.write("emergency_flatten", pair=pair, amount=t.get("amount"))
            except X.ExchangeError as error:
                self.events.write("emergency_flatten_failed", pair=pair, error=str(error)[:200])

    def _write_status(self, live):
        snap = {"ts": _utc(), "mode": self.mode, "state": self.state, "strategy": f"{self.manifest['strategy_id']} v{self.manifest['version']}",
                "capital_limit": self.limits.capital_limit, "problems": self.problems, "loop": self._loop,
                "data_age_seconds": self.stale.age(), "api_failures": max(self.api_breaker.consecutive, self.exch_breaker.consecutive),
                "last_trade": self.last_trade, "last_veto": self.last_veto, "risk_halted": self.risk.halted, "halt_reason": self.risk.halt_reason,
                "limits_fingerprint": self.limits.fingerprint()}
        if live:
            exposure = sum(float(t.get("stake_amount") or 0.0) for t in live["open"])
            snap.update({"equity": live["equity"], "available_capital": max(0.0, live["equity"] - exposure), "capital_allocated": exposure,
                         "realised_pnl": live["pnl_closed"], "unrealised_pnl": live["pnl_all"] - live["pnl_closed"],
                         "drawdown": (live["peak"] - live["equity"]) / live["peak"] if live["peak"] else 0.0,
                         "today": live["equity"] - (self._persist.get("day_start_equity") or live["equity"]),
                         "open_positions": [{"pair": t["pair"], "stake": t.get("stake_amount"), "profit_abs": t.get("profit_abs"),
                                             "open_date": t.get("open_date")} for t in live["open"]],
                         "gate": live["gate"], "entries_today": live["entries_today"]})
        self.snapshot = snap
        _atomic_json(paths.STATUS_FILE, snap)
        return snap

    # ----------------------------------------------------------------- run
    def run_forever(self, interval=10, max_loops=None):
        _atomic_json(paths.PID_FILE, {"pid": os.getpid(), "mode": self.mode})
        try:
            loops = 0
            while self.state not in ("STOPPED", "EMERGENCY"):
                if self.engine_proc and not self.engine_proc.alive() and self.state != "WIND_DOWN":
                    self.events.write("engine_died", note="Freqtrade is not running; entries are vetoed (gate goes stale)")
                    self.problems.append("engine process not running")
                    self.api_breaker.failure()
                self.step()
                loops += 1
                if max_loops and loops >= max_loops:
                    break
                self.sleep(interval)
        finally:
            if os.path.exists(paths.PID_FILE):
                os.remove(paths.PID_FILE)
