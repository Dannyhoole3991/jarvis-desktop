"""
Tiny always-on listener, separate from Jarvis itself, with two jobs:

1. Start the real Jarvis process on request. Has to be a separate
   process: if Jarvis is the thing that's not running, nothing inside
   Jarvis can answer a "start me" request.

2. Run the phone's "mirror" of the actual live dev conversation with
   Claude (see _run_mirror_message below) -- the fallback for fixing
   Jarvis remotely. This ALSO has to live here, not inside Jarvis
   itself: fixing Jarvis means Jarvis will likely be crashing/getting
   restarted a lot during that exact conversation, so the mirror can't
   depend on Jarvis's own engine being healthy.

Meant to be registered as a Windows Task Scheduler task that runs at
logon (pythonw, no console window) so it's always reachable whenever
Danny is logged into the PC, independent of whether Jarvis itself is
open or closed.

For now this only launches the terminal version (START_JARVIS_FINAL_WORKING.bat,
a visible console window) -- the desktop HUD version will be added later.
"""
import glob
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

LAUNCHER_PORT = int(os.environ.get("JARVIS_LAUNCHER_PORT", "8766"))
SHARED_SECRET = os.environ.get("JARVIS_PHONE_SECRET", "")
# Only needed for the poll loops below -- reaching the phone's cloud
# backend from off the tailnet, since it can't reach back in here.
JARVIS_PHONE_BACKEND_URL = os.environ.get("JARVIS_PHONE_BACKEND_URL", "").rstrip("/")
JARVIS_DIR = os.path.dirname(os.path.abspath(__file__))
JARVIS_BAT = os.path.join(JARVIS_DIR, "START_JARVIS_FINAL_WORKING.bat")
JARVIS_STATUS_URL = "http://127.0.0.1:8765/status"
# Which real dev conversation counts as "the one" to mirror -- set by
# Danny (via me, Claude) whenever a new conversation should become that
# one; not auto-detected, since guessing wrong among several open
# conversations would be worse than asking.
ACTIVE_DEV_SESSION_PATH = os.path.join(JARVIS_DIR, "_active_dev_session.json")
# Tracks the chain's current tip: each mirror message resumes+forks
# from whatever this points at, then overwrites it with the NEW fork's
# own session id -- confirmed live that chaining forks this way (each
# one resuming the previous fork's id, never the same one twice)
# preserves full conversation continuity without ever touching the
# real live session.
MIRROR_STATE_PATH = os.path.join(JARVIS_DIR, "_mirror_session_state.json")


def _find_claude_cli():
    """Same logic as _find_claude_cli() in Jarvis_FINAL_WORKING.py -- kept as
    its own copy here since this has to run independently of that file."""
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    patterns = [
        os.path.join(local_appdata, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", "claude-code", "*", "claude.exe"),
        os.path.join(local_appdata, "Programs", "claude", "claude.exe"),
    ]
    candidates = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    if not candidates:
        return None
    candidates.sort(key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"[.\\/]", p)])
    return candidates[-1]


def _active_dev_session_id():
    try:
        with open(ACTIVE_DEV_SESSION_PATH, "r", encoding="utf-8") as handle:
            return (json.load(handle) or {}).get("session_id") or None
    except Exception:
        return None


def _load_mirror_chain_tip():
    try:
        with open(MIRROR_STATE_PATH, "r", encoding="utf-8") as handle:
            return (json.load(handle) or {}).get("current_id") or None
    except Exception:
        return None


