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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

LAUNCHER_PORT = int(os.environ.get("JARVIS_LAUNCHER_PORT", "8766"))
SHARED_SECRET = os.environ.get("JARVIS_PHONE_SECRET", "")
JARVIS_DIR = os.path.dirname(os.path.abspath(__file__))
JARVIS_BAT = os.path.join(JARVIS_DIR, "START_JARVIS_FINAL_WORKING.bat")
JARVIS_STATUS_URL = "http://localhost:8765/status"


def _jarvis_already_running():
    try:
        response = requests.get(JARVIS_STATUS_URL, timeout=2)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False


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

        if _jarvis_already_running():
            self._send_json(200, {"status": "already_running"})
            return

        try:
            # A new, visible console window -- deliberately the terminal
            # version, not the desktop HUD, per Danny's instruction (the
            # HUD needs its own separate remote-start handling later).
            # CREATE_NEW_CONSOLE already gives the .bat its own window,
            # so there's no need to go through cmd's "start" (which has
            # a sharp edge: an unquoted first argument like a title gets
            # misread as the command to run instead, silently failing).
            subprocess.Popen(
                [JARVIS_BAT],
                cwd=JARVIS_DIR,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            self._send_json(200, {"status": "starting"})
        except Exception as error:
            self._send_json(500, {"error": str(error)})


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", LAUNCHER_PORT), Handler)
    print(f"Jarvis launcher listening on port {LAUNCHER_PORT}")
    server.serve_forever()
