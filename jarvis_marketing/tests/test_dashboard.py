"""The dashboard: API views match the records, the laptop server's login/CSRF/lockout, and the Jarvis window's routes."""

import http.cookiejar
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request

from jarvis_marketing import api, controls as C, remote
from jarvis_marketing.tests.test_agents import DANNY, make, prepared, to_mode


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def dept_for(d):
    return api.Department(directors={d.business["slug"]: d})


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.d, self.clock, _ = prepared()
        self.dept = dept_for(self.d)
        self.slug = self.d.business["slug"]

    def test_overview_matches_the_records(self):
        v = api.overview(self.dept, self.slug)
        s = self.d.store
        self.assertTrue(v["simulated"])
        self.assertEqual(len(v["approvals_pending"]), len(s.find("approvals", status="pending")))
        self.assertEqual({c["id"]: c["status"] for c in v["candidates"]}, {c["id"]: c["status"] for c in s.find("candidates")})
        self.assertEqual(len(v["briefs"]), len(s.find("briefs")))
        self.assertEqual(v["actual"]["orders"], len(s.find("orders")))
        self.assertEqual(v["actual"]["spend"], 0.0)
        self.assertEqual(v["controls"]["mode"], C.REVIEW)
        self.assertTrue(all(v["controls"]["hard_off"].values()))
        for a in v["approvals_pending"]:
            self.assertEqual(a["post"]["approval_id"], a["id"])
        json.dumps(v, default=str)                         # the whole view serialises

    def test_overview_after_results_keeps_actual_and_estimate_apart(self):
        to_mode(self.d, C.TEST)
        for a in self.d.approvals.pending(self.d.bid):
            self.d.decide_post(DANNY, a["id"], "approve")
        self.d.publish_approved()
        for _ in range(18):
            self.clock.advance(86400)
            self.d.run_cycle(["collect", "judge", "learn"])
        v = api.overview(self.dept, self.slug)
        orders = self.d.store.find("orders")
        self.assertEqual(v["actual"]["revenue"], round(sum(o["total"] for o in orders), 2))
        self.assertEqual(v["actual"]["attributed_orders"], sum(1 for o in orders if o["post_id"]))
        self.assertEqual(v["estimated"]["label"], "estimate")
        self.assertEqual(v["reconcile_problems"], [])
        for e in v["experiments"]:
            if e["verdict"] in ("inconclusive", "insufficient_data"):
                self.assertIsNone(e["result"]["winner"])
        self.assertEqual(sum(v["verdicts"].values()), len([e for e in v["experiments"] if e["verdict"]]))

    def test_only_a_human_can_act_but_anyone_authorised_can_stop(self):
        a = self.d.approvals.pending(self.d.bid)[0]
        with self.assertRaises(PermissionError):
            api.handle_post(self.dept, f"/api/marketing/{self.slug}/approvals/{a['id']}/approve", {}, principal=None)
        with self.assertRaises(PermissionError):
            api.handle_post(self.dept, f"/api/marketing/{self.slug}/mode", {"mode": "TEST"}, principal=None)
        with self.assertRaises(PermissionError):
            api.handle_post(self.dept, f"/api/marketing/{self.slug}/mode", {"mode": "SCALE"}, principal=C.HumanPrincipal("Danny", "test"))
        api.handle_post(self.dept, "/api/marketing/stop", {"reason": "brain"}, principal=None)
        self.assertTrue(self.d.controls.stopped()["on"])
        with self.assertRaises(PermissionError):
            api.handle_post(self.dept, "/api/marketing/resume", {}, principal=None)
        api.handle_post(self.dept, "/api/marketing/resume", {}, principal=DANNY)
        self.assertFalse(self.d.controls.stopped()["on"])

    def test_approve_in_review_mode_waits_for_test_mode(self):
        a = self.d.approvals.pending(self.d.bid)[0]
        out = api.handle_post(self.dept, f"/api/marketing/{self.slug}/approvals/{a['id']}/approve", {}, principal=DANNY)
        self.assertIn("TEST", out["text"])
        self.assertEqual(self.d.adapters.publisher.calls, [])

    def test_stopping_an_experiment_rejects_its_waiting_posts(self):
        e = self.d.store.find("experiments")[0]
        api.handle_post(self.dept, f"/api/marketing/{self.slug}/experiments/{e['id']}/stop", {}, principal=DANNY)
        self.assertEqual(self.d.store.get("experiments", e["id"])["status"], "stopped")
        self.assertTrue(all(p["status"] == "rejected" for p in self.d.store.find("posts", experiment_id=e["id"])))

    def test_media_is_only_served_from_the_assets_folder(self):
        c = self.d.store.find("creatives")[0]
        with self.assertRaises(ValueError):          # a SIMULATED render has no video
            api.media_path(self.dept, self.slug, c["id"])
        self.d.store.update("creatives", c["id"], files_json={"video": os.path.abspath(__file__)})
        with self.assertRaises(ValueError):
            api.media_path(self.dept, self.slug, c["id"])