def _save_mirror_chain_tip(session_id):
    try:
        with open(MIRROR_STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump({"current_id": session_id}, handle)
    except Exception:
        pass


def _reset_mirror_chain():
    try:
        os.remove(MIRROR_STATE_PATH)
    except Exception:
        pass


def _notify_mirror(text):
    if not JARVIS_PHONE_BACKEND_URL or not SHARED_SECRET:
        return
    # A dropped reply here is worse than for most other relays in this
    # project: there's no separate fallback channel showing it happened
    # (unlike, say, the visible PC status indicator), so a single failed
    # POST just looks like Jarvis silently ignored the message. Retried
    # once before giving up.
    for attempt in range(2):
        try:
            requests.post(
                f"{JARVIS_PHONE_BACKEND_URL}/api/mirror_said",
                json={"text": text},
                headers={"Authorization": f"Bearer {SHARED_SECRET}"},
                timeout=8,
            )
            return
        except Exception:
            if attempt == 0:
                time.sleep(1)


# A hang here isn't hypothetical -- confirmed live that a real usage
# limit hit made the underlying CLI call sit blocked indefinitely
# rather than failing fast, and with no bound on the wait, the phone
# never heard anything at all, and since the poll loop runs messages
# strictly in order, every message after it would have queued up
# uselessly behind that one stuck call forever.
MIRROR_TIMEOUT_SECONDS = 300


def _run_mirror_message(text):
    """
    Send one message into the mirror chain and relay the reply back.
    Runs synchronously in the poll loop's own thread on purpose --
    these have to happen strictly in order, since each one depends on
    the previous one's resulting session id.
    """
    claude_exe = _find_claude_cli()
    if not claude_exe:
        _notify_mirror("(Couldn't find Claude Code installed on this PC.)")
        return

    # First message ever (or first since a reset): base it on the
    # designated live dev session. Every message after that continues
    # from wherever the PREVIOUS mirror message's own fork left off --
    # NOT the original session again, which would drop everything said
    # in between. If neither exists, this starts a brand new session
    # with no history at all, covering "open you if you're not open".
    resume_id = _load_mirror_chain_tip() or _active_dev_session_id()

    args = [
        claude_exe, "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--add-dir", JARVIS_DIR,
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
    ]
    if resume_id:
        args += ["--resume", resume_id, "--fork-session"]

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=JARVIS_DIR,
        )
    except Exception as error:
        _notify_mirror(f"(Couldn't start a session: {error})")
        return

    message = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    try:
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        proc.stdin.close()
    except Exception as error:
        _notify_mirror(f"(Couldn't send that message: {error})")
        return

    result = {"reply": "", "session_id": None, "error": None}

    def reader():
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                event_type = event.get("type")
                if event_type == "stream_event":
                    inner = event.get("event") or {}
                    inner_type = inner.get("type")
                    if inner_type == "content_block_start":
                        block = inner.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            _notify_mirror(f"[working: {block.get('name', 'tool')}]")
                    elif inner_type == "content_block_delta":
                        delta = inner.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            result["reply"] += delta.get("text", "")
                elif event_type == "result":
                    result["session_id"] = event.get("session_id")
        except Exception as error:
            result["error"] = str(error)

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()
    reader_thread.join(timeout=MIRROR_TIMEOUT_SECONDS)

    def _stderr_tail():
        try:
            return (proc.stderr.read() or "").strip()[-300:]
        except Exception:
            return ""

    if reader_thread.is_alive():
        try:
            proc.kill()
        except Exception:
            pass
        detail = _stderr_tail()
        suffix = f" Details: {detail}" if detail else ""
        _notify_mirror(
            f"(That one got stuck and never finished after {MIRROR_TIMEOUT_SECONDS}s, sir -- "
            f"possibly a usage limit.{suffix} Try again shortly.)"
        )
        return

    if result["error"]:
        _notify_mirror(f"(Something went wrong reading the reply: {result['error']})")

    reply_buffer = result["reply"]
    new_session_id = result["session_id"]
    if reply_buffer.strip():
        _notify_mirror(reply_buffer.strip())
    if new_session_id:
        _save_mirror_chain_tip(new_session_id)
    else:
        detail = _stderr_tail()
        suffix = f" Details: {detail}" if detail else ""
        _notify_mirror(f"(No reply came back that time, sir -- try sending it again?{suffix})")


