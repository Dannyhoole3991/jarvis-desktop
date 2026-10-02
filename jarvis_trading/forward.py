"""
Forward-only paper trial of the near-miss strategies.

QUESTION: how do strategies that nearly passed the historical gates behave on genuinely UNSEEN live market data?

What this is NOT: a second chance to pass. The research gates are unchanged, the near-misses stay REJECTED in the
registry, and nothing about a forward trial can promote a strategy:
  - the registry refuses to take a forward-trial version live (`_go_live`), the promotion review fails it, and the
    live bundle freezer refuses it; no code here imports the live package (tests/test_separation.py);
  - trials run Freqtrade in DRY-RUN only (assert_safe_config), with the research Risk Engine gate.

Integrity rules (all recorded in the append-only registry before/while the trial runs):
  - the exact strategy version, parameters, code hash, evaluation criteria and a HARD start timestamp are frozen and
    stored when the trial is created; one trial per strategy version, ever;
  - the strategy file is a read-only copy verified by hash every tick; any change INVALIDATES the trial;
  - only trades opened after the start timestamp count; nothing is tuned, re-fitted or adjusted during the trial;
  - every signal, entry and exit is written to a hash-chained ledger (tamper-evident), and the ledger head is
    periodically committed to the append-only registry log;
  - failures are recorded, not fixed: a failing strategy keeps running unchanged to the pre-registered horizon.

Results use the lab's cost assumptions: Freqtrade dry-run applies the exchange's fee but models no slippage, so each
trade is also reported after an assumed 0.05% slippage per side (the same assumption as the backtests).
"""

import calendar
import hashlib
import json
import os
import sqlite3
import stat
import time
from dataclasses import asdict, dataclass

from . import benchmark as BM
from . import freqtrade_adapter as A
from . import metrics as M
from . import paper as P
from . import regime as RG
from . import risk as RK
from . import stats as ST
from . import strategies as S
from . import supervisor as SUP
from . import tracking as T
from . import universe as U

FORWARD_DIR = os.path.join(A.LAB_DIR, "forward")
REPORT_DIR = os.path.join(A.LAB_DIR, "reports")
WALLET = 1000.0
SLIPPAGE = A.SLIPPAGE_ASSUMPTION
TERMINAL = ("COMPLETE_SUPPORTS", "COMPLETE_CONTRADICTS", "COMPLETE_INCONCLUSIVE", "INVALIDATED")
NOTE = ("This strategy is a near-miss in a forward-only trial. It remains REJECTED by the research gates and can never be "
        "promoted to live on the strength of this trial.")


# ----------------------------------------------------------------------------- rules
@dataclass(frozen=True)
class ForwardCriteria:
    """How the trial is judged. Fixed before it starts, stored with the trial, never changed."""
    min_days: int = 90
    min_trades: int = 20
    max_days: int = 180                       # ends here even if too few trades happened (then INCONCLUSIVE)
    max_degradation: float = 0.50             # SUPPORTS needs forward Sharpe >= (1 - this) x the out-of-sample Sharpe
    contradict_sharpe_fraction: float = 0.25  # CONTRADICTS if forward Sharpe < this x out-of-sample Sharpe
    contradict_profit_factor: float = 0.90
    breach_drawdown: float = 0.20             # recorded as a breach; never stops the trial
    exceptional_sharpe: float = 2.0           # flagged for HUMAN REVIEW (and as a possible bug); never promoted
    exceptional_min_days: int = 30
    exceptional_min_trades: int = 20
    alpha: float = 0.05                       # corrected by the number of strategies in the trial set

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class SelectionRules:
    """Which near-misses enter a trial. Uses only research results that already exist."""
    n: int = 5
    max_p_value: float = 0.10          # the corrected-away significance test: "near"
    min_oos_sharpe: float = 0.80
    max_oos_drawdown: float = 0.25
    max_instability: float = 0.25      # share of nearby parameter sets that lose money
    max_per_family: int = 2
    max_overlap: float = 0.50          # behavioural similarity (entry overlap) allowed between two selected strategies


# --------------------------------------------------------------------------- selection
def _signature(registry, sid, ver):
    exps = registry.experiments(sid, ver, "signature")
    return set(exps[-1]["metrics"].get("entries", [])) if exps else set()


