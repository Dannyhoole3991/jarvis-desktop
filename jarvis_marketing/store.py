"""
The marketing department's records: one SQLite file per profile (mock / live), so simulated data
can never mix with real data.

Every knowledge record carries a label (fact / estimate / hypothesis / prediction / verified),
a timestamp, the model version that made it and whether it is SIMULATED. The audit table is
append-only (triggers refuse UPDATE and DELETE). Columns ending in _json hold JSON and are
decoded on the way out.
"""

import contextlib
import json
import re
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL, profile TEXT NOT NULL, created_at REAL NOT NULL);

CREATE TABLE IF NOT EXISTS controls (
    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at REAL NOT NULL, updated_by TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, business_id INTEGER, actor TEXT NOT NULL, action TEXT NOT NULL,
    subject TEXT, outcome TEXT NOT NULL, details_json TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;

CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL,
    started_at REAL NOT NULL, finished_at REAL, task_count INTEGER NOT NULL DEFAULT 0, note TEXT);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, cycle_id INTEGER, agent TEXT NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, idem_key TEXT UNIQUE NOT NULL, input_json TEXT, output_json TEXT, error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL, timeout_s REAL NOT NULL,
    lease_token TEXT, lease_until REAL, created_at REAL NOT NULL, started_at REAL, finished_at REAL);

CREATE TABLE IF NOT EXISTS fetches (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, adapter TEXT NOT NULL, url TEXT NOT NULL, ok INTEGER NOT NULL,
    fetched_at REAL NOT NULL, content_hash TEXT, snapshot TEXT, simulated INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, subject_type TEXT NOT NULL, subject_key TEXT NOT NULL,
    label TEXT NOT NULL, field TEXT NOT NULL, claim TEXT NOT NULL, value_json TEXT, unit TEXT,
    source_url TEXT, source_name TEXT, fetch_id INTEGER, quote TEXT, basis TEXT, observed_at REAL,
    collected_at REAL NOT NULL, confidence REAL NOT NULL, simulated INTEGER NOT NULL, model_version TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS evidence_subject ON evidence (subject_type, subject_key);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, keyword TEXT NOT NULL, status TEXT NOT NULL,
    rank INTEGER, score REAL, data_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    simulated INTEGER NOT NULL, UNIQUE (business_id, keyword));

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, opportunity_id INTEGER, supplier TEXT NOT NULL,
    supplier_product_id TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, score REAL,
    data_json TEXT, reasons_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    simulated INTEGER NOT NULL, model_version TEXT, UNIQUE (business_id, supplier, supplier_product_id));

CREATE TABLE IF NOT EXISTS briefs (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL, concept TEXT NOT NULL,
    status TEXT NOT NULL, label TEXT NOT NULL, data_json TEXT NOT NULL, created_at REAL NOT NULL,
    model_version TEXT NOT NULL, simulated INTEGER NOT NULL, UNIQUE (candidate_id, concept));

