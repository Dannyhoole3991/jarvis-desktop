"""
Jarvis Business Brain
=====================

Persistent memory, a deterministic decision engine, and the autonomous
DISCOVER -> RESEARCH -> EVALUATE -> SELECT -> BUILD -> LAUNCH -> MEASURE
-> ANALYSE -> IMPROVE -> SCALE/ABANDON -> LEARN loop, built as a separate
importable module (not stuffed into Jarvis_FINAL_WORKING.py) so it can
be tested and iterated on independently.

Two hard, non-negotiable boundaries shape this whole design (not a
Jarvis-specific choice -- a fixed constraint on what this assistant is
ever allowed to do, for anyone):
  1. Never spends real money or creates an account by itself. Any step
     that needs a payment, a new account, or identity verification gets
     logged to human_action_queue and the experiment pauses there,
     rather than being faked or skipped silently.
  2. Every opportunity is scored from evidence actually gathered via a
     real web search, by deterministic code -- never "the AI said this
     one's good". The reasoning for every decision is recorded.

Storage: a local SQLite DB (jarvis_business.db, gitignored like
jarvis_memory.json's data/ siblings -- this is the user's own business
data, not source code). Schema is created on first import if missing.
"""

import datetime
import json
import os
import re
import sqlite3
import threading

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_business.db")

_db_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    business_model_type TEXT NOT NULL,
    description TEXT,
    evidence_json TEXT,
    discovered_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovered',
    rejection_reason TEXT,
    demand_score REAL, competition_score REAL, startup_cost_est REAL,
    operating_cost_est REAL, margin_est REAL, revenue_est REAL,
    scalability_score REAL, automation_score REAL, time_required_score REAL,
    complexity_score REAL, platform_dependence_score REAL, risk_score REAL,
    evidence_quality_score REAL, total_score REAL,
    scoring_reasoning TEXT, scored_at TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id),
    business_model_type TEXT NOT NULL,
    hypothesis TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL,
    ended_at TEXT,
    cost_spent REAL NOT NULL DEFAULT 0,
    revenue_earned REAL NOT NULL DEFAULT 0,
    artifact_paths TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES experiments(id),
    recorded_at TEXT NOT NULL,
    traffic INTEGER, conversions INTEGER, conversion_rate REAL,
    revenue REAL, cost REAL, notes TEXT
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    experiment_id INTEGER REFERENCES experiments(id),
    opportunity_id INTEGER REFERENCES opportunities(id),
    category TEXT NOT NULL,
    lesson_text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    decision_type TEXT NOT NULL,
    opportunity_id INTEGER,
    experiment_id INTEGER,
    reasoning TEXT
);

CREATE TABLE IF NOT EXISTS human_action_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    action_type TEXT NOT NULL,
    description TEXT NOT NULL,
    opportunity_id INTEGER,
    experiment_id INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS budget (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    daily_cap REAL NOT NULL DEFAULT 2.0,
    per_experiment_cap REAL NOT NULL DEFAULT 1.0,
    total_spent_all_time REAL NOT NULL DEFAULT 0,
    spent_today REAL NOT NULL DEFAULT 0,
    spent_today_date TEXT
);

CREATE TABLE IF NOT EXISTS cycle_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    summary TEXT
);
"""


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with _db_lock:
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO budget (id, daily_cap, per_experiment_cap, total_spent_all_time, spent_today, spent_today_date) "
                "VALUES (1, 2.0, 1.0, 0, 0, ?)",
                (_today(),),
            )
            conn.commit()
        finally:
            conn.close()


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _today():
    return datetime.date.today().isoformat()


def _normalize_name(name):
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


# ============================================================
# Budget -- the one thing with real financial consequences, so this is
# plain deterministic code, not anything an LLM call could talk its way
# around.
# ============================================================

def get_budget():
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM budget WHERE id = 1").fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def can_spend(amount):
    """True if `amount` fits under BOTH the daily cap and what's left of it today."""
    budget = get_budget()
    if budget is None:
        return False
    if budget["spent_today_date"] != _today():
        return amount <= budget["daily_cap"]
    return (budget["spent_today"] + amount) <= budget["daily_cap"]