def select_near_misses(registry, rules=None, benchmark_path=None):
    """
    Near-miss = REJECTED, and the ONLY reason is the multiple-testing significance test. From those, keep the ones with
    the strongest out-of-sample evidence, rank by the leaderboard's luck-adjusted score, and keep the set diverse
    (family cap, no behavioural near-duplicates). Returns {"selected": [...], "skipped": [...], "rules": {...}}.
    """
    rules = rules or SelectionRules()
    track = T.build(registry, benchmark_path=benchmark_path)
    candidates, skipped = [], []
    for r in track["rows"]:
        if r["status"] != "rejected" or not r["codes"] or not set(r["codes"]) <= {"NOT_SIGNIFICANT"}:
            continue
        label = f"{r['strategy_id']} {r['template']}"
        oos, conf, stab = r["out_of_sample"] or {}, r["confidence"] or {}, r["stability"] or {}
        why = []
        if S.is_ml(r["template"]):
            why.append("ML strategies cannot run in Freqtrade dry-run")
        if (conf.get("p_value") is None) or conf["p_value"] > rules.max_p_value:
            why.append(f"p-value {conf.get('p_value')} above {rules.max_p_value}")
        if (oos.get("sharpe") or 0) < rules.min_oos_sharpe:
            why.append(f"out-of-sample Sharpe {oos.get('sharpe')} below {rules.min_oos_sharpe}")
        if (oos.get("max_drawdown") or 1) > rules.max_oos_drawdown:
            why.append("out-of-sample drawdown too high")
        if (stab.get("neighbour_fail_fraction") or 0) > rules.max_instability:
            why.append("parameter instability")
        if registry.has_forward_trial(r["strategy_id"], r["version"]):
            why.append("already has a forward trial")
        (skipped if why else candidates).append({"label": label, "why": why} if why else r)
    candidates.sort(key=lambda r: (-(r["score"] if r["score"] is not None else -99), (r["confidence"] or {}).get("p_value") or 1))
    selected, families = [], {}
    for r in candidates:
        if len(selected) >= rules.n:
            break
        label = f"{r['strategy_id']} {r['template']}"
        oos, conf, stab = r["out_of_sample"] or {}, r["confidence"] or {}, r["stability"] or {}
        if families.get(r["template"], 0) >= rules.max_per_family:
            skipped.append({"label": label, "why": [f"already {rules.max_per_family} selected from the {r['template']} family"]})
            continue
        sig = _signature(registry, r["strategy_id"], r["version"])
        clash = None
        for other in selected:
            osig = _signature(registry, other["strategy_id"], other["version"])
            if sig and osig and len(sig & osig) / len(sig | osig) > rules.max_overlap:
                clash = other["strategy_id"]
                break
        if clash:
            skipped.append({"label": label, "why": [f"trades too similarly to already-selected {clash}"]})
            continue
        families[r["template"]] = families.get(r["template"], 0) + 1
        selected.append({
            "strategy_id": r["strategy_id"], "version": r["version"], "template": r["template"], "parameters": r["parameters"],
            "rank": len(selected) + 1, "luck_adjusted_score": r["score"], "oos_sharpe": oos.get("sharpe"), "oos_calmar": oos.get("calmar"),
            "oos_max_drawdown": oos.get("max_drawdown"), "oos_trades": oos.get("n_trades"), "p_value": (r["confidence"] or {}).get("p_value"),
            "required_p": conf.get("required_p"), "parameter_instability": stab.get("neighbour_fail_fraction"),
            "development": r["development"], "vs_passive_sharpe": r.get("vs_passive_sharpe"),
            "reason": (f"Rejected ONLY by the multiple-testing significance test (p={conf.get('p_value'):.3f}, "
                       f"needed < {conf.get('required_p'):.1e}); passed every other gate; out-of-sample Sharpe "
                       f"{oos.get('sharpe'):.2f}, drawdown {oos.get('max_drawdown'):.1%}; ranked {len(selected) + 1} by luck-adjusted score "
                       f"among {len(candidates)} qualifying near-misses.")})
    return {"selected": selected, "skipped": skipped[:30], "rules": asdict(rules), "qualifying_near_misses": len(candidates)}


# ---------------------------------------------------------------------------- freezing
RECORDER = '''

# --- Forward-trial recorder (appended; the strategy logic above is unchanged) -----------------------------------
# It only APPENDS what the strategy produced to a log; it never alters a dataframe, and any error here is swallowed.
import json as _fj
import os as _fo
import time as _ft


def _fwd_record(kind, pair, frame):
    path = _fo.environ.get("JARVIS_SIGNAL_LOG")
    if not path:
        return
    try:
        row = frame.iloc[-1]
        record = {"ts": _ft.time(), "kind": kind, "pair": pair, "candle": str(row["date"]), "close": float(row["close"])}
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(_fj.dumps(record) + "\\n")
    except Exception:
        pass


_fwd_entry = {cls}.populate_entry_trend
_fwd_exit = {cls}.populate_exit_trend


def _fwd_entry_logged(self, dataframe, metadata):
    out = _fwd_entry(self, dataframe, metadata)
    try:
        if "enter_long" in out.columns and out["enter_long"].iloc[-1] == 1:
            _fwd_record("entry_signal", metadata.get("pair"), out)
    except Exception:
        pass
    return out


def _fwd_exit_logged(self, dataframe, metadata):
    out = _fwd_exit(self, dataframe, metadata)
    try:
        if "exit_long" in out.columns and out["exit_long"].iloc[-1] == 1:
            _fwd_record("exit_signal", metadata.get("pair"), out)
    except Exception:
        pass
    return out


{cls}.populate_entry_trend = _fwd_entry_logged
{cls}.populate_exit_trend = _fwd_exit_logged
'''


def frozen_source(code, class_name):
    return code + RECORDER.replace("{cls}", class_name)


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def trial_paths(trial):
    base = os.path.join(FORWARD_DIR, f"T{trial['trial_id']:03d}_{trial['strategy_id']}_v{trial['version']}")
    return {"dir": base, "strategy_dir": os.path.join(base, "strategy"), "strategy": os.path.join(base, "strategy", "strategy.py"),
            "config": os.path.join(base, "config.json"), "gate": os.path.join(base, "risk_gate.json"), "signals": os.path.join(base, "signals.jsonl"),
            "ledger": os.path.join(base, "ledger.jsonl"), "db": os.path.join(base, "trades.sqlite"), "log": os.path.join(base, "freqtrade.log"),
            "pid": os.path.join(base, "pid.txt"), "protocol": os.path.join(base, "protocol.json")}


def protocol_hash(trial):
    keep = {k: trial[k] for k in ("strategy_id", "version", "content_hash", "code_sha256", "start_epoch", "criteria_fingerprint")}
    keep["params"] = trial["params"]
    keep["selection"] = trial["selection"]
    return sha256_text(json.dumps(keep, sort_keys=True, default=str))