CREATE TABLE IF NOT EXISTS creatives (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, brief_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
    provider TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL, creative_uid TEXT UNIQUE NOT NULL,
    files_json TEXT, checks_json TEXT, spec_json TEXT, log TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    simulated INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS qa_reviews (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, creative_id INTEGER NOT NULL, passed INTEGER NOT NULL,
    overall REAL NOT NULL, scores_json TEXT NOT NULL, fixes_json TEXT, label TEXT NOT NULL, created_at REAL NOT NULL,
    model_version TEXT NOT NULL, simulated INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL, kind TEXT NOT NULL,
    status TEXT NOT NULL, idem_key TEXT UNIQUE NOT NULL, data_json TEXT NOT NULL, verdict TEXT,
    result_json TEXT, created_at REAL NOT NULL, started_at REAL, ended_at REAL, simulated INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, experiment_id INTEGER NOT NULL, creative_id INTEGER NOT NULL,
    platform TEXT NOT NULL, content_hash TEXT NOT NULL, caption TEXT NOT NULL, link TEXT, status TEXT NOT NULL,
    approval_id INTEGER, external_ref TEXT, error TEXT, created_at REAL NOT NULL, published_at REAL,
    simulated INTEGER NOT NULL, UNIQUE (platform, content_hash));

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, action TEXT NOT NULL, subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL, payload_hash TEXT NOT NULL, summary TEXT NOT NULL, status TEXT NOT NULL,
    requested_by TEXT NOT NULL, requested_at REAL NOT NULL, decided_by TEXT, decided_at REAL, note TEXT,
    expires_at REAL, used_at REAL);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, experiment_id INTEGER, post_id INTEGER, source TEXT NOT NULL,
    metric TEXT NOT NULL, value REAL, available INTEGER NOT NULL, collected_at REAL NOT NULL, observed_at REAL,
    simulated INTEGER NOT NULL, UNIQUE (source, post_id, metric, collected_at));

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, order_ref TEXT NOT NULL, created_at REAL NOT NULL,
    total REAL NOT NULL, currency TEXT NOT NULL, items_json TEXT, attribution_json TEXT, post_id INTEGER,
    source TEXT NOT NULL, collected_at REAL NOT NULL, simulated INTEGER NOT NULL, UNIQUE (business_id, order_ref));

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, experiment_id INTEGER, label TEXT NOT NULL, text TEXT NOT NULL,
    topic TEXT, direction INTEGER NOT NULL DEFAULT 0, evidence_json TEXT NOT NULL, created_at REAL NOT NULL,
    model_version TEXT NOT NULL, simulated INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, kind TEXT NOT NULL, period TEXT NOT NULL, text TEXT NOT NULL,
    data_json TEXT, created_at REAL NOT NULL, UNIQUE (business_id, kind, period));

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY, business_id INTEGER, ts REAL NOT NULL, level TEXT NOT NULL, text TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, source TEXT NOT NULL, call TEXT NOT NULL, ok INTEGER NOT NULL,
    items INTEGER, error TEXT, started_at REAL NOT NULL, finished_at REAL NOT NULL, simulated INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS source_runs_source ON source_runs (business_id, source, started_at);

CREATE TABLE IF NOT EXISTS ads (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
    keyword TEXT, advertiser TEXT, data_json TEXT NOT NULL, fetch_id INTEGER, first_seen REAL NOT NULL,
    last_seen REAL NOT NULL, simulated INTEGER NOT NULL, UNIQUE (business_id, source, external_id));

CREATE TABLE IF NOT EXISTS ad_analyses (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, ad_id INTEGER NOT NULL, keyword TEXT, data_json TEXT NOT NULL,
    created_at REAL NOT NULL, model_version TEXT NOT NULL, simulated INTEGER NOT NULL, UNIQUE (ad_id, model_version));

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, candidate_id INTEGER UNIQUE NOT NULL, status TEXT NOT NULL,
    data_json TEXT NOT NULL, checks_json TEXT, product_ref TEXT, handle TEXT, url TEXT, approval_id INTEGER,
    content_hash TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, simulated INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS catalog (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, key TEXT NOT NULL, source TEXT NOT NULL, title TEXT NOT NULL,
    status TEXT NOT NULL, data_json TEXT NOT NULL, verified_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    simulated INTEGER NOT NULL, UNIQUE (business_id, key));

CREATE TABLE IF NOT EXISTS channel_listings (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, product_id INTEGER NOT NULL, platform TEXT NOT NULL,
    readiness TEXT NOT NULL, score REAL, stage TEXT NOT NULL, checks_json TEXT, fixes_json TEXT, escalations_json TEXT,
    payload_json TEXT, economics_json TEXT, statuses_json TEXT, recommended INTEGER NOT NULL DEFAULT 0, reasons_json TEXT,
    approval_id INTEGER, external_ref TEXT, checked_at REAL NOT NULL, simulated INTEGER NOT NULL, UNIQUE (product_id, platform));

CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY, business_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL, data_json TEXT NOT NULL,
    label TEXT NOT NULL, created_at REAL NOT NULL, model_version TEXT NOT NULL, simulated INTEGER NOT NULL);
