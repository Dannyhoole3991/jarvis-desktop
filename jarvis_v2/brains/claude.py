"""
The main brain: one long-lived Claude Code process on Danny's subscription.

It is started once and fed one JSON line per turn over stdin (--input-format
stream-json); its stdout is a stream of JSON events (--output-format stream-json).
Keeping the process alive keeps the conversation in context and makes replies fast.

Usage limits: Claude Code reports `rate_limit_event`s. A blocking status
("rejected" etc.) means the subscription window is used up; its `resetsAt` says
when it renews. That is raised as BrainUnavailable("usage_limit", retry_at=resetsAt)
so the router can switch to a fallback brain and come back at exactly that time.
Every observation is saved to data/usage.json for the dashboard's usage bars.
"""

import json
import os
import queue
import subprocess
import threading
import time

from .. import config, persona
from ..claude_env import child_env
from . import BrainUnavailable

BLOCKING_STATUSES = {"rejected", "blocked", "exceeded", "throttled"}
AUTH_MARKERS = ("failed to authenticate", "oauth", "not logged in", "please run /login", "invalid api key", "login")
LIMIT_MARKERS = ("usage limit", "rate limit", "limit reached", "limit will reset", "out of extra usage")


def write_mcp_config():
    """data/mcp.json: tells Claude Code how to start Jarvis's tool server."""
    import sys
    config.ensure_dirs()
    cfg = {"mcpServers": {"jarvis": {"type": "stdio", "command": sys.executable, "args": ["-m", "jarvis_v2.mcp_server"],
                                     "env": {"PYTHONPATH": config.REPO_DIR, "PYTHONIOENCODING": "utf-8"}}}}
    with open(config.MCP_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return config.MCP_CONFIG_FILE


def record_usage(info, path=None):
    """Keeps the latest view of each usage window (five_hour, seven_day, ...) for the usage bars."""
    path = path or config.USAGE_FILE
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
    except Exception:
        stored = {"windows": {}}
    now = time.time()
    status = str(info.get("status") or "")
    windows = stored.setdefault("windows", {})
    for key, raw in (info.get("unifiedWindows") or {}).items():
        if isinstance(raw, dict):
            windows[key] = {"utilization": raw.get("utilization"), "resets_at": raw.get("resetsAt"),
                            "status": raw.get("status") or status, "observed_at": now}
    named = info.get("rateLimitType")
    if named and named not in (info.get("unifiedWindows") or {}):
        windows[named] = {"utilization": info.get("utilization"), "resets_at": info.get("resetsAt"), "status": status, "observed_at": now}
    stored.update(observed_at=now, status=status, rate_limit_type=named)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stored, f, indent=1)
    return stored