def start_set(registry, rules=None, criteria=None, now=None, base_dir=None, selection=None):
    """
    Selects the near-misses, freezes each exact version and records the trial with a hard start timestamp.
    Idempotent per strategy version: a version can only ever have ONE trial.
    """
    rules, criteria = rules or SelectionRules(), criteria or ForwardCriteria()
    selection = selection or select_near_misses(registry, rules)
    now = float(now if now is not None else time.time())
    set_id = time.strftime("FWD-%Y%m%d-%H%M%S", time.gmtime(now))
    started = []
    for item in selection["selected"]:
        sid, ver = item["strategy_id"], item["version"]
        if registry.has_forward_trial(sid, ver):
            continue
        meta, stored = registry.get(sid), registry.get_version(sid, ver)
        template = meta["source"].split(":", 1)[1]
        class_name, regenerated, _ = S.generate(template, stored["parameters"])
        if regenerated != stored["code"]:
            raise RuntimeError(f"{sid} v{ver}: stored code does not regenerate from its template and parameters; not starting a trial")
        code = frozen_source(stored["code"], class_name)
        trial_id = registry.create_forward_trial(set_id, sid, ver, stored["content_hash"], sha256_text(code), stored["parameters"], now,
                                                 asdict(criteria), criteria.fingerprint(), dict(item, class_name=class_name, set_rules=asdict(rules)))
        trial = registry.forward_trial(trial_id)
        paths = trial_paths(trial)
        os.makedirs(paths["strategy_dir"], exist_ok=True)
        with open(paths["strategy"], "w", encoding="utf-8", newline="\n") as f:
            f.write(code)
        os.chmod(paths["strategy"], stat.S_IREAD)
        with open(paths["protocol"], "w", encoding="utf-8") as f:
            json.dump({"trial_id": trial_id, "protocol_hash": protocol_hash(trial), "start_epoch": now, "criteria": asdict(criteria),
                       "note": NOTE}, f, indent=2, sort_keys=True)
        registry.log_forward(trial_id, "status", {"status": "RUNNING", "start_epoch": now, "protocol_hash": protocol_hash(trial)})
        registry.log_decision("forward_trial", f"Forward-only paper trial started for {sid} v{ver} ({template}) at {_iso(now)}. {NOTE}", sid, ver)
        started.append(trial)
    return {"set_id": set_id, "started": started, "selection": selection}


# --------------------------------------------------------------------------- ledger
class Ledger:
    """Append-only, hash-chained JSONL. Editing or deleting any earlier line breaks every later hash."""

    def __init__(self, path):
        self.path = path
        self._head, self._seq, self._keys = "0" * 64, 0, set()
        for rec in self.records():
            self._head, self._seq = rec["hash"], rec["seq"]
            if rec["payload"].get("key"):
                self._keys.add(rec["payload"]["key"])

    @staticmethod
    def _hash(prev, seq, ts, kind, payload):
        return hashlib.sha256((prev + json.dumps({"seq": seq, "ts": ts, "kind": kind, "payload": payload}, sort_keys=True, default=str)).encode()).hexdigest()

    def records(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    @property
    def head(self):
        return self._head

    def has(self, key):
        return key in self._keys

    def append(self, kind, payload, ts=None):
        if payload.get("key") and payload["key"] in self._keys:
            return None
        ts = _iso(ts if ts is not None else time.time())
        seq = self._seq + 1
        digest = self._hash(self._head, seq, ts, kind, payload)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"seq": seq, "ts": ts, "kind": kind, "payload": payload, "prev": self._head, "hash": digest}, default=str) + "\n")
        self._head, self._seq = digest, seq
        if payload.get("key"):
            self._keys.add(payload["key"])
        return digest

    def verify(self):
        prev, problems = "0" * 64, []
        for i, rec in enumerate(self.records(), start=1):
            if rec["seq"] != i or rec["prev"] != prev or rec["hash"] != self._hash(prev, rec["seq"], rec["ts"], rec["kind"], rec["payload"]):
                problems.append(f"ledger record {i} does not chain correctly (the ledger was altered)")
                break
            prev = rec["hash"]
        return not problems, problems


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ------------------------------------------------------------------ market data (public)
_cache = {}


def default_fetcher(pair, since_ms):
    import ccxt  # in the Freqtrade venv; public candles, no key
    return ccxt.binance({"enableRateLimit": True}).fetch_ohlcv(pair, "1d", since=since_ms, limit=1000)


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        value = fn()
    except Exception:
        value = None
    _cache[key] = (time.time(), value)
    return value