"""

TABLES = {"businesses", "controls", "audit", "cycles", "tasks", "fetches", "evidence", "opportunities", "candidates",
          "briefs", "creatives", "qa_reviews", "experiments", "posts", "approvals", "metrics", "orders", "lessons",
          "reports", "notifications", "source_runs", "ads", "ad_analyses", "listings", "assessments", "catalog", "channel_listings"}

# Anything that looks like a credential is cut out of audit details and logs.
_SECRET_KEYS = re.compile(r"(?i)(secret|token|password|api[_-]?key|authorization|cookie|cvv|card)")
_SECRET_VALUES = re.compile(r"(shpat_[A-Za-z0-9]+|sk-[A-Za-z0-9_-]{10,}|Bearer\s+\S+)")


def redact(value):
    if isinstance(value, dict):
        return {k: ("[redacted]" if _SECRET_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUES.sub("[redacted]", value)
    return value


def _ident(name):
    if not re.fullmatch(r"[a-z_]+", name or ""):
        raise ValueError(f"bad column name {name!r}")
    return name


def _decode(row):
    if row is None:
        return None
    out = {}
    for key in row.keys():
        value = row[key]
        if key.endswith("_json"):
            if value is not None:
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            key = key[:-5]
        out[key] = value
    return out


class Store:
    def __init__(self, path, clock=time.time):
        self.path = path
        self.clock = clock
        self.lock = threading.RLock()
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextlib.contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    # ---------------------------------------------------------- generic helpers
    @staticmethod
    def _encode(fields):
        out = {}
        for k, v in fields.items():
            if k.endswith("_json") and v is not None and not isinstance(v, str):
                v = json.dumps(v, default=str, sort_keys=True)
            elif isinstance(v, bool):
                v = int(v)
            out[_ident(k)] = v
        return out

    def insert(self, table, **fields):
        if table not in TABLES:
            raise ValueError(table)
        f = self._encode(fields)
        cols = ", ".join(f)
        with self.lock, self.conn() as c:
            cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(f))})", list(f.values()))
            return cur.lastrowid

    def update(self, table, row_id, where=None, **fields):
        """Updates one row. `where` adds conditions ({"status": "running"}); returns True if the row changed."""
        if table not in TABLES:
            raise ValueError(table)
        f = self._encode(fields)
        cond = {_ident(k): v for k, v in (where or {}).items()}
        sql = (f"UPDATE {table} SET {', '.join(k + '=?' for k in f)} WHERE id=?"
               + "".join(f" AND {k}=?" for k in cond))
        with self.lock, self.conn() as c:
            return c.execute(sql, list(f.values()) + [row_id] + list(cond.values())).rowcount == 1

    def get(self, table, row_id):
        if table not in TABLES:
            raise ValueError(table)
        with self.conn() as c:
            return _decode(c.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone())

    def find(self, table, order="id", limit=1000, **where):
        if table not in TABLES:
            raise ValueError(table)
        if not re.fullmatch(r"[a-z_]+( DESC)?", order):
            raise ValueError(order)
        cond = {_ident(k): v for k, v in where.items()}
        sql = f"SELECT * FROM {table}"
        if cond:
            sql += " WHERE " + " AND ".join((f"{k} IS NULL" if v is None else
                                             f"{k} IN ({','.join('?' * len(v))})" if isinstance(v, (list, tuple, set)) else f"{k}=?")
                                            for k, v in cond.items())
        sql += f" ORDER BY {order} LIMIT {int(limit)}"
        params = []
        for v in cond.values():
            if v is None:
                continue
            params.extend(v if isinstance(v, (list, tuple, set)) else [v])
        with self.conn() as c:
            return [_decode(r) for r in c.execute(sql, params).fetchall()]

    def one(self, table, **where):
        rows = self.find(table, limit=1, **where)
        return rows[0] if rows else None

    def sql(self, query, params=()):
        """Read-only queries for reports (SELECT only)."""
        if not re.match(r"(?is)\s*select\b", query):
            raise ValueError("read-only")
        with self.conn() as c:
            return [_decode(r) for r in c.execute(query, params).fetchall()]

    # ---------------------------------------------------------- business + audit
    def business_id(self, business):
        row = self.one("businesses", slug=business["slug"])
        if row:
            return row["id"]
        try:
            return self.insert("businesses", slug=business["slug"], name=business["name"], profile=business["profile"],
                               created_at=self.clock())
        except sqlite3.IntegrityError:
            return self.one("businesses", slug=business["slug"])["id"]

    def audit(self, actor, action, outcome, subject=None, business_id=None, **details):
        return self.insert("audit", ts=self.clock(), business_id=business_id, actor=str(actor), action=action,
                           subject=subject, outcome=outcome, details_json=redact(details))

    def notify(self, text, level="info", business_id=None):
        return self.insert("notifications", business_id=business_id, ts=self.clock(), level=level, text=redact(text)[:600])
