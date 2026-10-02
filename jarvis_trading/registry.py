"""
Strategy registry: the lab's permanent, append-only record.

Guarantees (enforced by SQLite triggers, not just by convention):
  - A strategy version's code, parameters, hash and periods can never be
    edited. Changing anything creates a NEW version.
  - Experiment results, status changes and decisions are append-only: no
    UPDATE, no DELETE.
  - A rejected strategy version is terminal. To try again you create a new
    version, so the failure stays on the record.
  - Creating an identical strategy (same code + parameters + market +
    timeframe + periods) returns the existing one instead of a duplicate.

Every experiment stores what is needed to reproduce it: data hash, the exact
command, parameters, random seed and tool versions.
"""

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(os.path.dirname(HERE), "trading_lab", "jarvis_trading.db")

STATUSES = ("hypothesis", "generated", "backtested", "candidate", "rejected", "paper", "retired")
# What each status may move to. "rejected" has no exits: it is terminal for that version.
ALLOWED = {
    "hypothesis": ("generated", "rejected"),
    "generated": ("backtested", "rejected"),
    "backtested": ("candidate", "rejected"),
    "candidate": ("paper", "rejected"),
    "paper": ("retired", "rejected"),
    "retired": (),
    "rejected": (),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    hypothesis TEXT NOT NULL,
    source TEXT NOT NULL,
    market TEXT NOT NULL,
    timeframe TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id TEXT NOT NULL REFERENCES strategies(strategy_id),
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    code TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    training_period TEXT, validation_period TEXT, test_period TEXT,
    change_reason TEXT,
    PRIMARY KEY (strategy_id, version)
);
CREATE TABLE IF NOT EXISTS status_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
    status TEXT NOT NULL, reason TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL, version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    data_hash TEXT, command TEXT, params_json TEXT, seed INTEGER, tool_versions_json TEXT,
    metrics_json TEXT, passed INTEGER, notes TEXT,
    started_at TEXT, finished_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, strategy_id TEXT, version INTEGER, category TEXT NOT NULL, detail TEXT NOT NULL
);
"""

_IMMUTABLE = ("strategies", "strategy_versions", "status_log", "experiments", "decisions_log")
TRIGGERS = "".join(
    f"""
CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, 'registry is append-only: {t} rows cannot be modified'); END;
CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, 'registry is append-only: {t} rows cannot be deleted'); END;
"""
    for t in _IMMUTABLE
)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def content_hash(code, parameters, market, timeframe, periods=None):
    payload = json.dumps({"code": code, "parameters": parameters, "market": market,
                          "timeframe": timeframe, "periods": periods or {}}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RegistryError(Exception):
    pass


class Registry:
    def __init__(self, db_path=None):
        self.db_path = db_path or DEFAULT_DB
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA + TRIGGERS)

    @contextlib.contextmanager
    def _connect(self):
        # sqlite3's own "with conn:" commits but never CLOSES the connection,
        # which leaks file handles (and locks the file on Windows).
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------ creation
    def create_strategy(self, hypothesis, source, market, timeframe, parameters, code,
                        training_period=None, validation_period=None, test_period=None):
        """Returns (strategy_id, version, created). Identical content returns the existing entry (created=False)."""
        periods = {"training": training_period, "validation": validation_period, "test": test_period}
        digest = content_hash(code, parameters, market, timeframe, periods)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")  # serialise id allocation across worker processes
            existing = conn.execute(
                "SELECT v.strategy_id, v.version FROM strategy_versions v WHERE v.content_hash = ?", (digest,)
            ).fetchone()
            if existing:
                return existing["strategy_id"], existing["version"], False
            n = conn.execute("SELECT COUNT(*) AS n FROM strategies").fetchone()["n"]
            strategy_id = f"S-{n + 1:04d}"
            conn.execute("INSERT INTO strategies VALUES (?,?,?,?,?,?)",
                         (strategy_id, _now(), hypothesis, source, market, timeframe))
            conn.execute(
                "INSERT INTO strategy_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (strategy_id, 1, _now(), json.dumps(parameters, sort_keys=True), code, digest,
                 training_period, validation_period, test_period, "initial version"))
            conn.execute("INSERT INTO status_log (strategy_id, version, status, reason, ts) VALUES (?,?,?,?,?)",
                         (strategy_id, 1, "generated", "Strategy generated from hypothesis.", _now()))
            return strategy_id, 1, True

    def new_version(self, strategy_id, parameters, code, change_reason,
                    training_period=None, validation_period=None, test_period=None):
        """Never edits an existing version: appends version n+1. Returns (version, created)."""
        base = self.get(strategy_id)
        if base is None:
            raise RegistryError(f"Unknown strategy {strategy_id}")
        if not change_reason:
            raise RegistryError("A new version must say why it differs.")
        periods = {"training": training_period, "validation": validation_period, "test": test_period}
        digest = content_hash(code, parameters, base["market"], base["timeframe"], periods)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            same = conn.execute("SELECT version FROM strategy_versions WHERE content_hash = ?", (digest,)).fetchone()
            if same:
                return same["version"], False
            latest = conn.execute("SELECT MAX(version) AS v FROM strategy_versions WHERE strategy_id = ?",
                                  (strategy_id,)).fetchone()["v"]
            version = latest + 1
            conn.execute(
                "INSERT INTO strategy_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (strategy_id, version, _now(), json.dumps(parameters, sort_keys=True), code, digest,
                 training_period, validation_period, test_period, change_reason))
            conn.execute("INSERT INTO status_log (strategy_id, version, status, reason, ts) VALUES (?,?,?,?,?)",
                         (strategy_id, version, "generated", f"New version: {change_reason}", _now()))
            return version, True

    # ------------------------------------------------------------- status
    def status(self, strategy_id, version):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM status_log WHERE strategy_id=? AND version=? ORDER BY id DESC LIMIT 1",
                (strategy_id, version)).fetchone()
        return row["status"] if row else None

    def set_status(self, strategy_id, version, new_status, reason):
        if new_status not in STATUSES:
            raise RegistryError(f"Unknown status '{new_status}'")
        if not reason or not str(reason).strip():
            raise RegistryError("Every status change needs a reason.")
        with self._lock:
            current = self.status(strategy_id, version)
            if current is None:
                raise RegistryError(f"Unknown strategy version {strategy_id} v{version}")
            if new_status not in ALLOWED[current]:
                raise RegistryError(f"{strategy_id} v{version} is '{current}'; moving to '{new_status}' is not allowed.")
            with self._connect() as conn:
                conn.execute("INSERT INTO status_log (strategy_id, version, status, reason, ts) VALUES (?,?,?,?,?)",
                             (strategy_id, version, new_status, reason, _now()))

    # -------------------------------------------------------- experiments
    def record_experiment(self, strategy_id, version, kind, metrics, passed=None, notes="", data_hash=None,
                          command=None, params=None, seed=None, tool_versions=None, started_at=None):
        if self.status(strategy_id, version) is None:
            raise RegistryError(f"Unknown strategy version {strategy_id} v{version}")
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO experiments (strategy_id, version, kind, data_hash, command, params_json, seed, "
                "tool_versions_json, metrics_json, passed, notes, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (strategy_id, version, kind, data_hash, command, json.dumps(params or {}, sort_keys=True), seed,
                 json.dumps(tool_versions or {}, sort_keys=True), json.dumps(metrics, sort_keys=True, default=str),
                 None if passed is None else int(bool(passed)), notes, started_at, _now()))
            return cur.lastrowid

    def experiments(self, strategy_id, version=None, kind=None):
        query, args = "SELECT * FROM experiments WHERE strategy_id=?", [strategy_id]
        if version is not None:
            query += " AND version=?"
            args.append(version)
        if kind:
            query += " AND kind=?"
            args.append(kind)
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(query + " ORDER BY id", args).fetchall()]
        for r in rows:
            r["metrics"] = json.loads(r["metrics_json"])
        return rows

    def known_parameter_sets(self):
        """[(template, market, canonical_params_json)] for every registered version (to avoid re-testing)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.source, s.market, v.parameters_json FROM strategy_versions v JOIN strategies s ON s.strategy_id=v.strategy_id").fetchall()
        return [(r["source"].split(":", 1)[-1], r["market"], r["parameters_json"]) for r in rows]

    def all_experiments_grouped(self):
        """{(strategy_id, version): {kind: [{metrics, passed}, ...]}} in one query, for dashboards over many strategies."""
        with self._connect() as conn:
            rows = conn.execute("SELECT strategy_id, version, kind, metrics_json, passed FROM experiments ORDER BY id").fetchall()
        out = {}
        for r in rows:
            out.setdefault((r["strategy_id"], r["version"]), {}).setdefault(r["kind"], []).append(
                {"metrics": json.loads(r["metrics_json"]), "passed": r["passed"]})
        return out

    def all_experiments(self, kind):
        """Every experiment of one kind across all strategies (for cross-strategy checks such as near-duplicate detection)."""
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, strategy_id, version, metrics_json FROM experiments WHERE kind=? ORDER BY id", (kind,)).fetchall()]
        for r in rows:
            r["metrics"] = json.loads(r["metrics_json"])
        return rows

    def count_trials(self):
        """
        Parameter sets ever backtested: one per strategy version that reached its training backtest, plus the
        neighbours tried for robustness. This is the multiple-testing count.
        """
        with self._connect() as conn:
            own = conn.execute("SELECT COUNT(*) AS n FROM experiments WHERE kind='backtest_training'").fetchone()["n"]
            neighbours = conn.execute(
                "SELECT COALESCE(SUM(json_extract(metrics_json, '$.neighbours_tested')), 0) AS n FROM experiments WHERE kind='robustness'"
            ).fetchone()["n"]
        return int(own) + int(neighbours or 0)

    def log_decision(self, category, detail, strategy_id=None, version=None):
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO decisions_log (ts, strategy_id, version, category, detail) VALUES (?,?,?,?,?)",
                         (_now(), strategy_id, version, category, detail))

    # ------------------------------------------------------------ queries
    def get(self, strategy_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM strategies WHERE strategy_id=?", (strategy_id,)).fetchone()
        return dict(row) if row else None

    def get_version(self, strategy_id, version):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM strategy_versions WHERE strategy_id=? AND version=?",
                               (strategy_id, version)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["parameters"] = json.loads(d["parameters_json"])
        return d

    def versions(self, strategy_id):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT version, created_at, content_hash, change_reason FROM strategy_versions "
                "WHERE strategy_id=? ORDER BY version", (strategy_id,)).fetchall()]

    def status_history(self, strategy_id, version=None):
        query, args = "SELECT * FROM status_log WHERE strategy_id=?", [strategy_id]
        if version is not None:
            query += " AND version=?"
            args.append(version)
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(query + " ORDER BY id", args).fetchall()]

    def list_versions(self, status=None):
        """Every strategy version with its CURRENT status (and the reason for it)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT s.strategy_id, s.hypothesis, s.market, s.timeframe, v.version, v.content_hash, v.created_at, "
                "(SELECT status FROM status_log l WHERE l.strategy_id=v.strategy_id AND l.version=v.version ORDER BY l.id DESC LIMIT 1) AS status, "
                "(SELECT reason FROM status_log l WHERE l.strategy_id=v.strategy_id AND l.version=v.version ORDER BY l.id DESC LIMIT 1) AS reason "
                "FROM strategy_versions v JOIN strategies s ON s.strategy_id=v.strategy_id ORDER BY s.strategy_id, v.version"
            ).fetchall()
        out = [dict(r) for r in rows]
        return [r for r in out if status is None or r["status"] == status]

    def recent_decisions(self, limit=30):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM decisions_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