def benchmark_since(start_epoch, fetcher=None, pairs=None):
    """Equal-weight daily-rebalanced buy-and-hold of the universe over the SAME window as the trial (gross of costs)."""
    fetcher = fetcher or default_fetcher
    pairs = pairs or U.UNIVERSE

    def work():
        per_pair = {}
        for pair in pairs:
            rows = fetcher(pair, int((start_epoch - 3 * 86400) * 1000))
            rets = {}
            for prev, cur in zip(rows, rows[1:]):
                day = int(cur[0] // 1000 // 86400)
                if day * 86400 >= start_epoch - 86400 and prev[4]:
                    rets[day] = cur[4] / prev[4] - 1.0
            per_pair[pair] = rets
        daily = BM.equal_weight_daily_returns(per_pair)
        out = BM.summarise(daily)
        out["days_covered"] = len(daily)
        return out
    return _cached(("bench", int(start_epoch)), 3600, work)


def current_regime(fetcher=None):
    """Regime label for today from BTC daily candles (past data only)."""
    fetcher = fetcher or default_fetcher

    def work():
        rows = fetcher("BTC/USDT", int((time.time() - 520 * 86400) * 1000))
        closes = [(r[0] / 1000.0, r[4]) for r in rows]
        volumes = [(int(r[0] // 1000 // 86400), r[5]) for r in rows]
        hist = RG.classify(closes, volumes)
        return hist[-1]["label"] if hist else "unknown"
    return _cached(("regime",), 3600, work) or "unknown"


# --------------------------------------------------------------------- trade reading
def read_trade_rows(db_path):
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, pair, is_open, open_date, close_date, open_rate, close_rate, stake_amount, amount, fee_open, fee_close, fee_open_cost, "
            "fee_close_cost, close_profit, close_profit_abs, exit_reason, enter_tag, leverage, is_short FROM trades ORDER BY id").fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows]


def adjusted_ratio(raw_ratio, slippage=SLIPPAGE):
    """Profit ratio after paying `slippage` more on entry and receiving `slippage` less on exit."""
    return (1.0 + raw_ratio) * (1.0 - slippage) / (1.0 + slippage) - 1.0


def closed_trades(rows, start_epoch, slippage=SLIPPAGE):
    """Closed trades opened AFTER the hard start timestamp, in the lab's trade format, slippage-adjusted (and raw)."""
    out = []
    for r in rows:
        if r["is_open"] or not r["close_date"]:
            continue
        opened = M.to_epoch(r["open_date"])
        if opened < start_epoch:
            continue
        raw = float(r["close_profit"] or 0.0)
        adj = adjusted_ratio(raw, slippage)
        stake = float(r["stake_amount"] or 0.0)
        out.append({"pair": r["pair"], "open_ts": opened, "close_ts": M.to_epoch(r["close_date"]), "profit_ratio": adj, "profit_abs": adj * stake,
                    "raw_profit_ratio": raw, "raw_profit_abs": float(r["close_profit_abs"] or 0.0), "stake": stake,
                    "fee_paid": float(r["fee_open_cost"] or 0.0) + float(r["fee_close_cost"] or 0.0),
                    "leverage": float(r["leverage"] or 1.0), "is_short": bool(r["is_short"])})
    return sorted(out, key=lambda t: t["close_ts"])


# ----------------------------------------------------------------------- evaluation
def compute_forward_metrics(trades, start_epoch, now):
    adj = M.compute_metrics(trades, WALLET, start_epoch, now) if trades else {"n_trades": 0}
    raw_trades = [dict(t, profit_ratio=t["raw_profit_ratio"], profit_abs=t["raw_profit_abs"]) for t in trades]
    raw = M.compute_metrics(raw_trades, WALLET, start_epoch, now) if trades else {"n_trades": 0}
    days = max(0.0, (now - start_epoch) / 86400.0)
    adj["days_elapsed"] = days
    adj["raw_total_return"] = raw.get("total_return")
    adj["raw_sharpe"] = raw.get("sharpe")
    adj["trades_per_day"] = (adj.get("n_trades") or 0) / days if days > 0 else None
    return adj


def forward_pvalue(trades, n_in_set):
    if len(trades) < 5:
        return {"p_value": None, "n": len(trades)}
    per_trade = ST.sign_flip_p_value([t["profit_ratio"] for t in trades], n_perm=4000, seed=1)
    by_day = {}
    for t in trades:
        d = int(t["close_ts"] // 86400)
        by_day[d] = by_day.get(d, 0.0) + t["profit_abs"] / WALLET
    per_day = ST.sign_flip_p_value(list(by_day.values()), n_perm=4000, seed=1)
    p = None if per_trade is None or per_day is None else max(per_trade, per_day)
    return {"p_value": p, "per_trade": per_trade, "per_day": per_day, "n": len(trades), "alpha_corrected": 0.05 / max(1, n_in_set)}


def expected_from_research(registry, sid, ver):
    """What the research evidence said to expect: out-of-sample and development metrics."""
    def last(kind):
        e = registry.experiments(sid, ver, kind)
        return e[-1]["metrics"] if e else None
    oos, train, val = last("backtest_test"), last("backtest_training"), last("backtest_validation")
    return {"out_of_sample": oos, "training": train, "validation": val}


def evaluate_verdict(fwd, oos, criteria):
    """
    Pre-registered judgement. Returns {"outcome", "reasons"}; outcome is RUNNING until the horizon is reached.
    SUPPORTS / CONTRADICTS / INCONCLUSIVE describe the historical evidence only: none of them promotes anything.
    """
    days, n = fwd.get("days_elapsed") or 0.0, fwd.get("n_trades") or 0
    horizon = (days >= criteria.min_days and n >= criteria.min_trades) or days >= criteria.max_days
    if not horizon:
        return {"outcome": "RUNNING", "reasons": [f"{days:.0f} of at least {criteria.min_days} days and {n} of at least {criteria.min_trades} trades so far"]}
    if n < criteria.min_trades:
        return {"outcome": "COMPLETE_INCONCLUSIVE", "reasons": [f"only {n} trades in {days:.0f} days (needed {criteria.min_trades}); too little evidence either way"]}
    oos_sharpe = (oos or {}).get("sharpe")
    sharpe, ret, pf = fwd.get("sharpe"), fwd.get("total_return") or 0.0, fwd.get("profit_factor")
    reasons = []
    contradict = False
    if ret <= 0:
        contradict = True
        reasons.append(f"forward net return {ret:.1%} after fees and slippage is not positive")
    if oos_sharpe and (sharpe is None or sharpe < criteria.contradict_sharpe_fraction * oos_sharpe):
        contradict = True
        reasons.append(f"forward Sharpe {sharpe if sharpe is None else round(sharpe, 2)} is below {criteria.contradict_sharpe_fraction:.0%} of the out-of-sample Sharpe {oos_sharpe:.2f}")
    if pf is not None and pf < criteria.contradict_profit_factor:
        contradict = True
        reasons.append(f"forward profit factor {pf:.2f} is below {criteria.contradict_profit_factor}")
    if contradict:
        return {"outcome": "COMPLETE_CONTRADICTS", "reasons": reasons}
    keeps = oos_sharpe and sharpe is not None and sharpe >= (1 - criteria.max_degradation) * oos_sharpe
    if ret > 0 and keeps and (pf is None or pf >= 1.0):
        return {"outcome": "COMPLETE_SUPPORTS", "reasons": [f"forward Sharpe {sharpe:.2f} retains at least {1 - criteria.max_degradation:.0%} of the out-of-sample Sharpe {oos_sharpe:.2f} with a positive net return"]}
    return {"outcome": "COMPLETE_INCONCLUSIVE", "reasons": [f"forward results are positive but weaker than the out-of-sample evidence (Sharpe {sharpe if sharpe is None else round(sharpe, 2)} vs {oos_sharpe})"]}


def comparisons(fwd, expected, bench):
    """Forward performance next to the original backtest (training/validation), the out-of-sample results and the benchmark."""
    oos, train, val = expected.get("out_of_sample") or {}, expected.get("training") or {}, expected.get("validation") or {}
    rel = lambda a, b: None if a is None or not b else a / b
    out = {
        "vs_out_of_sample": {"oos_sharpe": oos.get("sharpe"), "forward_sharpe": fwd.get("sharpe"), "sharpe_ratio": rel(fwd.get("sharpe"), oos.get("sharpe")),
                             "oos_avg_daily_return": oos.get("avg_daily_return"), "forward_avg_daily_return": fwd.get("avg_daily_return"),
                             "oos_max_drawdown": oos.get("max_drawdown"), "forward_max_drawdown": fwd.get("max_drawdown"),
                             "oos_win_rate": oos.get("win_rate"), "forward_win_rate": fwd.get("win_rate"),
                             "oos_trades_per_day": (oos.get("n_trades") or 0) / oos["days"] if oos.get("days") else None, "forward_trades_per_day": fwd.get("trades_per_day")},
        "vs_original_backtest": {"training_sharpe": train.get("sharpe"), "validation_sharpe": val.get("sharpe"), "forward_sharpe": fwd.get("sharpe"),
                                 "training_avg_daily_return": train.get("avg_daily_return"), "validation_avg_daily_return": val.get("avg_daily_return")},
    }
    if bench:
        out["vs_benchmark"] = {"benchmark_return": bench.get("total_return"), "benchmark_sharpe": bench.get("sharpe"), "benchmark_max_drawdown": bench.get("max_drawdown"),
                               "forward_return": fwd.get("total_return"), "excess_return": None if fwd.get("total_return") is None or bench.get("total_return") is None else fwd["total_return"] - bench["total_return"],
                               "forward_max_drawdown": fwd.get("max_drawdown"), "benchmark_days": bench.get("days")}
    return out


# ------------------------------------------------------------------ one trial: sync/check
def sync_ledger(registry, trial, now=None, regime=None):
    """Copies every new signal, entry and exit into the hash-chained ledger. Returns how many records were added."""
    paths = trial_paths(trial)
    ledger = Ledger(paths["ledger"])
    added = 0
    regime = regime or "unknown"
    if os.path.exists(paths["signals"]):
        with open(paths["signals"], "r", encoding="utf-8") as f:
            for line in f:
                try:
                    s = json.loads(line)
                except ValueError:
                    continue
                if s["ts"] < trial["start_epoch"]:
                    continue
                key = f"signal|{s['kind']}|{s['pair']}|{s['candle']}"
                if ledger.append("signal", {"key": key, "signal": s["kind"], "pair": s["pair"], "candle": s["candle"], "close": s["close"],
                                            "recorded_ts": s["ts"], "regime": regime}, ts=s["ts"]):
                    added += 1
    for r in read_trade_rows(paths["db"]):
        opened = M.to_epoch(r["open_date"])
        if opened < trial["start_epoch"]:
            if ledger.append("excluded", {"key": f"pre|{r['id']}", "trade_id": r["id"], "pair": r["pair"], "open_date": r["open_date"],
                                          "reason": "opened before the hard start timestamp; excluded from all results"}):
                added += 1
            continue
        if ledger.append("entry", {"key": f"entry|{r['id']}", "trade_id": r["id"], "pair": r["pair"], "open_date": r["open_date"], "open_rate": r["open_rate"],
                                   "stake": r["stake_amount"], "amount": r["amount"], "fee_rate": r["fee_open"], "fee_cost": r["fee_open_cost"],
                                   "assumed_slippage_per_side": SLIPPAGE, "entry_price_with_slippage": float(r["open_rate"] or 0) * (1 + SLIPPAGE),
                                   "enter_tag": r["enter_tag"], "regime": regime}, ts=opened):
            added += 1
        if not r["is_open"] and r["close_date"]:
            raw = float(r["close_profit"] or 0.0)
            if ledger.append("exit", {"key": f"exit|{r['id']}", "trade_id": r["id"], "pair": r["pair"], "close_date": r["close_date"], "close_rate": r["close_rate"],
                                      "exit_reason": r["exit_reason"], "fee_rate": r["fee_close"], "fee_cost": r["fee_close_cost"], "raw_profit_ratio": raw,
                                      "raw_profit_abs": r["close_profit_abs"], "assumed_slippage_per_side": SLIPPAGE, "exit_price_with_slippage": float(r["close_rate"] or 0) * (1 - SLIPPAGE),
                                      "slippage_adjusted_profit_ratio": adjusted_ratio(raw), "regime": regime}, ts=M.to_epoch(r["close_date"])):
                added += 1
    return added


def check(registry, trial, now=None, fetcher=None, n_in_set=None):
    """Computes the current standing, logs it (with the ledger head), records breaches/flags once, and finalises at the horizon."""
    now = float(now if now is not None else time.time())
    criteria = ForwardCriteria(**trial["criteria"])
    paths = trial_paths(trial)
    rows = read_trade_rows(paths["db"])
    trades = closed_trades(rows, trial["start_epoch"])
    fwd = compute_forward_metrics(trades, trial["start_epoch"], now)
    open_positions = sum(1 for r in rows if r["is_open"] and M.to_epoch(r["open_date"]) >= trial["start_epoch"])
    expected = expected_from_research(registry, trial["strategy_id"], trial["version"])
    bench = benchmark_since(trial["start_epoch"], fetcher) if fetcher is not False else None
    n_set = n_in_set or len(registry.forward_trials(trial["set_id"])) or 1
    comp = comparisons(fwd, expected, bench)
    pval = forward_pvalue(trades, n_set)
    verdict = evaluate_verdict(fwd, expected.get("out_of_sample"), criteria)
    ledger = Ledger(paths["ledger"])
    details = {"asof": _iso(now), "days_elapsed": fwd["days_elapsed"], "metrics": fwd, "open_positions": open_positions, "comparisons": comp,
               "forward_significance": pval, "interim_or_final": verdict, "ledger_head": ledger.head, "ledger_records": len(ledger.records()),
               "regime": current_regime(fetcher) if fetcher is not False else "unknown", "note": NOTE}
    registry.log_forward(trial["trial_id"], "check", details)

    # breaches and flags are recorded once each; they never change the strategy or stop the trial
    seen = {r["details"].get("type") for r in registry.forward_log(trial["trial_id"], "breach", limit=50) + registry.forward_log(trial["trial_id"], "flag", limit=50)}
    n = fwd.get("n_trades") or 0
    if (fwd.get("max_drawdown") or 0) > criteria.breach_drawdown and "drawdown" not in seen:
        registry.log_forward(trial["trial_id"], "breach", {"type": "drawdown", "value": fwd["max_drawdown"], "limit": criteria.breach_drawdown,
                                                         "action": "recorded only; the strategy is not modified"})
    if n >= 10 and (fwd.get("total_return") or 0) < 0 and "losing" not in seen:
        registry.log_forward(trial["trial_id"], "breach", {"type": "losing", "value": fwd.get("total_return"), "trades": n,
                                                         "action": "failure recorded; the strategy continues unchanged to the horizon"})
    if ((fwd.get("sharpe") or 0) >= criteria.exceptional_sharpe and fwd["days_elapsed"] >= criteria.exceptional_min_days and n >= criteria.exceptional_min_trades
            and "exceptional" not in seen):
        registry.log_forward(trial["trial_id"], "flag", {"type": "exceptional", "sharpe": fwd["sharpe"],
                                                       "action": "FLAGGED FOR HUMAN REVIEW. Not promoted: this is a near-miss and cannot go live. Check for bugs before believing it."})
    status = registry.forward_status(trial["trial_id"])
    if verdict["outcome"] != "RUNNING" and status == "RUNNING":
        registry.log_forward(trial["trial_id"], "status", {"status": verdict["outcome"], "reasons": verdict["reasons"], "final_metrics": fwd,
                                                          "forward_significance": pval, "ledger_head": ledger.head})
        registry.log_decision("forward_trial", f"Forward trial for {trial['strategy_id']} v{trial['version']} finished: {verdict['outcome']}. {NOTE}",
                              trial["strategy_id"], trial["version"])
    return details


def audit(registry, trial):
    """Everything needed to verify a trial was run as pre-registered. Returns {"ok", "problems", "facts"}."""
    problems, paths = [], trial_paths(trial)
    try:
        with open(paths["strategy"], "r", encoding="utf-8") as f:
            actual = sha256_text(f.read())
        if actual != trial["code_sha256"]:
            problems.append("the strategy file differs from the frozen version")
    except OSError:
        problems.append("the frozen strategy file is missing")
    if ForwardCriteria(**trial["criteria"]).fingerprint() != trial["criteria_fingerprint"]:
        problems.append("the stored evaluation criteria do not match their fingerprint")
    meta = registry.get(trial["strategy_id"])
    stored = registry.get_version(trial["strategy_id"], trial["version"])
    if not stored or stored["content_hash"] != trial["content_hash"]:
        problems.append("the registry version no longer matches the frozen content hash")
    if registry.status(trial["strategy_id"], trial["version"]) != "rejected":
        problems.append("the strategy's research status is not 'rejected' (it must remain a near-miss)")
    ledger = Ledger(paths["ledger"])
    ok, bad = ledger.verify()
    problems += bad
    committed = [r["details"].get("ledger_head") for r in registry.forward_log(trial["trial_id"], "check", limit=2000)]
    chain = {rec["hash"] for rec in ledger.records()} | {"0" * 64}
    for head in committed:
        if head and head not in chain:
            problems.append("a ledger head committed to the registry no longer appears in the ledger (records were removed or rewritten)")
            break
    live_refusal = registry.has_forward_trial(trial["strategy_id"], trial["version"])
    return {"ok": not problems, "problems": problems,
            "facts": {"trial_id": trial["trial_id"], "strategy": f"{trial['strategy_id']} v{trial['version']}", "start": _iso(trial["start_epoch"]),
                      "protocol_hash": protocol_hash(trial), "code_sha256": trial["code_sha256"], "criteria_fingerprint": trial["criteria_fingerprint"],
                      "ledger_records": len(ledger.records()), "ledger_head": ledger.head, "can_never_go_live": live_refusal,
                      "research_status": registry.status(trial["strategy_id"], trial["version"]), "dry_run_only": True}}


# ---------------------------------------------------------------- process supervision
class ForwardSupervisor:
    """Keeps every RUNNING trial alive and recorded; survives restarts because all state is in the registry and trial folders."""

    def __init__(self, registry, check_every=3600, alive=SUP.process_alive, clock=time.time, launcher=None, fetcher=None, risk_engine=None):
        self.registry, self.check_every, self.alive, self.clock = registry, check_every, alive, clock
        self.launcher, self.fetcher = launcher or self._launch, fetcher
        self.risk = risk_engine or RK.RiskEngine()
        self._last_check, self._restarts = {}, {}

    # --- process control
    def _pid(self, paths):
        try:
            with open(paths["pid"], "r", encoding="utf-8") as f:
                return int(f.read().strip())
        except Exception:
            return None

    def _launch(self, trial):
        import subprocess
        paths = trial_paths(trial)
        cfg = A.build_config(list(self.risk.limits.allowed_pairs), trial["params"]["timeframe"], WALLET, WALLET * self.risk.limits.max_position_pct,
                             self.risk.limits.max_open_positions)
        cfg["db_url"] = "sqlite:///" + paths["db"].replace("\\", "/")
        cfg["initial_state"] = "running"
        A.assert_safe_config(cfg)
        with open(paths["config"], "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, sort_keys=True)
        self.write_gate(trial)
        args = [A.FT_EXE, "trade", "--dry-run", "--config", paths["config"], "--userdir", A.USER_DIR, "--strategy", trial["selection"]["class_name"],
                "--strategy-path", paths["strategy_dir"], "--db-url", cfg["db_url"]]
        if "--dry-run" not in args:
            raise RuntimeError("refusing to launch a forward trial without --dry-run")
        env = dict(os.environ, JARVIS_RISK_GATE_FILE=paths["gate"], JARVIS_SIGNAL_LOG=paths["signals"])
        for k in ("FREQTRADE__EXCHANGE__KEY", "FREQTRADE__EXCHANGE__SECRET"):
            env.pop(k, None)
        with open(paths["log"], "ab") as log:   # the child keeps its own inherited handle; the parent must not leak one
            proc = subprocess.Popen(args, env=env, stdout=log, stderr=log, cwd=A.LAB_DIR)
        with open(paths["pid"], "w", encoding="utf-8") as f:
            f.write(str(proc.pid))
        return proc.pid

    def stop(self, trial):
        import subprocess
        paths = trial_paths(trial)
        pid = self._pid(paths)
        if pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        if os.path.exists(paths["pid"]):
            os.remove(paths["pid"])

    def write_gate(self, trial):
        paths = trial_paths(trial)
        rows = read_trade_rows(paths["db"])
        closed = closed_trades(rows, 0)
        equity = WALLET + sum(t["raw_profit_abs"] for t in closed)
        opens = [{"pair": r["pair"], "value": float(r["stake_amount"] or 0)} for r in rows if r["is_open"]]
        peak = max([WALLET] + [WALLET + sum(t["raw_profit_abs"] for t in closed[:i + 1]) for i in range(len(closed))])
        snap = self.risk.gate_snapshot(RK.PortfolioState(equity=equity, peak_equity=peak, start_of_day_equity=equity, open_positions=opens))
        os.makedirs(paths["dir"], exist_ok=True)
        with open(paths["gate"], "w", encoding="utf-8") as f:
            json.dump(snap, f)

    # --- one supervision pass
    def tick(self):
        actions = []
        trials = self.registry.forward_trials()
        for trial in trials:
            tid = trial["trial_id"]
            if self.registry.forward_status(tid) != "RUNNING":
                continue
            paths = trial_paths(trial)
            problems = audit_light(trial)
            if problems:
                self.registry.log_forward(tid, "integrity", {"problems": problems})
                self.registry.log_forward(tid, "status", {"status": "INVALIDATED", "reasons": problems})
                self.stop(trial)
                actions.append(f"T{tid} INVALIDATED: {problems[0]}")
                continue
            if not self.alive(self._pid(paths)):
                recent = [t for t in self._restarts.get(tid, []) if self.clock() - t < 3600]
                self._restarts[tid] = recent
                if len(recent) < 5:
                    try:
                        self.launcher(trial)
                        self._restarts[tid].append(self.clock())
                        self.registry.log_forward(tid, "note", {"event": "process (re)started", "ts": _iso(self.clock())})
                        actions.append(f"T{tid} started")
                    except Exception as error:
                        actions.append(f"T{tid} could not start: {error}")
            try:
                self.write_gate(trial)
                regime = current_regime(self.fetcher) if self.fetcher is not False else "unknown"
                sync_ledger(self.registry, trial, self.clock(), regime)
                if self.clock() - self._last_check.get(tid, 0) >= self.check_every:
                    self._last_check[tid] = self.clock()
                    check(self.registry, trial, self.clock(), self.fetcher, len(trials))
                    if self.registry.forward_status(tid) != "RUNNING":
                        self.stop(trial)
                        actions.append(f"T{tid} finished: {self.registry.forward_status(tid)}")
                        write_report(self.registry, trial["set_id"])
            except Exception as error:
                actions.append(f"T{tid} supervision error: {error}")
        return actions


def audit_light(trial):
    """Cheap per-tick integrity check: the frozen strategy file must still hash to what was recorded."""
    paths = trial_paths(trial)
    try:
        with open(paths["strategy"], "r", encoding="utf-8") as f:
            if sha256_text(f.read()) != trial["code_sha256"]:
                return ["the frozen strategy file was modified"]
    except OSError:
        return ["the frozen strategy file is missing"]
    return []


# ------------------------------------------------------------------- status and report
def status_view(registry, now=None):
    """Read-only summary for the HUD and chat. Works in any Python (no pandas needed)."""
    now = float(now if now is not None else time.time())
    trials = registry.forward_trials()
    rows = []
    for t in trials:
        status = registry.forward_status(t["trial_id"])
        checks = registry.forward_log(t["trial_id"], "check", limit=1)
        last = checks[0]["details"] if checks else {}
        m = last.get("metrics") or {}
        comp = last.get("comparisons") or {}
        end = None
        if status in TERMINAL:
            fin = registry.forward_log(t["trial_id"], "status", limit=1)
            end = fin[0]["ts"] if fin else None
        rows.append({"trial_id": t["trial_id"], "set_id": t["set_id"], "strategy": f"{t['strategy_id']} v{t['version']}", "template": t["selection"].get("template"),
                     "start": _iso(t["start_epoch"]), "days_elapsed": (now - t["start_epoch"]) / 86400.0 if status not in TERMINAL else m.get("days_elapsed"),
                     "status": status, "trades": m.get("n_trades", 0), "pnl_pct": m.get("total_return"), "max_drawdown": m.get("max_drawdown"),
                     "sharpe": m.get("sharpe"), "win_rate": m.get("win_rate"), "losing_streak": m.get("longest_losing_streak"),
                     "open_positions": last.get("open_positions", 0), "vs_oos": comp.get("vs_out_of_sample"), "vs_benchmark": comp.get("vs_benchmark"),
                     "interim": (last.get("interim_or_final") or {}), "regime": last.get("regime"),
                     "flags": [r["details"] for r in registry.forward_log(t["trial_id"], "flag", limit=5)],
                     "breaches": [r["details"] for r in registry.forward_log(t["trial_id"], "breach", limit=5)],
                     "oos_sharpe": t["selection"].get("oos_sharpe"), "last_check": checks[0]["ts"] if checks else None, "ended": end,
                     "can_never_go_live": True})
    return {"trials": rows, "set_ids": sorted({t["set_id"] for t in trials}),
            "title": "FORWARD PAPER TRIAL", "note": NOTE if trials else "No forward trial has been started."}


def write_report(registry, set_id=None, out_dir=None):
    """Plain-language report: does live forward performance support or contradict the historical evidence?"""
    trials = registry.forward_trials(set_id)
    if not trials:
        return None
    out_dir = out_dir or REPORT_DIR
    os.makedirs(out_dir, exist_ok=True)
    set_id = set_id or trials[0]["set_id"]
    outcomes = {}
    lines = [f"# Forward paper trial report: {set_id}", "",
             "These strategies are **near-misses**: they were rejected by the historical research gates only because their out-of-sample "
             "evidence could not clear the multiple-testing significance bar. This trial measures how they behave on live data they have "
             "never seen. **It does not change their status and none of them can be promoted to live because of it.**", ""]
    for t in trials:
        status = registry.forward_status(t["trial_id"])
        outcomes[status] = outcomes.get(status, 0) + 1
        checks = registry.forward_log(t["trial_id"], "check", limit=1)
        d = checks[0]["details"] if checks else {}
        m, comp, p = d.get("metrics") or {}, d.get("comparisons") or {}, d.get("forward_significance") or {}
        a = audit(registry, t)
        fmt = lambda x, pct=False: "-" if x is None else (f"{x:.1%}" if pct else f"{x:.2f}")
        lines += [f"## T{t['trial_id']}: {t['strategy_id']} v{t['version']} ({t['selection'].get('template')})", "",
                  f"- Started (hard timestamp): {_iso(t['start_epoch'])}; days elapsed {fmt(m.get('days_elapsed'))}; **status {status}**",
                  f"- Why selected: {t['selection'].get('reason')}",
                  f"- Forward results (after fees and assumed slippage): {m.get('n_trades', 0)} trades, net return {fmt(m.get('total_return'), True)}, "
                  f"Sharpe {fmt(m.get('sharpe'))}, max drawdown {fmt(m.get('max_drawdown'), True)}, win rate {fmt(m.get('win_rate'), True)}, "
                  f"longest losing streak {m.get('longest_losing_streak')}, fees {fmt(m.get('fees_total'))} USDT",
                  f"- Versus out-of-sample: Sharpe {fmt((comp.get('vs_out_of_sample') or {}).get('forward_sharpe'))} vs {fmt((comp.get('vs_out_of_sample') or {}).get('oos_sharpe'))}; "
                  f"drawdown {fmt((comp.get('vs_out_of_sample') or {}).get('forward_max_drawdown'), True)} vs {fmt((comp.get('vs_out_of_sample') or {}).get('oos_max_drawdown'), True)}",
                  f"- Versus original backtest: training Sharpe {fmt((comp.get('vs_original_backtest') or {}).get('training_sharpe'))}, validation Sharpe {fmt((comp.get('vs_original_backtest') or {}).get('validation_sharpe'))}",
                  f"- Versus buy-and-hold of the universe over the same window: return {fmt((comp.get('vs_benchmark') or {}).get('forward_return'), True)} vs {fmt((comp.get('vs_benchmark') or {}).get('benchmark_return'), True)}",
                  f"- Forward significance (corrected for {len(trials)} strategies): p = {p.get('p_value')} (needs < {p.get('alpha_corrected')})",
                  f"- Verdict: {(d.get('interim_or_final') or {}).get('outcome')} - {'; '.join((d.get('interim_or_final') or {}).get('reasons', []))}",
                  f"- Audit: {'OK' if a['ok'] else 'PROBLEMS: ' + '; '.join(a['problems'])}; protocol {a['facts']['protocol_hash'][:16]}, ledger head {a['facts']['ledger_head'][:16]} ({a['facts']['ledger_records']} records)", ""]
        for flag in [r["details"] for r in registry.forward_log(t["trial_id"], "flag", limit=5)] + [r["details"] for r in registry.forward_log(t["trial_id"], "breach", limit=5)]:
            lines.append(f"  - recorded: {flag.get('type')}: {flag.get('action')}")
    supports, contradicts = outcomes.get("COMPLETE_SUPPORTS", 0), outcomes.get("COMPLETE_CONTRADICTS", 0)
    done = sum(outcomes.get(k, 0) for k in TERMINAL)
    lines += ["", "## Overall", "", f"{done} of {len(trials)} trials are complete: {supports} support the historical evidence, {contradicts} contradict it, "
              f"{outcomes.get('COMPLETE_INCONCLUSIVE', 0)} are inconclusive, {outcomes.get('INVALIDATED', 0)} were invalidated, {outcomes.get('RUNNING', 0)} still running.",
              "Whatever the outcome, none of these strategies is a live candidate. A strategy that does well here would have to re-enter the research "
              "process from the beginning, with a new version, under the unchanged gates."]
    text = "\n".join(lines)
    with open(os.path.join(out_dir, f"forward_trial_{set_id}.md"), "w", encoding="utf-8") as f:
        f.write(text)
    return text


def main(argv=None):
    import argparse
    from . import registry as R
    ap = argparse.ArgumentParser(description="Forward-only paper trial of near-miss strategies (dry-run only)")
    ap.add_argument("command", choices=["select", "start", "status", "report", "audit"])
    args = ap.parse_args(argv)
    reg = R.Registry()
    if args.command == "select":
        print(json.dumps(select_near_misses(reg), indent=2, default=str))
    elif args.command == "start":
        res = start_set(reg)
        print(json.dumps({"set_id": res["set_id"], "started": [f"{t['strategy_id']} v{t['version']}" for t in res["started"]]}, indent=2))
    elif args.command == "status":
        print(json.dumps(status_view(reg), indent=2, default=str))
    elif args.command == "report":
        print(write_report(reg))
    elif args.command == "audit":
        print(json.dumps([audit(reg, t) for t in reg.forward_trials()], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
