"""
Runs: every piece of Claude Code work Jarvis starts, recorded from start to finish.

A run is one `claude -p` process working in one project folder on one prompt. It is a row in
SQLite (jarvis_v2/data/runs.db) with its project, prompt, status, tokens, cost and the full event
stream, so the Runs tab can show what happened and nothing is ever a black box.

Invariants
  - Every state change is written to the database BEFORE anything else hears about it.
  - A run always reaches an end state (succeeded / failed / cancelled / timed_out), whatever happens.
  - Runs need Claude. While Claude's allowance is used up they wait as "waiting_for_claude" and
    start by themselves when it renews (the fallback brains never write code).
  - At most MAX_CONCURRENT run at once: the Pro allowance is small and shared with the chat brain.

Safety: runs execute with --permission-mode acceptEdits (file edits inside the project are allowed,
no prompts) plus an allowlist of ordinary dev commands; the dangerous ones are denied outright.
Jarvis's OWN code is never edited in place: that goes through selfimprove.py (a separate git
worktree, tests, and Danny's approval).
"""

import contextlib
import json
import os
import sqlite3
import subprocess
import threading
import time

from . import config
from .claude_env import child_env

DB_FILE = os.path.join(config.DATA_DIR, "runs.db")
MAX_CONCURRENT = 1
DEFAULT_TIMEOUT = 45 * 60
TERMINAL = ("succeeded", "failed", "cancelled", "timed_out")
ACTIVE = ("queued", "waiting_for_claude", "running")

# Ordinary dev commands a run may use without asking. On Windows Claude Code runs commands through
# its PowerShell tool as well as Bash (measured: "py hello.py" was refused when only Bash(...) was
# allowed), so every pattern is allowed/denied for BOTH shells.
_SAFE_COMMANDS = ["git status*", "git diff*", "git log*", "git add*", "git commit*", "git checkout -b*", "git branch*",
                  "git stash*", "py *", "python *", "pip install*", "pytest*", "npm *", "npx *", "node *",
                  "ls*", "dir*", "cat *", "mkdir*", "echo *", "Get-ChildItem*", "Get-Content*", "New-Item*", "Test-Path*"]
_DANGEROUS_COMMANDS = ["git push*", "git reset --hard*", "git clean*", "rm -rf*", "rm -r *", "del *", "rmdir*", "rd *",
                       "format*", "curl*", "wget*", "shutdown*", "Remove-Item*", "Stop-Computer*", "Restart-Computer*",
                       "Invoke-WebRequest*", "Invoke-RestMethod*", "Set-ExecutionPolicy*", "reg *"]
ALLOWED_TOOLS = (["Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit", "TodoWrite", "WebSearch", "WebFetch"]
                 + [f"{shell}({c})" for c in _SAFE_COMMANDS for shell in ("Bash", "PowerShell")])