def record_spend(amount, experiment_id=None):
    """
    Actually records a spend that already happened (e.g. an OpenAI API
    call) -- this is bookkeeping, not a gate. Call can_spend() BEFORE
    doing the thing that costs money; call this AFTER, with the real (or
    best-estimate) cost.
    """
    if amount <= 0:
        return
    with _db_lock:
        conn = _connect()
        try:
            today = _today()
            row = conn.execute("SELECT spent_today, spent_today_date, total_spent_all_time FROM budget WHERE id = 1").fetchone()
            spent_today = row["spent_today"] if row["spent_today_date"] == today else 0.0
            conn.execute(
                "UPDATE budget SET spent_today = ?, spent_today_date = ?, total_spent_all_time = total_spent_all_time + ? WHERE id = 1",
                (spent_today + amount, today, amount),
            )
            if experiment_id is not None:
                conn.execute(
                    "UPDATE experiments SET cost_spent = cost_spent + ? WHERE id = ?",
                    (amount, experiment_id),
                )
            conn.commit()
        finally:
            conn.close()


def record_revenue(experiment_id, amount, notes=None):
    """
    Records REAL money Danny actually received -- Jarvis has no payment
    account of its own and never will, so this only ever gets called when
    Danny tells it he got paid (e.g. after a Fiverr/Upwork order, or an
    AdSense payout). Increments the experiment's running total rather than
    overwriting it, same pattern as record_spend.
    """
    if amount <= 0:
        return
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE experiments SET revenue_earned = revenue_earned + ? WHERE id = ?",
                (amount, experiment_id),
            )
            conn.commit()
        finally:
            conn.close()
    record_metric(experiment_id, revenue=amount, notes=notes or f"Revenue logged: £{amount:.2f}")
    log_decision("revenue", f"Logged real revenue of £{amount:.2f}." + (f" {notes}" if notes else ""), experiment_id=experiment_id)


def set_budget(daily_cap=None, per_experiment_cap=None):
    with _db_lock:
        conn = _connect()
        try:
            if daily_cap is not None:
                conn.execute("UPDATE budget SET daily_cap = ? WHERE id = 1", (daily_cap,))
            if per_experiment_cap is not None:
                conn.execute("UPDATE budget SET per_experiment_cap = ? WHERE id = 1", (per_experiment_cap,))
            conn.commit()
        finally:
            conn.close()


# ============================================================
# Logging helpers -- decisions and lessons are recorded for every
# consequential step, so Jarvis can look back at WHY before deciding
# again, and so Danny can audit it without having to ask.
# ============================================================

def log_decision(decision_type, reasoning, opportunity_id=None, experiment_id=None):
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO decisions_log (timestamp, decision_type, opportunity_id, experiment_id, reasoning) VALUES (?, ?, ?, ?, ?)",
                (_now(), decision_type, opportunity_id, experiment_id, reasoning),
            )
            conn.commit()
        finally:
            conn.close()


def has_metric_today(experiment_id):
    """
    True if a metric was already recorded for this experiment today --
    used to cap analysis to once per experiment per day, so the
    business cycle's analysis step doesn't perpetually report "something
    to do" (re-measuring the same unchanged articles) and block
    discovery/scoring/building from ever running again.
    """
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id FROM metrics WHERE experiment_id = ? AND recorded_at >= ? LIMIT 1",
                (experiment_id, _today()),
            ).fetchone()
            return row is not None
        finally:
            conn.close()


def record_metric(experiment_id, traffic=None, conversions=None, conversion_rate=None,
                   revenue=None, cost=None, notes=None):
    """
    Records one real, timestamped measurement for an experiment (e.g. a
    pageview count pulled from Cloudflare Web Analytics). Appends rather
    than overwrites -- the metrics table is a time series, so calling
    this repeatedly over an experiment's life builds a real trend, not
    just a single snapshot.
    """
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO metrics (experiment_id, recorded_at, traffic, conversions, conversion_rate, revenue, cost, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (experiment_id, _now(), traffic, conversions, conversion_rate, revenue, cost, notes),
            )
            conn.commit()
        finally:
            conn.close()