def _mirror_poll_loop():
    """Same reasoning as _poll_backend_loop -- checks in with the phone
    backend instead of waiting for an inbound call it could never receive."""
    if not JARVIS_PHONE_BACKEND_URL or not SHARED_SECRET:
        return
    while True:
        try:
            response = requests.post(
                f"{JARVIS_PHONE_BACKEND_URL}/api/mirror_poll",
                headers={"Authorization": f"Bearer {SHARED_SECRET}"},
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("reset"):
                    _reset_mirror_chain()
                for message in data.get("messages", []):
                    _run_mirror_message(message)
        except Exception:
            pass
        time.sleep(3)


def _jarvis_already_running():
    try:
        response = requests.get(JARVIS_STATUS_URL, timeout=2)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False


def _notify_phone(text):
    """
    Best-effort, fire-and-forget push straight to the phone backend's
    pc_events feed -- reusing the same endpoint Jarvis itself posts to
    (see _relay_speech_to_phone in Jarvis_FINAL_WORKING.py). Fired here,
    from the launcher, because Jarvis's OWN startup greeting can be
    10-30s away (model/voice init) -- confirmed live that waiting on it
    made the "it started" acknowledgment arrive late or get missed
    entirely. This fires the instant the process is actually spawned.
    """
    if not JARVIS_PHONE_BACKEND_URL or not SHARED_SECRET:
        return

    def worker():
        try:
            requests.post(
                f"{JARVIS_PHONE_BACKEND_URL}/api/pc_said",
                json={"text": text},
                headers={"Authorization": f"Bearer {SHARED_SECRET}"},
                timeout=5,
            )
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()


def _start_jarvis_if_needed():
    if _jarvis_already_running():
        return "already_running"
    try:
        # A new, visible console window -- deliberately the terminal
        # version, not the desktop HUD, per Danny's instruction (the
        # HUD needs its own separate remote-start handling later).
        # CREATE_NEW_CONSOLE already gives the .bat its own window, so
        # there's no need to go through cmd's "start" (which has a
        # sharp edge: an unquoted first argument like a title gets
        # misread as the command to run instead, silently failing).
        subprocess.Popen(
            [JARVIS_BAT],
            cwd=JARVIS_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
        _notify_phone("Starting Jarvis up now, sir.")
        return "starting"
    except Exception as error:
        print("Launcher: failed to start Jarvis:", error)
        return "error"


def _poll_backend_loop():
    """
    Same reasoning as Jarvis's own _phone_poll_loop: Render can't reach
    this PC's private Tailscale address, so rather than waiting for an
    inbound /start call that will never arrive once the phone backend
    is deployed off-PC, this checks in with it instead.
    """
    if not JARVIS_PHONE_BACKEND_URL or not SHARED_SECRET:
        return
    while True:
        try:
            response = requests.post(
                f"{JARVIS_PHONE_BACKEND_URL}/api/launcher_poll",
                headers={"Authorization": f"Bearer {SHARED_SECRET}"},
                timeout=10,
            )
            if response.status_code == 200 and response.json().get("start_requested"):
                _start_jarvis_if_needed()
        except Exception:
            pass
        time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep this quiet -- it just sits in the background

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        if not SHARED_SECRET:
            return True  # not configured yet -- fail open only in local dev
        token = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return token == SHARED_SECRET

    def do_GET(self):
        if self.path == "/status":
            self._send_json(200, {"launcher": "ok", "jarvis_running": _jarvis_already_running()})
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/start":
            self.send_error(404)
            return
        if not self._check_auth():
            self._send_json(401, {"error": "unauthorized"})
            return

        status = _start_jarvis_if_needed()
        code = 500 if status == "error" else 200
        self._send_json(code, {"status": status})


if __name__ == "__main__":
    threading.Thread(target=_poll_backend_loop, daemon=True).start()
    threading.Thread(target=_mirror_poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", LAUNCHER_PORT), Handler)
    print(f"Jarvis launcher listening on port {LAUNCHER_PORT}")
    server.serve_forever()
