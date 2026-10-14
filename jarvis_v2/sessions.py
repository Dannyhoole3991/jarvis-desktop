"""
Every Claude Code session on this PC: what it is, what it's doing, and whether it needs Danny.

Sources (read only, written live by other processes, so every parse tolerates half-written files):
  - ~/.claude/sessions/<pid>.json : one per live `claude` process (the "roster"): sessionId, cwd,
    name, status (busy / idle / waiting / shell), waitingFor (e.g. "permission prompt"),
    and on Windows a named-pipe messagingSocketPath for posting a message into it.
    The <pid>.*.key files beside them are private to their session and are NEVER read.
  - ~/.claude/projects/<cwd with every non-alphanumeric as '-'>/<sessionId>.jsonl : the transcript.

States, per conversation:
  needs_you  waiting on Danny (a permission prompt, a question, a dialog)
  working    busy
  idle       finished its turn
  gone       its process has exited
  fresh      started but never prompted (not announced)

Jarvis's own processes (his chat brain, and the runs he tracks separately) are excluded.

WINDOWS NOTE: os.kill(pid, 0) does NOT test a process on Windows -- it TERMINATES it.
Liveness is checked with OpenProcess/GetExitCodeProcess instead.
"""

import ctypes
import glob
import json
import os
import re
import time

from . import config

CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude")
SESSIONS_DIR = os.path.join(CLAUDE_DIR, "sessions")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
HUMAN_HAND = ("permission", "dialog", "trust", "login", "auth")
QUESTION_TOOLS = ("AskUserQuestion", "ExitPlanMode")
URL_RE = re.compile(r"\S+://\S+|\bwww\.\S+")

NEEDS_YOU, WORKING, IDLE, GONE, FRESH, SHELL, UNKNOWN = "needs_you", "working", "idle", "gone", "fresh", "shell", "unknown"


def pid_alive(pid):
    """True if the process exists. Never signals or touches it."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok) and code.value == 259             # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def encode_cwd(cwd):
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def roster(sessions_dir=None):
    """Every parseable <pid>.json, newest first."""
    out = []
    for path in glob.glob(os.path.join(sessions_dir or SESSIONS_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            out.append({"pid": int(d["pid"]), "session_id": str(d["sessionId"]), "cwd": str(d["cwd"]),
                        "name": d.get("name") or "", "status": d.get("status"), "waiting_for": d.get("waitingFor") or None,
                        "kind": d.get("kind") or "", "entrypoint": d.get("entrypoint") or "",
                        "pipe": d.get("messagingSocketPath") or None,
                        "updated": (d.get("statusUpdatedAt") or d.get("updatedAt") or 0) / 1000.0,
                        "started": (d.get("startedAt") or 0) / 1000.0})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return sorted(out, key=lambda e: -e["updated"])


def _tail_lines(path, nbytes=256_000):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    if size > nbytes:
        lines = lines[1:]                                   # first line is probably cut
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _text_of(message):
    content = (message or {}).get("content")
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts)


def recap(cwd, session_id, projects_dir=None):
    """What the transcript says: topic, Danny's last message, the last reply, recent tools."""
    path = os.path.join(projects_dir or PROJECTS_DIR, encode_cwd(cwd), f"{session_id}.jsonl")
    if not os.path.exists(path):
        return {"exists": False}
    last_user, last_text, tools, topic, last_ts = None, None, [], None, None
    for obj in _tail_lines(path):
        kind = obj.get("type")
        if obj.get("timestamp"):
            last_ts = obj["timestamp"]
        msg = obj.get("message") or {}
        if kind == "user" and not obj.get("isMeta"):
            content = msg.get("content")
            is_tool_result = isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            text = _text_of(msg).strip()
            if text and not is_tool_result and not text.startswith("<"):
                last_user = text
                topic = topic or text
        elif kind == "assistant":
            text = _text_of(msg).strip()
            if text:
                last_text = text
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tools.append(block.get("name"))
        elif kind == "summary" and obj.get("summary"):
            topic = obj["summary"]
    return {"exists": True, "last_user": last_user, "last_text": last_text, "recent_tools": tools[-8:],
            "topic": topic, "last_activity": last_ts, "transcript": path}


def looks_like_question(text):
    """Did it stop to ask something? URLs are ignored (a '?' in a link isn't a question)."""
    if not text:
        return False
    tail = URL_RE.sub("", text.strip())[-400:].rstrip()
    if tail.endswith("?"):
        return True
    return any(line.rstrip().endswith("?") for line in tail.splitlines()[-3:])


def derive_state(entry, rc, alive):
    if not alive:
        return GONE, None
    if not rc.get("exists"):
        return FRESH, None
    if entry.get("waiting_for"):
        return NEEDS_YOU, entry["waiting_for"]
    status = entry.get("status")
    if status == "waiting":
        return NEEDS_YOU, None
    if status == "busy":
        return WORKING, None
    if status == "shell":
        return SHELL, None
    if status is None:
        return UNKNOWN, None
    if any(t in QUESTION_TOOLS for t in rc.get("recent_tools") or []) or looks_like_question(rc.get("last_text")):
        return NEEDS_YOU, "it asked you a question"
    return IDLE, None