def log_lesson(category, lesson_text, opportunity_id=None, experiment_id=None):
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO lessons (created_at, experiment_id, opportunity_id, category, lesson_text) VALUES (?, ?, ?, ?, ?)",
                (_now(), experiment_id, opportunity_id, category, lesson_text),
            )
            conn.commit()
        finally:
            conn.close()


def queue_human_action(action_type, description, opportunity_id=None, experiment_id=None):
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO human_action_queue (created_at, action_type, description, opportunity_id, experiment_id) VALUES (?, ?, ?, ?, ?)",
                (_now(), action_type, description, opportunity_id, experiment_id),
            )
            conn.commit()
        finally:
            conn.close()


def resolve_human_actions_like(description_substring, status="done"):
    """Marks every human_action_queue entry containing this text as resolved -- used once Danny's actually done the thing it was asking for."""
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "UPDATE human_action_queue SET status = ? WHERE description LIKE ? AND status = 'pending'",
                (status, f"%{description_substring}%"),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()


def has_human_action_like(description_substring):
    """
    True if ANY human_action_queue entry ever (pending, done, or
    dismissed) contains this text -- used to queue a one-time request
    (e.g. "set up monetization") exactly once rather than re-queuing it
    on every single future run that would otherwise trigger it again.
    """
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id FROM human_action_queue WHERE description LIKE ? LIMIT 1",
                (f"%{description_substring}%",),
            ).fetchone()
            return row is not None
        finally:
            conn.close()


# ============================================================
# Opportunities
# ============================================================

def already_known(name, business_model_type):
    """
    True if this exact idea (same normalized name + same business model)
    has already been discovered before, regardless of what happened to
    it -- the whole point is to stop Jarvis re-proposing something
    already tried and abandoned, or already rejected, as if it were new.
    """
    normalized = _normalize_name(name)
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT id FROM opportunities WHERE normalized_name = ? AND business_model_type = ?",
                (normalized, business_model_type),
            ).fetchone()
            return row is not None
        finally:
            conn.close()


def save_opportunity(name, business_model_type, description, evidence):
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "INSERT INTO opportunities (name, normalized_name, business_model_type, description, evidence_json, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, _normalize_name(name), business_model_type, description, json.dumps(evidence), _now()),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def get_opportunity(opportunity_id):
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def list_opportunities(status=None, limit=50):
    with _db_lock:
        conn = _connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM opportunities WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM opportunities ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def set_opportunity_status(opportunity_id, status, rejection_reason=None):
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE opportunities SET status = ?, rejection_reason = ? WHERE id = ?",
                (status, rejection_reason, opportunity_id),
            )
            conn.commit()
        finally:
            conn.close()


# ============================================================
# Decision engine -- deterministic, weighted scoring over evidence the
# research step actually gathered. No step here just asks a model
# "is this good?" and trusts the answer.
# ============================================================

# Weights sum to 1.0. Favors automation/scalability/low-risk/low-cost
# over raw revenue potential, since the whole point is something Jarvis
# can actually run without Danny, not just something lucrative on paper.
SCORE_WEIGHTS = {
    "demand_score": 0.14,
    "evidence_quality_score": 0.10,
    "automation_score": 0.16,
    "scalability_score": 0.12,
    "margin_score": 0.10,
    "competition_score": 0.08,       # higher = LESS competition (inverted at scoring time)
    "startup_cost_score": 0.08,      # higher = LOWER cost (inverted at scoring time)
    "operating_cost_score": 0.06,    # higher = LOWER cost (inverted at scoring time)
    "time_required_score": 0.06,     # higher = LESS time (inverted at scoring time)
    "complexity_score": 0.04,        # higher = LESS complex (inverted at scoring time)
    "platform_dependence_score": 0.03,  # higher = LESS dependent (inverted at scoring time)
    "risk_score": 0.03,              # higher = LOWER risk (inverted at scoring time)
}


