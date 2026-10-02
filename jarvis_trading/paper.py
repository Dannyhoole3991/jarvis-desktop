"""
Paper-trading manager: runs strategies that survived the research gates in
Freqtrade's dry-run mode against LIVE market data with simulated money, and
compares what happens with what the backtest predicted.

Hard rules (enforced here, not just documented):
  - Only strategy versions with status 'candidate' may enter paper trading.
  - The config is passed through assert_safe_config: dry_run, no credentials,
    spot only, no leverage. There is no code path that builds a live config.
  - Every entry goes through the independent Risk Engine's gate file; the
    manager rewrites the gate from real paper-account state and the engine
    (Freqtrade) can only read it.
  - A paper run that breaches the predefined criteria is stopped and its
    version REJECTED in the registry, with the reason.
"""

import json
import os
import sqlite3
import calendar
import subprocess
import time

from . import criteria as C
from . import freqtrade_adapter as A
from . import metrics as M
from . import risk as RK
from . import strategies as S

PAPER_DIR = os.path.join(A.LAB_DIR, "paper")
PAPER_WALLET = 1000.0


class PaperError(Exception):
    pass


def _paths(strategy_id, version):
    base = os.path.join(PAPER_DIR, f"{strategy_id}_v{version}")
    return {"dir": base, "db": os.path.join(base, "trades.sqlite"), "gate": os.path.join(base, "risk_gate.json"),
            "config": os.path.join(base, "config_paper.json"), "pid": os.path.join(base, "pid.txt"),
            "risk_state": os.path.join(base, "risk_state.json")}


def read_paper_trades(db_path):
    """Closed paper trades from Freqtrade's SQLite file, in the lab's trade format."""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT pair, open_date, close_date, stake_amount, profit_ratio, profit_abs, fee_open_cost, fee_close_cost "
            "FROM trades WHERE is_open = 0 AND close_date IS NOT NULL ORDER BY close_date").fetchall()
    finally:
        conn.close()
    return [{"pair": r["pair"], "open_ts": M.to_epoch(r["open_date"]), "close_ts": M.to_epoch(r["close_date"]),
             "profit_ratio": float(r["profit_ratio"] or 0.0), "profit_abs": float(r["profit_abs"] or 0.0),
             "stake": float(r["stake_amount"] or 0.0),
             "fee_paid": float(r["fee_open_cost"] or 0.0) + float(r["fee_close_cost"] or 0.0)} for r in rows]


def read_open_positions(db_path):
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        return [{"pair": p, "value": float(v or 0.0)} for p, v in
                conn.execute("SELECT pair, stake_amount FROM trades WHERE is_open = 1").fetchall()]
    finally:
        conn.close()