DENIED_TOOLS = [f"{shell}({c})" for c in _DANGEROUS_COMMANDS for shell in ("Bash", "PowerShell")]
MAX_EVENT_CHARS = 20000

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'run',          -- run | build | self_improve
    project TEXT NOT NULL,
    cwd TEXT NOT NULL,
    prompt TEXT NOT NULL,
    model TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    exit_code INTEGER,
    session_id TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    result TEXT,
    error TEXT,
    meta TEXT                                  -- JSON: build/self-improve details
);
CREATE TABLE IF NOT EXISTS run_events (
    run_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT,
    body TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


class RunStore:
    """The single source of truth for runs. Every path writes here; the dashboard only reads."""

    def __init__(self, path=None):
        self.path = path or DB_FILE
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextlib.contextmanager
    def _conn(self):
        """Commit on success, roll back on error, and ALWAYS close (sqlite3's own `with` doesn't close)."""
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create(self, project, cwd, prompt, kind="run", model=None, meta=None, status="queued"):
        with self.lock, self._conn() as c:
            cur = c.execute("INSERT INTO runs (kind, project, cwd, prompt, model, status, created_at, meta) VALUES (?,?,?,?,?,?,?,?)",
                            (kind, project, cwd, prompt, model, status, time.time(), json.dumps(meta or {})))
            return cur.lastrowid

    def update(self, run_id, **fields):
        if not fields:
            return
        if "meta" in fields and not isinstance(fields["meta"], str):
            fields["meta"] = json.dumps(fields["meta"])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock, self._conn() as c:
            c.execute(f"UPDATE runs SET {cols} WHERE id=?", (*fields.values(), run_id))

    def add_event(self, run_id, event):
        body = json.dumps(event, default=str)
        if len(body) > MAX_EVENT_CHARS:
            # Never chop the JSON itself (that made it unreadable): keep a valid, shortened summary instead.
            short = {"type": event.get("type"), "subtype": event.get("subtype"), "truncated": True,
                     "summary": summarise_event(event) or body[:2000]}
            if event.get("type") == "result":
                short.update({k: event.get(k) for k in ("result", "is_error", "usage", "total_cost_usd")})
                short["result"] = str(short.get("result") or "")[:MAX_EVENT_CHARS // 2]
            body = json.dumps(short, default=str)
        with self.lock, self._conn() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id=?", (run_id,)).fetchone()[0]
            c.execute("INSERT INTO run_events (run_id, seq, ts, type, body) VALUES (?,?,?,?,?)",
                      (run_id, seq, time.time(), str(event.get("type")), body))

    def get(self, run_id):
        with self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _row(row)

    def list(self, limit=50, statuses=None, project=None, kind=None):
        q, args = "SELECT * FROM runs WHERE 1=1", []
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        if project:
            q += " AND project=?"
            args.append(project)
        if kind:
            q += " AND kind=?"
            args.append(kind)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [_row(r) for r in c.execute(q, args).fetchall()]

    def events(self, run_id, since=0, limit=500):
        with self._conn() as c:
            rows = c.execute("SELECT seq, ts, type, body FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                             (run_id, since, limit)).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "type": r["type"], "event": json.loads(r["body"])} for r in rows]


def _row(row):
    if row is None:
        return None
    d = dict(row)
    try:
        d["meta"] = json.loads(d.get("meta") or "{}")
    except ValueError:
        d["meta"] = {}
    return d


def claude_available():
    """False while the router has Claude blocked (usage used up / logged out)."""
    from . import router as R
    st = R.load_state()
    b = (st.get("blocked") or {}).get("claude")
    return not (b and b.get("until", 0) > time.time())


def summarise_event(ev):
    """One short human line for the transcript view (None = not worth showing)."""
    if ev.get("truncated"):
        return ev.get("summary")
    kind = ev.get("type")
    if kind == "assistant":
        parts = []
        for block in (ev.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                parts.append(block["text"].strip())
            elif block.get("type") == "tool_use":
                inp = block.get("input") or {}
                target = inp.get("file_path") or inp.get("path") or inp.get("command") or inp.get("pattern") or ""
                parts.append(f"→ {block.get('name')} {str(target)[:120]}".rstrip())
        return "\n".join(parts) or None
    if kind == "result":
        return f"Finished: {str(ev.get('result') or '')[:600]}"
    if kind == "system" and ev.get("subtype") == "init":
        return "Claude Code started."
    return None


class Executor:
    """Starts queued runs (one at a time), streams their events into the store, and always finishes them."""

    def __init__(self, store=None, popen=subprocess.Popen, exe=None, claude_ok=claude_available, poll_seconds=5):
        self.store = store or RunStore()
        self.popen = popen
        self.exe = exe
        self.claude_ok = claude_ok
        self.poll_seconds = poll_seconds
        self.procs = {}
        self.threads = {}
        self.wake = threading.Event()
        self.listeners = []                      # callables(run_id, run) on every state change
        self._thread = None
        self._stop = False

    # ------------------------------------------------------------ public
    def submit(self, project, cwd, prompt, kind="run", model=None, meta=None, timeout=DEFAULT_TIMEOUT):
        if not os.path.isdir(cwd):
            raise ValueError(f"The project folder doesn't exist: {cwd}")
        meta = dict(meta or {}, timeout=timeout)
        run_id = self.store.create(project, cwd, prompt, kind=kind, model=model, meta=meta)
        self._notify(run_id)
        self.wake.set()
        return run_id

    def cancel(self, run_id):
        run = self.store.get(run_id)
        if not run or run["status"] in TERMINAL:
            return False
        proc = self.procs.get(run_id)
        self.store.update(run_id, status="cancelled", finished_at=time.time(), error="Cancelled by Danny")
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        self._notify(run_id)
        return True

    def retry(self, run_id):
        run = self.store.get(run_id)
        if not run:
            raise ValueError("No such run")
        return self.submit(run["project"], run["cwd"], run["prompt"], kind=run["kind"], model=run["model"],
                           meta=dict(run["meta"], retry_of=run_id))

    def start(self):
        if self._thread is None:
            self._recover()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-runs")
            self._thread.start()
        return self

    def stop(self):
        self._stop = True
        self.wake.set()
        for proc in list(self.procs.values()):
            try:
                proc.kill()
            except Exception:
                pass

    # ----------------------------------------------------------- internal
    def _notify(self, run_id):
        run = self.store.get(run_id)
        for fn in list(self.listeners):
            try:
                fn(run_id, run)
            except Exception:
                pass

    def _recover(self):
        """A run that was 'running' when Jarvis last stopped can't be resumed: mark it failed, honestly."""
        for run in self.store.list(limit=500, statuses=["running"]):
            self.store.update(run["id"], status="failed", finished_at=time.time(), error="Jarvis was restarted while this run was going")

    def _loop(self):
        while not self._stop:
            self.wake.wait(self.poll_seconds)
            self.wake.clear()
            try:
                self.tick()
            except Exception as error:
                print("runs: tick failed:", error)

    def tick(self):
        """Starts the next waiting run if there's room. Returns the started run id (or None)."""
        running = [rid for rid, p in self.procs.items() if p.poll() is None]
        if len(running) >= MAX_CONCURRENT:
            return None
        waiting = self.store.list(limit=100, statuses=["queued", "waiting_for_claude"])
        if not waiting:
            return None
        nxt = waiting[-1]                         # oldest first
        if not self.claude_ok():
            for run in waiting:
                if run["status"] == "queued":
                    self.store.update(run["id"], status="waiting_for_claude")
                    self._notify(run["id"])
            return None
        self._launch(nxt)
        return nxt["id"]

    def command(self, run):
        exe = self.exe or config.find_claude_exe()
        cmd = [exe, "-p", run["prompt"], "--output-format", "stream-json", "--verbose",
               "--permission-mode", "acceptEdits",
               "--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *DENIED_TOOLS]
        if run.get("model"):
            cmd += ["--model", run["model"]]
        return cmd

    def _launch(self, run):
        run_id = run["id"]
        try:
            proc = self.popen(self.command(run), cwd=run["cwd"], env=child_env(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                              creationflags=0x08000000 if os.name == "nt" else 0)
        except Exception as error:
            self.store.update(run_id, status="failed", finished_at=time.time(), error=f"Couldn't start Claude Code: {error}")
            self._notify(run_id)
            return
        self.procs[run_id] = proc
        self.store.update(run_id, status="running", started_at=time.time())
        self._notify(run_id)
        t = threading.Thread(target=self._drive, args=(run, proc), daemon=True, name=f"run-{run_id}")
        self.threads[run_id] = t
        t.start()

    def join(self, run_id, timeout=None):
        """Waits until a run's thread has completely finished (including its final write)."""
        t = self.threads.get(run_id)
        if t is not None:
            t.join(timeout)

    def _drive(self, run, proc):
        run_id = run["id"]
        deadline = time.time() + float((run.get("meta") or {}).get("timeout") or DEFAULT_TIMEOUT)
        timed_out = threading.Event()

        def watchdog():
            while proc.poll() is None:
                if time.time() > deadline:
                    timed_out.set()
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return
                time.sleep(2)
        threading.Thread(target=watchdog, daemon=True).start()

        result, is_error, tokens_in, tokens_out, cost, session = None, False, 0, 0, 0.0, None
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    ev = {"type": "raw", "text": line[:2000]}
                self.store.add_event(run_id, ev)
                kind = ev.get("type")
                if kind == "system" and ev.get("subtype") == "init":
                    session = ev.get("session_id")
                    self.store.update(run_id, session_id=session)
                elif kind == "rate_limit_event":
                    try:
                        from .brains.claude import record_usage
                        record_usage(dict(ev.get("rate_limit_info") or {}))
                    except Exception:
                        pass
                elif kind == "result":
                    result = str(ev.get("result") or "")
                    is_error = bool(ev.get("is_error"))
                    usage = ev.get("usage") or {}
                    tokens_in = int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
                    tokens_out = int(usage.get("output_tokens") or 0)
                    cost = float(ev.get("total_cost_usd") or 0)
            code = proc.wait()
        except Exception as error:
            code, is_error, result = -1, True, f"Jarvis lost track of the run: {error}"
        finally:
            self.procs.pop(run_id, None)

        current = self.store.get(run_id)
        if current and current["status"] == "cancelled":
            status, error = "cancelled", current.get("error")
        elif timed_out.is_set():
            status, error = "timed_out", "Took longer than allowed and was stopped"
        elif code == 0 and not is_error and result is not None:
            status, error = "succeeded", None
        else:
            status, error = "failed", (result or f"Claude Code exited with code {code}")[:2000]
            if result and any(m in result.lower() for m in ("usage limit", "limit reached", "rate limit")):
                error = "Claude's allowance ran out during this run. Retry it once it renews."
        self.store.update(run_id, status=status, finished_at=time.time(), exit_code=code, result=(result or "")[:20000],
                          error=error, input_tokens=tokens_in, output_tokens=tokens_out, cost_usd=cost)
        self._notify(run_id)
        self.wake.set()


def describe(run):
    """One spoken-style line about a run."""
    if not run:
        return "I can't find that run."
    took = ""
    if run.get("started_at"):
        end = run.get("finished_at") or time.time()
        took = f" ({int((end - run['started_at']) // 60)} min)"
    base = f"Run {run['id']} in {run['project']}: {run['status'].replace('_', ' ')}{took}."
    if run["status"] == "succeeded" and run.get("result"):
        base += " " + run["result"][:300]
    elif run.get("error"):
        base += " " + run["error"][:300]
    return base