class ClaudeBrain:
    name = "claude"
    label = "Claude"

    def __init__(self, exe=None, model=None, popen=subprocess.Popen, clock=time.time, turn_timeout=None):
        self.exe = exe or config.find_claude_exe()
        self.model = model or config.CLAUDE_MODEL
        self.popen, self.clock = popen, clock
        self.turn_timeout = turn_timeout or config.CLAUDE_TURN_TIMEOUT
        self.proc = None
        self.events = queue.Queue()
        self.session_id = None
        self.lock = threading.Lock()
        self.last_tools = []
        self.home = None             # the folder Claude Code runs in (default: Jarvis's home)

    # ------------------------------------------------------------ process
    def command(self):
        return [self.exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--model", self.model, "--setting-sources", "project",
                "--strict-mcp-config", "--mcp-config", write_mcp_config(),
                "--append-system-prompt-file", self._write_context(),
                "--allowedTools", "mcp__jarvis__*", "Read", "Glob", "Grep", "WebSearch", "WebFetch",
                "--disallowedTools", "Edit", "Write", "NotebookEdit", "Bash", "PowerShell"]

    def _write_context(self):
        config.ensure_dirs()
        with open(config.CLAUDE_CONTEXT_FILE, "w", encoding="utf-8") as f:
            f.write(persona.context_block())
        return config.CLAUDE_CONTEXT_FILE

    def _start(self):
        persona.write_claude_home()
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        self.proc = self.popen(self.command(), cwd=self.home or config.HOME_DIR, env=child_env(), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding="utf-8", errors="replace",
                               bufsize=1, creationflags=flags)
        self.events = queue.Queue()
        threading.Thread(target=self._pump, args=(self.proc, self.events), daemon=True).start()

    @staticmethod
    def _pump(proc, events):
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                events.put(json.loads(line))
            except ValueError:
                events.put({"type": "raw", "text": line})
        events.put({"type": "_eof"})

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.alive():
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
        self.proc = None

    # --------------------------------------------------------------- turns
    def ask(self, text, history=None, preamble=""):
        """One turn. `preamble` carries what happened while Claude was away (fallback handover)."""
        with self.lock:
            for attempt in (1, 2):
                if not self.alive():
                    try:
                        self._start()
                    except OSError as error:
                        raise BrainUnavailable("offline", self.clock() + config.CLAUDE_FAILURE_RETRY_SECONDS, f"couldn't start Claude Code ({error})")
                content = (preamble + "\n\n" + text) if preamble else text
                msg = {"type": "user", "message": {"role": "user", "content": content}}
                try:
                    self.proc.stdin.write(json.dumps(msg) + "\n")
                    self.proc.stdin.flush()
                except (OSError, ValueError):
                    self.stop()
                    continue
                try:
                    return self._read_turn()
                except _Died:
                    self.stop()
                    if attempt == 2:
                        raise BrainUnavailable("error", self.clock() + 60, "Claude Code stopped unexpectedly")
            raise BrainUnavailable("error", self.clock() + 60, "Claude Code would not take the message")

    def _read_turn(self):
        deadline = time.time() + self.turn_timeout
        texts, blocked, self.last_tools = [], None, []
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                self.stop()
                raise BrainUnavailable("error", self.clock() + 30, "Claude took too long to answer")
            try:
                ev = self.events.get(timeout=remaining)
            except queue.Empty:
                continue
            kind = ev.get("type")
            if kind == "_eof":
                if blocked:
                    raise blocked
                raise _Died()
            if kind == "system" and ev.get("subtype") == "init":
                self.session_id = ev.get("session_id")
            elif kind == "rate_limit_event":
                info = dict(ev.get("rate_limit_info") or {})
                try:
                    record_usage(info)
                except Exception:
                    pass
                if str(info.get("status") or "").lower() in BLOCKING_STATUSES:
                    resets = info.get("resetsAt")
                    retry = float(resets) if isinstance(resets, (int, float)) else self.clock() + config.CLAUDE_DEFAULT_BLOCK_SECONDS
                    blocked = BrainUnavailable("usage_limit", retry, f"{info.get('rateLimitType') or 'usage'} window used up")
            elif kind == "assistant":
                for block in (ev.get("message") or {}).get("content") or []:
                    if block.get("type") == "text" and block.get("text"):
                        texts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        self.last_tools.append(block.get("name"))
            elif kind == "result":
                result_text = str(ev.get("result") or "")
                if blocked:
                    raise blocked
                if ev.get("is_error"):
                    low = result_text.lower()
                    if any(m in low for m in LIMIT_MARKERS):
                        raise BrainUnavailable("usage_limit", self.clock() + config.CLAUDE_DEFAULT_BLOCK_SECONDS, result_text[:200])
                    if any(m in low for m in AUTH_MARKERS):
                        self.stop()
                        raise BrainUnavailable("auth", self.clock() + config.CLAUDE_FAILURE_RETRY_SECONDS, result_text[:200])
                    raise BrainUnavailable("error", self.clock() + 60, result_text[:200] or "Claude returned an error")
                return (result_text or "\n".join(texts)).strip()

    def probe(self):
        """A tiny real turn: True if Claude answers (used to confirm the usage window has renewed)."""
        try:
            return bool(self.ask("Reply with the single word: ready"))
        except BrainUnavailable:
            return False


class _Died(Exception):
    pass