class PaperManager:
    def __init__(self, registry, risk_engine=None):
        self.registry = registry
        self.risk = risk_engine or RK.RiskEngine()

    # ------------------------------------------------------------ gating
    def eligible(self):
        return self.registry.list_versions(status="candidate")

    def portfolio_state(self, strategy_id, version):
        p = _paths(strategy_id, version)
        trades = read_paper_trades(p["db"])
        equity = PAPER_WALLET + sum(t["profit_abs"] for t in trades)
        curve = M.equity_curve(trades, PAPER_WALLET)
        peak = max([PAPER_WALLET] + [e for _, e in curve])
        today = int(max([t["close_ts"] for t in trades] + [0]) // 86400)
        start_of_day = PAPER_WALLET + sum(t["profit_abs"] for t in trades if int(t["close_ts"] // 86400) < today)
        return RK.PortfolioState(equity=equity, peak_equity=peak, start_of_day_equity=start_of_day,
                                 open_positions=read_open_positions(p["db"]))

    def write_gate(self, strategy_id, version):
        p = _paths(strategy_id, version)
        os.makedirs(p["dir"], exist_ok=True)
        engine = RK.RiskEngine(self.risk.limits, state_path=p["risk_state"])
        snap = engine.gate_snapshot(self.portfolio_state(strategy_id, version))
        with open(p["gate"], "w", encoding="utf-8") as f:
            json.dump(snap, f)
        return snap

    # ----------------------------------------------------------- lifecycle
    def prepare(self, strategy_id, version):
        status = self.registry.status(strategy_id, version)
        if status not in ("candidate", "paper"):
            raise PaperError(f"{strategy_id} v{version} is '{status}'. Only candidates that survived research may be paper traded.")
        v = self.registry.get_version(strategy_id, version)
        pairs = self.risk.limits.allowed_pairs
        p = _paths(strategy_id, version)
        os.makedirs(p["dir"], exist_ok=True)
        cfg = A.build_config(list(pairs), v["parameters"]["timeframe"], PAPER_WALLET, 100.0, 2)
        cfg["db_url"] = "sqlite:///" + p["db"].replace("\\", "/")
        A.assert_safe_config(cfg)
        with open(p["config"], "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, sort_keys=True)
        self.write_gate(strategy_id, version)
        if status == "candidate":
            self.registry.set_status(strategy_id, version, "paper", "Entered live-market paper trading (simulated money, dry-run).")
            self.registry.log_decision("paper_start", "Paper trading prepared; risk gate written.", strategy_id, version)
        return p

    def command(self, strategy_id, version):
        p = _paths(strategy_id, version)
        v = self.registry.get_version(strategy_id, version)
        template = self.registry.get(strategy_id)["source"].split(":", 1)[1]
        name, source, _ = S.generate(template, v["parameters"])
        A.write_strategy(name, source)
        return [A.FT_EXE, "trade", "--dry-run", "--config", p["config"], "--userdir", A.USER_DIR,
                "--strategy", name, "--strategy-path", A.STRATEGY_DIR, "--db-url", "sqlite:///" + p["db"].replace("\\", "/")]

    def start(self, strategy_id, version):
        p = self.prepare(strategy_id, version)
        args = self.command(strategy_id, version)
        if "--dry-run" not in args:
            raise PaperError("Refusing to start: --dry-run missing.")
        env = dict(os.environ, JARVIS_RISK_GATE_FILE=p["gate"])
        for secret in ("FREQTRADE__EXCHANGE__KEY", "FREQTRADE__EXCHANGE__SECRET"):
            env.pop(secret, None)
        log = open(os.path.join(p["dir"], "freqtrade.log"), "ab")
        proc = subprocess.Popen(args, env=env, stdout=log, stderr=log, cwd=A.LAB_DIR)
        with open(p["pid"], "w") as f:
            f.write(str(proc.pid))
        return proc.pid

    def stop(self, strategy_id, version):
        p = _paths(strategy_id, version)
        if not os.path.exists(p["pid"]):
            return False
        pid = int(open(p["pid"]).read().strip())
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        os.remove(p["pid"])
        return True

    # ---------------------------------------------------------- evaluation
    def expected_from_backtest(self, strategy_id, version):
        tests = self.registry.experiments(strategy_id, version, "backtest_test")
        if not tests:
            return {}
        m = tests[-1]["metrics"]
        days = m.get("days") or 0
        return {"avg_daily_return": m.get("avg_daily_return"),
                "trades_per_day": (m.get("n_trades") or 0) / days if days else None}

    def evaluate(self, strategy_id, version, criteria=None):
        """Compares paper results with the backtest's expectation; rejects and stops the run on a breach."""
        p = _paths(strategy_id, version)
        trades = read_paper_trades(p["db"])
        if trades:
            start, end = min(t["open_ts"] or t["close_ts"] for t in trades), max(t["close_ts"] for t in trades)
        else:
            start = end = None
        # observation window starts when paper trading started, not at the first trade
        started = self._paper_start_epoch(strategy_id, version)
        window_end = max(end or 0, time.time())
        paper = M.compute_metrics(trades, PAPER_WALLET, started or start, window_end) if (started or trades) else {"n_trades": 0, "days": 0}
        verdict = C.evaluate_paper(paper, self.expected_from_backtest(strategy_id, version), criteria)
        self.registry.record_experiment(strategy_id, version, "paper_check",
                                        {"metrics": paper, "verdict": verdict.status, "reasons": verdict.reasons},
                                        passed=verdict.status != "REJECTED")
        if verdict.status == "REJECTED" and self.registry.status(strategy_id, version) == "paper":
            self.stop(strategy_id, version)
            self.registry.set_status(strategy_id, version, "rejected", "Paper trading: " + verdict.summary())
            self.registry.log_decision("paper_rejected", verdict.summary(), strategy_id, version)
        self.write_gate(strategy_id, version)
        return verdict, paper

    def _paper_start_epoch(self, strategy_id, version):
        for h in self.registry.status_history(strategy_id, version):
            if h["status"] == "paper":
                return calendar.timegm(time.strptime(h["ts"], "%Y-%m-%dT%H:%M:%S"))
        return None