def _is_jarvis_own(entry):
    cwd = os.path.normcase(os.path.realpath(entry["cwd"]))
    if cwd == os.path.normcase(os.path.realpath(config.HOME_DIR)):
        return True                                          # Jarvis's chat brain
    return entry.get("entrypoint") in ("sdk-cli", "sdk-py", "sdk-ts") or entry.get("kind") in ("print", "sdk")


def snapshot(sessions_dir=None, projects_dir=None, alive=pid_alive, include_own=False):
    """One record per CONVERSATION (several processes can share one), Jarvis's own excluded."""
    by_conv = {}
    for e in roster(sessions_dir):
        if not include_own and _is_jarvis_own(e):
            continue
        by_conv.setdefault(e["session_id"], []).append(e)
    out = []
    for sid, entries in by_conv.items():
        live = [e for e in entries if alive(e["pid"])]
        primary = (live or entries)[0]
        rc = recap(primary["cwd"], sid, projects_dir)
        state, reason = derive_state(primary, rc, bool(live))
        if reason is None and state == NEEDS_YOU:
            reason = "it's waiting for you"
        project = os.path.basename(primary["cwd"].rstrip("\\/")) or primary["cwd"]
        out.append({"session_id": sid, "name": primary["name"] or (rc.get("topic") or project)[:60], "project": project,
                    "cwd": primary["cwd"], "state": state, "reason": reason, "pid": primary["pid"],
                    "entrypoint": primary["entrypoint"], "steerable": bool(primary.get("pipe")) and bool(live),
                    "pipe": primary.get("pipe"), "updated": primary["updated"],
                    "human_hand": bool(reason) and any(k in reason.lower() for k in HUMAN_HAND),
                    "topic": (rc.get("topic") or "")[:300], "last_user": (rc.get("last_user") or "")[:600],
                    "last_text": (rc.get("last_text") or "")[:1500], "recent_tools": rc.get("recent_tools") or []})
    order = {NEEDS_YOU: 0, WORKING: 1, IDLE: 2, SHELL: 3, UNKNOWN: 4, FRESH: 5, GONE: 6}
    return sorted(out, key=lambda s: (order.get(s["state"], 9), -s["updated"]))


def find(name, sessions=None):
    """The session Danny means: by its name, its project, or words from its topic."""
    sessions = sessions if sessions is not None else snapshot()
    key = (name or "").lower().strip()
    if not key:
        return None
    for field in ("name", "project"):
        exact = [s for s in sessions if s[field].lower() == key]
        if exact:
            return exact[0]
    for field in ("name", "project", "topic"):
        hits = [s for s in sessions if key in s[field].lower()]
        if hits:
            return hits[0]
    return None


def describe(sessions):
    """Spoken summary of the sessions."""
    live = [s for s in sessions if s["state"] not in (GONE, FRESH)]
    if not live:
        return "No Claude Code sessions are running apart from mine."
    parts = []
    needs = [s for s in live if s["state"] == NEEDS_YOU]
    for s in needs:
        parts.append(f"'{s['name']}' in {s['project']} needs you: {s['reason']}.")
    working = [s for s in live if s["state"] == WORKING]
    if working:
        parts.append(f"{len(working)} working: " + ", ".join(f"'{s['name']}'" for s in working) + ".")
    idle = [s for s in live if s["state"] == IDLE]
    if idle:
        parts.append(f"{len(idle)} idle: " + ", ".join(f"'{s['name']}'" for s in idle) + ".")
    return " ".join(parts)


# ---------------------------------------------------------------- steering
SENT, NOT_LIVE, REFUSED, FAILED = "sent", "not_live", "refused", "failed"


def post_message(pipe, text, timeout=5.0):
    """
    Posts one message into a session as a new turn (PEER authority, not Danny's: it can't clear a
    permission prompt). Returns sent / not_live / refused / failed. "sent" only means the bytes
    left; it can't confirm the session accepted them.
    """
    if not text or not text.strip():
        return REFUSED
    if not pipe:
        return NOT_LIVE
    payload = json.dumps({"type": "user", "message": {"role": "user", "content": text.strip()}}) + "\n"
    if pipe.startswith("\\\\.\\pipe\\"):
        try:
            with open(pipe, "w", encoding="utf-8") as f:     # a Windows named pipe opens like a file
                f.write(payload)
            return SENT
        except FileNotFoundError:
            return NOT_LIVE
        except OSError:
            return FAILED
    import socket
    if not os.path.exists(pipe):
        return NOT_LIVE
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(pipe)
        s.sendall(payload.encode("utf-8"))
        return SENT
    except (ConnectionRefusedError, FileNotFoundError):
        return NOT_LIVE
    except OSError:
        return FAILED
    finally:
        s.close()


def delivered(session, text, after, projects_dir=None):
    """Did the message actually land? True once it appears in the session's transcript after `after`."""
    rc_path = os.path.join(projects_dir or PROJECTS_DIR, encode_cwd(session["cwd"]), f"{session['session_id']}.jsonl")
    needle = text.strip()[:80]
    for obj in _tail_lines(rc_path, 64_000):
        if obj.get("type") == "user" and needle and needle in _text_of(obj.get("message")):
            return True
    return False
