"""
Tests for the v2 server: security, the brain API, panels, and working without the voice engine.

    py -3 -m unittest jarvis_v2.tests.test_server -v
"""

import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from jarvis_v2 import config, router as R, server
from jarvis_v2.tests.test_v2 import FakeBrain


def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (config.DATA_DIR, config.BRAIN_STATE_FILE, config.USAGE_FILE, config.ENGINE_URL,
                    server.TOKEN_FILE, server.ENGINE_FLAG_FILE, dict(server.PANEL_SOURCES))
        config.DATA_DIR = self.tmp.name
        config.BRAIN_STATE_FILE = os.path.join(self.tmp.name, "brain_state.json")
        config.USAGE_FILE = os.path.join(self.tmp.name, "usage.json")
        config.ENGINE_URL = f"http://127.0.0.1:{_free_port()}"     # nothing listens: the engine is "off"
        server.TOKEN_FILE = os.path.join(self.tmp.name, "server_token")
        server.ENGINE_FLAG_FILE = os.path.join(self.tmp.name, "use_v2_brain")
        server.PANEL_SOURCES["store"] = (lambda: "store ok", 300)
        server.PANEL_SOURCES["money"] = (lambda: "money ok", 0)
        self.router = R.Router([FakeBrain("claude", ["hello sir", "second"]), FakeBrain("openai", []), FakeBrain("local", [])],
                               state_path=config.BRAIN_STATE_FILE)
        self.port = _free_port()
        from jarvis_v2 import runs
        self.executor = runs.Executor(store=runs.RunStore(os.path.join(self.tmp.name, "runs.db")), popen=None)
        self.srv, self.state = server.start(self.router, port=self.port, executor=self.executor)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        (config.DATA_DIR, config.BRAIN_STATE_FILE, config.USAGE_FILE, config.ENGINE_URL,
         server.TOKEN_FILE, server.ENGINE_FLAG_FILE, panels) = self.old
        server.PANEL_SOURCES.clear()
        server.PANEL_SOURCES.update(panels)
        self.tmp.cleanup()

    def call(self, path, body=None, token=True, host=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET")
        if token:
            req.add_header("X-Jarvis-Token", self.state.token if token is True else token)
        if host:
            req.add_header("Host", host)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_api_needs_the_token(self):
        self.assertEqual(self.call("/api/state", token=False)[0], 401)
        self.assertEqual(self.call("/api/state", token="wrong")[0], 401)
        self.assertEqual(self.call("/api/ask", {"text": "hi"}, token=False)[0], 401)
        self.assertEqual(self.call("/api/state")[0], 200)

    def test_foreign_host_header_is_refused(self):
        self.assertEqual(self.call("/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.call("/", token=False, host="evil.example")[0], 403)

    def test_page_carries_the_token_and_refuses_framing(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with urllib.request.urlopen(req, timeout=10) as r:
            page = r.read().decode()
            self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
        self.assertIn(self.state.token, page)
        self.assertIn("orbCanvas", page)

    def test_token_file_is_written_for_the_engine(self):
        with open(server.TOKEN_FILE, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.state.token)

    def test_ask_uses_the_router(self):
        status, r = self.call("/api/ask", {"text": "hi"})
        self.assertEqual((status, r["text"], r["brain"]), (200, "hello sir", "claude"))
        self.assertFalse(self.state.thinking)

    def test_typed_command_falls_back_to_the_brain_when_the_engine_is_off(self):
        status, r = self.call("/api/command", {"text": "hi"})
        self.assertEqual(r["text"], "hello sir")
        self.assertIn("offline", r["via"])

    def test_state_reports_engine_offline(self):
        status, s = self.call("/api/state")
        self.assertFalse(s["engine"]["online"])
        self.assertIn("status_text", s)

    def test_brain_choice(self):
        status, r = self.call("/api/brain", {"use": "local"})
        self.assertIn("local", r["text"])
        self.assertEqual(R.load_state(config.BRAIN_STATE_FILE)["forced"], "local")
        self.assertEqual(self.call("/api/brain", {"use": "gemini"})[0], 400)
        self.call("/api/brain", {"use": "auto"})
        self.assertIsNone(R.load_state(config.BRAIN_STATE_FILE)["forced"])

    def test_panels_load_in_the_background_and_money_only_on_request(self):
        self.call("/api/panel/store")
        for _ in range(50):
            p = self.call("/api/panel/store")[1]
            if p["text"]:
                break
            time.sleep(0.05)
        self.assertEqual(p["text"], "store ok")
        self.assertIsNone(self.call("/api/panel/money")[1]["text"])      # never started by itself
        self.call("/api/panel/money/refresh", {})
        for _ in range(50):
            p = self.call("/api/panel/money")[1]
            if p["text"]:
                break
            time.sleep(0.05)
        self.assertEqual(p["text"], "money ok")
        self.assertIn("error", self.call("/api/panel/secrets")[1])

    def test_engine_flag_is_switched_on_and_off(self):
        server.set_engine_brain(True)
        with open(server.ENGINE_FLAG_FILE, encoding="utf-8") as f:
            self.assertEqual(f.read(), "1")
        server.set_engine_brain(False)
        with open(server.ENGINE_FLAG_FILE, encoding="utf-8") as f:
            self.assertEqual(f.read(), "0")


if __name__ == "__main__":
    unittest.main()
