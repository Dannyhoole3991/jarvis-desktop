"""
Tiny always-on listener, separate from Jarvis itself, whose only job is
to start the real Jarvis process on request. It has to be a separate
process: if Jarvis is the thing that's not running, nothing inside
Jarvis can answer a "start me" request.

Meant to be registered as a Windows Task Scheduler task that runs at
logon (pythonw, no console window) so it's always reachable whenever
Danny is logged into the PC, independent of whether Jarvis itself is
open or closed.

For now this only launches the terminal version (START_JARVIS_FINAL_WORKING.bat,
a visible console window) -- the desktop HUD version will be added later.
"""
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

LAUNCHER_PORT = int(os.environ.get("JARVIS_LAUNCHER_PORT", "8766"))
SHARED_SECRET = os.environ.get("JARVIS_PHONE_SECRET", "")
# Only needed for the poll loop below -- reaching the phone's cloud
# backend from off the tailnet, since it can't reach back in here.
JARVIS_PHONE_BACKEND_URL = os.environ.get("JARVIS_PHONE_BACKEND_URL", "").rstrip("/")
JARVIS_DIR = os.path.dirname(os.path.abspath(__file__))
JARVIS_BAT = os.path.join(JARVIS_DIR, "START_JARVIS_FINAL_WORKING.bat")
JARVIS_STATUS_URL = "http://127.0.0.1:8765/status"


def _jarvis_already_running():
    try:
        response = requests.get(JARVIS_STATUS_URL, timeout=2)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False


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
    server = ThreadingHTTPServer(("0.0.0.0", LAUNCHER_PORT), Handler)
    print(f"Jarvis launcher listening on port {LAUNCHER_PORT}")
    server.serve_forever()