def _clamp01to10(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 5.0
    return max(0.0, min(10.0, value))


def score_opportunity(opportunity_id, evidence):
    """
    `evidence` is a dict of 0-10 ratings plus cost/revenue estimates,
    gathered by a real web-search-backed research call (see
    jarvis_business_research.gather_evidence in the caller) -- this
    function does NOT call any model. It just computes a weighted
    composite from numbers already gathered, so the same evidence always
    produces the same score, and the math is auditable.

    Expected keys in `evidence` (all 0-10 unless noted, missing keys
    default to a neutral 5):
      demand_score, evidence_quality_score, automation_score,
      scalability_score, margin_score, competition_score (10=little
      competition), startup_cost_score (10=very cheap to start),
      operating_cost_score (10=very cheap to run), time_required_score
      (10=very little ongoing time), complexity_score (10=very simple),
      platform_dependence_score (10=not reliant on one platform),
      risk_score (10=very low risk)
      startup_cost_est, operating_cost_est_monthly, revenue_est_monthly (£, raw estimates, not scores)
    """
    scored = {key: _clamp01to10(evidence.get(key, 5)) for key in SCORE_WEIGHTS}
    total = sum(scored[key] * weight for key, weight in SCORE_WEIGHTS.items())

    reasoning_lines = [f"{key.replace('_', ' ')}: {scored[key]:.1f}/10 (weight {weight:.0%})"
                        for key, weight in SCORE_WEIGHTS.items()]
    reasoning = (
        f"Composite score {total:.2f}/10.\n" + "\n".join(reasoning_lines) +
        f"\nEstimated startup cost: £{evidence.get('startup_cost_est', 0):.2f}, "
        f"estimated monthly operating cost: £{evidence.get('operating_cost_est_monthly', 0):.2f}, "
        f"estimated monthly revenue: £{evidence.get('revenue_est_monthly', 0):.2f}."
    )

    with _db_lock:
        conn = _connect()
        try:
            conn.execute(
                """UPDATE opportunities SET
                    demand_score=?, competition_score=?, startup_cost_est=?, operating_cost_est=?,
                    margin_est=?, revenue_est=?, scalability_score=?, automation_score=?,
                    time_required_score=?, complexity_score=?, platform_dependence_score=?,
                    risk_score=?, evidence_quality_score=?, total_score=?,
                    scoring_reasoning=?, scored_at=?, status='scored'
                   WHERE id=?""",
                (
                    scored["demand_score"], scored["competition_score"],
                    evidence.get("startup_cost_est", 0), evidence.get("operating_cost_est_monthly", 0),
                    scored["margin_score"], evidence.get("revenue_est_monthly", 0),
                    scored["scalability_score"], scored["automation_score"],
                    scored["time_required_score"], scored["complexity_score"],
                    scored["platform_dependence_score"], scored["risk_score"],
                    scored["evidence_quality_score"], total,
                    reasoning, _now(), opportunity_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    log_decision("score", reasoning, opportunity_id=opportunity_id)
    return total, reasoning


def select_best_runnable_opportunity(runnable_types, minimum_score=5.5):
    """
    Same as select_best_opportunity, but restricted to business_model_types
    the caller can actually execute (see BUSINESS_RUNNABLE_TYPES in
    Jarvis_FINAL_WORKING.py) -- discovery can surface other kinds of ideas
    for honest comparison, but only a runnable one is ever actually
    selected to run.
    """
    if not runnable_types:
        return None
    placeholders = ",".join("?" for _ in runnable_types)
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                f"SELECT * FROM opportunities WHERE status = 'scored' AND total_score >= ? "
                f"AND business_model_type IN ({placeholders}) ORDER BY total_score DESC LIMIT 1",
                (minimum_score, *runnable_types),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def select_best_opportunity(minimum_score=5.5):
    """
    Best-scored opportunity that's actually ready to try: status='scored'
    (not already selected/testing/active/abandoned/rejected), above the
    minimum bar. Returns None if nothing qualifies -- caller should then
    discover more rather than force a bad pick.
    """
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT * FROM opportunities WHERE status = 'scored' AND total_score >= ? ORDER BY total_score DESC LIMIT 1",
                (minimum_score,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


# ============================================================
# Experiments
# ============================================================

def start_experiment(opportunity_id, business_model_type, hypothesis):
    with _db_lock:
        conn = _connect()
        try:
            cursor = conn.execute(
                "INSERT INTO experiments (opportunity_id, business_model_type, hypothesis, started_at) VALUES (?, ?, ?, ?)",
                (opportunity_id, business_model_type, hypothesis, _now()),
            )
            conn.execute("UPDATE opportunities SET status = 'testing' WHERE id = ?", (opportunity_id,))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()


def update_experiment(experiment_id, **fields):
    if not fields:
        return
    allowed = {"status", "ended_at", "artifact_paths", "notes", "revenue_earned"}
    sets, values = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        sets.append(f"{key} = ?")
        values.append(json.dumps(value) if key == "artifact_paths" else value)
    if not sets:
        return
    values.append(experiment_id)
    with _db_lock:
        conn = _connect()
        try:
            conn.execute(f"UPDATE experiments SET {', '.join(sets)} WHERE id = ?", values)
            conn.commit()
        finally:
            conn.close()


def get_experiment(experiment_id):
    with _db_lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def list_experiments(status=None, limit=50):
    with _db_lock:
        conn = _connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM experiments WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM experiments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def active_experiment():
    """The one experiment currently 'running', if any -- the loop only ever runs one at a time for now."""
    rows = list_experiments(status="running", limit=1)
    return rows[0] if rows else None


# ============================================================
# Dashboard summary -- one call for the HUD's Business tab.
# ============================================================

def summary():
    with _db_lock:
        conn = _connect()
        try:
            budget = dict(conn.execute("SELECT * FROM budget WHERE id = 1").fetchone())
            today = _today()
            spent_today = budget["spent_today"] if budget["spent_today_date"] == today else 0.0

            revenue_total = conn.execute("SELECT COALESCE(SUM(revenue_earned), 0) AS r FROM experiments").fetchone()["r"]
            cost_total = conn.execute("SELECT COALESCE(SUM(cost_spent), 0) AS c FROM experiments").fetchone()["c"]

            opp_counts = {row["status"]: row["n"] for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM opportunities GROUP BY status"
            ).fetchall()}
            exp_counts = {row["status"]: row["n"] for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM experiments GROUP BY status"
            ).fetchall()}

            recent_lessons = [dict(r) for r in conn.execute(
                "SELECT * FROM lessons ORDER BY id DESC LIMIT 8"
            ).fetchall()]
            recent_decisions = [dict(r) for r in conn.execute(
                "SELECT * FROM decisions_log ORDER BY id DESC LIMIT 15"
            ).fetchall()]
            pending_human_actions = [dict(r) for r in conn.execute(
                "SELECT * FROM human_action_queue WHERE status = 'pending' ORDER BY id DESC"
            ).fetchall()]
            active_experiments = [dict(r) for r in conn.execute(
                "SELECT e.*, o.name AS opportunity_name, "
                "(SELECT traffic FROM metrics m WHERE m.experiment_id = e.id ORDER BY m.id DESC LIMIT 1) AS latest_traffic "
                "FROM experiments e "
                "JOIN opportunities o ON o.id = e.opportunity_id "
                "WHERE e.status IN ('running', 'review') ORDER BY e.id DESC"
            ).fetchall()]

            return {
                "target_daily_revenue": [80, 100],
                "budget": {
                    "daily_cap": budget["daily_cap"],
                    "spent_today": spent_today,
                    "per_experiment_cap": budget["per_experiment_cap"],
                    "total_spent_all_time": budget["total_spent_all_time"],
                },
                "revenue_total": revenue_total,
                "cost_total": cost_total,
                "profit_total": revenue_total - cost_total,
                "opportunity_counts": opp_counts,
                "experiment_counts": exp_counts,
                "active_experiments": active_experiments,
                "recent_lessons": recent_lessons,
                "recent_decisions": recent_decisions,
                "pending_human_actions": pending_human_actions,
            }
        finally:
            conn.close()


init_db()