class RemoteServerTests(unittest.TestCase):
    PASSWORD = "correct horse battery staple"

    def setUp(self):
        self.d, _ = make()
        self.dept = dept_for(self.d)
        login = dict(remote.hash_password(self.PASSWORD, rounds=1000), bind="127.0.0.1")
        self.port = free_port()
        self.server, self.rs = remote.start(self.dept, login=login, bind="127.0.0.1", port=self.port)
        self.base = f"http://127.0.0.1:{self.port}"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def req(self, path, data=None, headers=None, form=False, opener=None):
        body = None
        h = dict(headers or {})
        if data is not None:
            body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        r = urllib.request.Request(self.base + path, data=body, headers=h, method="POST" if data is not None else "GET")
        try:
            with (opener or self.opener).open(r, timeout=10) as resp:
                return resp.status, resp.read().decode(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    def login(self, password=None):
        return self.req("/login", {"password": password or self.PASSWORD}, form=True)

    def csrf(self):
        return next(iter(self.rs.sessions.values()))["csrf"]

    def test_everything_needs_a_login(self):
        self.assertEqual(self.req("/api/marketing")[0], 401)
        status, body, _ = self.req("/")
        self.assertIn("Password", body)                       # redirected to the login page

    def test_login_then_read_and_act_with_csrf(self):
        status, body, _ = self.login()
        self.assertEqual(status, 200)
        self.assertIn("Marketing", body)
        self.assertNotIn("__JM_CSRF__", body)
        self.assertNotIn("__JARVIS_TOKEN__", body)
        status, body, _ = self.req("/api/marketing")
        self.assertEqual(status, 200)
        slug = json.loads(body)["businesses"][0]["slug"]
        self.assertEqual(self.req(f"/api/marketing/{slug}/mode", {"mode": "REVIEW"})[0], 403)          # no CSRF
        self.assertEqual(self.req(f"/api/marketing/{slug}/mode", {"mode": "REVIEW"}, {"X-CSRF": "wrong"})[0], 403)
        self.assertEqual(self.req(f"/api/marketing/{slug}/mode", {"mode": "REVIEW"},
                                  {"X-CSRF": self.csrf(), "Origin": "https://evil.example"})[0], 403)
        status, body, _ = self.req(f"/api/marketing/{slug}/mode", {"mode": "REVIEW"}, {"X-CSRF": self.csrf()})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.d.controls.mode(self.d.bid), C.REVIEW)
        status, body, _ = self.req(f"/api/marketing/{slug}/mode", {"mode": "SCALE"}, {"X-CSRF": self.csrf()})
        self.assertEqual(status, 403)
        audit = [a for a in self.d.store.find("audit") if a["action"] == "set_mode" and a["outcome"] == "ok"]
        self.assertIn("laptop", audit[-1]["actor"])

    def test_wrong_passwords_lock_out_and_are_audited(self):
        for _ in range(remote.LOCKOUT_FAILURES):
            self.assertEqual(self.login("nope")[0], 401)
        self.assertEqual(self.login()[0], 429)                 # even the right password, during the lockout
        fails = [a for a in self.d.store.find("audit") if a["action"] == "login" and a["outcome"] == "failed"]
        self.assertEqual(len(fails), remote.LOCKOUT_FAILURES)
        self.assertNotIn("nope", json.dumps(fails))

    def test_session_cookie_is_httponly_and_strict(self):
        r = urllib.request.Request(self.base + "/login", data=urllib.parse.urlencode({"password": self.PASSWORD}).encode(), method="POST")

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            urllib.request.build_opener(NoRedirect).open(r, timeout=10)
        except urllib.error.HTTPError as e:
            cookie = e.headers.get("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_no_file_access_and_security_headers(self):
        self.login()
        for path in ("/../../etc/passwd", "/api/marketing/collective-finds/creatives/1/media", "/jarvis_marketing/config.py"):
            status, body, _ = self.req(path)
            self.assertIn(status, (400, 404), path)
            self.assertNotIn("root:", body)
        _, _, headers = self.req("/api/marketing")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_password_is_stored_only_as_a_hash(self):
        path = os.path.join(tempfile.mkdtemp(), "login.json")
        remote.set_password("a long enough password", path=path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("a long enough password", text)
        self.assertTrue(remote.check_password("a long enough password", remote.load_login(path)))
        self.assertFalse(remote.check_password("wrong", remote.load_login(path)))
        with self.assertRaises(ValueError):
            remote.set_password("short", path=path)

    def test_logout_ends_the_session(self):
        self.login()
        self.assertEqual(self.req("/logout", {}, {"X-CSRF": self.csrf()})[0], 200)
        self.assertEqual(self.req("/api/marketing")[0], 401)


class WindowServerTests(unittest.TestCase):
    """The Marketing tab inside the Jarvis window (jarvis_v2/server.py)."""

    def setUp(self):
        from jarvis_v2 import config as V2, router as R, runs, server
        from jarvis_v2.tests.test_v2 import FakeBrain
        self.server_mod = server
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (V2.DATA_DIR, V2.BRAIN_STATE_FILE, V2.ENGINE_URL, server.TOKEN_FILE, server.ENGINE_FLAG_FILE)
        V2.DATA_DIR = self.tmp.name
        V2.BRAIN_STATE_FILE = os.path.join(self.tmp.name, "brain_state.json")
        V2.ENGINE_URL = f"http://127.0.0.1:{free_port()}"
        server.TOKEN_FILE = os.path.join(self.tmp.name, "server_token")
        server.ENGINE_FLAG_FILE = os.path.join(self.tmp.name, "use_v2_brain")
        router = R.Router([FakeBrain("claude", []), FakeBrain("openai", []), FakeBrain("local", [])], state_path=V2.BRAIN_STATE_FILE)
        executor = runs.Executor(store=runs.RunStore(os.path.join(self.tmp.name, "runs.db")), popen=None)
        self.port = free_port()
        self.srv, self.state = server.start(router, port=self.port, executor=executor)
        self.d, _, _ = prepared()
        self.state.marketing = dept_for(self.d)
        self.slug = self.d.business["slug"]

    def tearDown(self):
        from jarvis_v2 import config as V2
        self.srv.shutdown()
        self.srv.server_close()
        (V2.DATA_DIR, V2.BRAIN_STATE_FILE, V2.ENGINE_URL, self.server_mod.TOKEN_FILE, self.server_mod.ENGINE_FLAG_FILE) = self.old
        self.tmp.cleanup()

    def call(self, path, body=None, ui=False, token=True):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Jarvis-Token"] = self.state.token
        if ui:
            h["X-Jarvis-UI"] = self.state.ui_token
            h["Origin"] = f"http://127.0.0.1:{self.port}"          # a button press comes from the window page itself
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                   headers=h, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if "json" in resp.headers.get("Content-Type", "") else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_marketing_page_and_api_in_the_window(self):
        status, page = self.call("/marketing", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.state.token, page)
        self.assertNotIn("__JM_MODE__", page)
        self.assertEqual(self.call("/api/marketing", token=False)[0], 401)
        status, out = self.call(f"/api/marketing/{self.slug}")
        self.assertEqual(status, 200)
        self.assertEqual(len(out["approvals_pending"]), len(self.d.approvals.pending(self.d.bid)))

    def test_brain_token_can_read_and_stop_but_not_approve(self):
        a = self.d.approvals.pending(self.d.bid)[0]
        status, _ = self.call(f"/api/marketing/{self.slug}/approvals/{a['id']}/approve", {})
        self.assertEqual(status, 403)
        status, _ = self.call(f"/api/marketing/{self.slug}/mode", {"mode": "TEST"})
        self.assertEqual(status, 403)
        status, _ = self.call("/api/marketing/stop", {"reason": "Danny said stop"})
        self.assertEqual(status, 200)
        self.assertTrue(self.d.controls.stopped()["on"])
        self.assertEqual(self.call("/api/marketing/resume", {})[0], 403)
        self.assertEqual(self.call("/api/marketing/resume", {}, ui=True)[0], 200)

    def test_a_press_without_the_windows_origin_is_refused(self):
        h = {"Content-Type": "application/json", "X-Jarvis-Token": self.state.token, "X-Jarvis-UI": self.state.ui_token}
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/marketing/{self.slug}/mode", data=b'{"mode": "TEST"}',
                                   headers=h, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(r, timeout=10)
        self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.d.controls.mode(self.d.bid), C.REVIEW)

    def test_dannys_window_buttons_work(self):
        status, out = self.call(f"/api/marketing/{self.slug}/mode", {"mode": "TEST"}, ui=True)
        self.assertEqual(status, 200, out)
        a = self.d.approvals.pending(self.d.bid)[0]
        status, out = self.call(f"/api/marketing/{self.slug}/approvals/{a['id']}/approve", {}, ui=True)
        self.assertEqual(status, 200, out)
        import time
        for _ in range(50):
            if self.d.adapters.publisher.calls:
                break
            time.sleep(0.1)
        self.assertEqual(len(self.d.adapters.publisher.calls), 1)

    def test_the_brain_tools_exist_and_are_safe_names(self):
        from jarvis_v2 import tools
        self.assertIn("marketing_status", tools.TOOLS)
        self.assertIn("marketing_stop", tools.TOOLS)
        for name in ("marketing_status", "marketing_stop"):
            self.assertIsNone(tools.FORBIDDEN_NAMES.search(name))
        self.assertFalse([n for n in tools.TOOLS if "marketing" in n and n not in ("marketing_status", "marketing_stop")])


if __name__ == "__main__":
    unittest.main()


class CliTests(unittest.TestCase):
    def setUp(self):
        from jarvis_marketing import config
        self.config, self.old = config, config.DATA_ROOT
        config.DATA_ROOT = tempfile.mkdtemp()

    def tearDown(self):
        self.config.DATA_ROOT = self.old

    def test_terminal_commands_and_plain_refusals(self):
        from jarvis_marketing.__main__ import run
        self.assertEqual(run(["status"]), 0)
        self.assertEqual(run(["mode", "TEST"]), 2)            # one step at a time
        self.assertEqual(run(["mode", "REVIEW"]), 0)
        self.assertEqual(run(["stop", "test"]), 0)
        self.assertEqual(run(["cycle"]), 2)                    # refused while stopped
        self.assertEqual(run(["resume"]), 0)
        self.assertEqual(run(["serve"]), 1)                    # no laptop password yet: it says so and doesn't start


class NeedsYouTests(unittest.TestCase):
    def test_pending_posts_are_listed_for_the_needs_you_panel(self):
        d, _, _ = prepared()
        out = api.handle_get(dept_for(d), "/api/marketing", {})
        b = out["businesses"][0]
        self.assertEqual(len(b["pending"]), len(d.approvals.pending(d.bid)))
        self.assertEqual(b["profile"], "mock")
