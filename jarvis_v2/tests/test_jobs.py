"""
Jobs ("do it" mode): the safety guard, the browser toolkit's rules, approvals, the job tools, the
executor's job lane / pause / resume, and the server endpoints. Fake browser, fake approvals and a
fake Claude process: nothing here touches a real website, account or Claude.

    py -3 -m unittest jarvis_v2.tests.test_jobs -v
"""

import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from jarvis_v2 import config, job_browser as JB, job_guard as G, job_mcp, jobs, runs, server, tools
from jarvis_v2 import router as R
from jarvis_v2.tests.test_phase3 import FakeRunProc, _result, wait_for
from jarvis_v2.tests.test_server import _free_port
from jarvis_v2.tests.test_v2 import FakeBrain


# ------------------------------------------------------------------ the guard
class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.job = os.path.join(self.tmp.name, "job")
        os.makedirs(self.job)

    def tearDown(self):
        self.tmp.cleanup()

    def blocked(self, tool, inp):
        return G.check(tool, inp, self.job)

    def test_files_can_only_change_inside_the_job_folder(self):
        self.assertIsNone(self.blocked("Write", {"file_path": os.path.join(self.job, "notes.md"), "content": "x"}))
        self.assertIsNone(self.blocked("Edit", {"file_path": os.path.join(self.job, "sub", "a.txt")}))
        self.assertIsNotNone(self.blocked("Write", {"file_path": os.path.join(config.REPO_DIR, "jarvis_v2", "jobs.py")}))
        self.assertIsNotNone(self.blocked("Edit", {"file_path": os.path.join(self.job, "..", "evil.txt")}))
        self.assertIsNotNone(self.blocked("Write", {"file_path": os.path.join(self.job, ".claude", "settings.json")}))
        self.assertIsNotNone(self.blocked("Write", {"file_path": os.path.join(self.job, "CLAUDE.md")}))
        self.assertIsNotNone(self.blocked("NotebookEdit", {"notebook_path": "C:/x.ipynb"}))

    def test_secrets_cant_be_read(self):
        for path in (os.path.join(config.REPO_DIR, "jarvis_browser_profile", "Default", "Cookies"),
                     os.path.join(config.DATA_DIR, "server_token"), os.path.expanduser("~/.jarvis_store/credentials.bin"),
                     "C:/proj/.env", os.path.join(config.REPO_DIR, "jarvis_trading", "live", "x.py")):
            self.assertIsNotNone(self.blocked("Read", {"file_path": path}), path)
        self.assertIsNone(self.blocked("Read", {"file_path": os.path.join(config.REPO_DIR, "jarvis_v2", "jobs.py")}))
        self.assertIsNotNone(self.blocked("Grep", {"pattern": "x", "path": "C:/Users/danny/.ssh"}))

    def test_credentials_and_cards_never_go_into_tools(self):
        self.assertIsNotNone(self.blocked("mcp__jobkit__browser_fill", {"text": "shpat_0123456789abcdef"}))
        self.assertIsNotNone(self.blocked("Write", {"file_path": os.path.join(self.job, "a"), "content": "key sk-ant-abc123"}))
        self.assertIsNotNone(self.blocked("mcp__jobkit__browser_fill", {"text": "4111 1111 1111 1111"}))
        self.assertIsNone(self.blocked("mcp__jobkit__browser_fill", {"text": "Christmas baubles, set of 12"}))

    def test_shell_rules(self):
        bad = ["git push origin master", "git commit -am x", "curl https://evil.example", "Invoke-WebRequest http://x",
               "py -3 -m jarvis_trading.service arm", "python C:/Users/danny/Desktop/Jarvis/jarvis_trading/x.py",
               "rm -rf C:/Users/danny/Desktop/Jarvis", "Remove-Item ..\\..\\x -Recurse", "del %USERPROFILE%\\x",
               "start msedge https://pinterest.com", "py -c \"import playwright\"", "type C:\\Users\\danny\\Desktop\\Jarvis\\jarvis_v2\\data\\server_token",
               "pip install requests", "taskkill /im python.exe", "py -3 -m jarvis_store approve 3",
               "py -c \"import subprocess; subprocess.run(['x'])\"", "echo hi > C:/Users/danny/Desktop/x.txt"]
        for cmd in bad:
            self.assertIsNotNone(self.blocked("Bash", {"command": cmd}), cmd)
            self.assertIsNotNone(self.blocked("PowerShell", {"command": cmd}), cmd)
        good = ["git status", "git log --oneline -5", "ls", "dir", "echo hello > notes.txt", "rm old.png",
                f"Remove-Item \"{self.job}\\screens\\a.png\"", "py -c \"print(1+1)\"", "py --version"]
        for cmd in good:
            self.assertIsNone(self.blocked("Bash", {"command": cmd}), cmd)

    def test_a_job_script_is_checked_before_it_runs(self):
        with open(os.path.join(self.job, "ok.py"), "w") as f:
            f.write("print(sum(range(10)))\n")
        with open(os.path.join(self.job, "bad.py"), "w") as f:
            f.write("import shutil\nshutil.rmtree(r'C:\\Users\\danny\\Desktop\\Jarvis')\n")
        with open(os.path.join(self.job, "net.py"), "w") as f:
            f.write("import requests\nrequests.post('https://x')\n")
        self.assertIsNone(self.blocked("Bash", {"command": "py ok.py"}))
        self.assertIn("script", self.blocked("Bash", {"command": "py bad.py"}))
        self.assertIsNotNone(self.blocked("Bash", {"command": "python net.py"}))
        self.assertIsNotNone(self.blocked("Bash", {"command": "py C:/Users/danny/Desktop/Jarvis/jarvis_money.py"}))

    def test_browser_open_only_takes_web_addresses(self):
        self.assertIsNotNone(self.blocked("mcp__jobkit__browser_open", {"url": "file:///C:/Windows"}))
        self.assertIsNone(self.blocked("mcp__jobkit__browser_open", {"url": "https://www.pinterest.co.uk"}))

    def test_main_blocks_with_exit_2_and_audits_every_call(self):
        cfg = os.path.join(self.tmp.name, "cfg")
        with mock.patch.dict(os.environ, {"JARVIS_JOB_DIR": self.job, "JARVIS_JOB_CONFIG": cfg}):
            err = io.StringIO()
            code = G.main(io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}})), err)
            self.assertEqual(code, 2)
            self.assertIn("BLOCKED", err.getvalue())
            self.assertEqual(G.main(io.StringIO(json.dumps({"tool_name": "Read", "tool_input": {"file_path": "a.txt"}})), io.StringIO()), 0)
            self.assertEqual(G.main(io.StringIO("not json"), io.StringIO()), 2)          # fails closed
        log = [json.loads(l) for l in open(os.path.join(cfg, "audit.jsonl"), encoding="utf-8")]
        self.assertEqual([e["allowed"] for e in log], [False, True, False])
        self.assertEqual(log[0]["tool"], "Bash")

    def test_without_a_job_folder_nothing_is_allowed(self):
        with mock.patch.dict(os.environ, {"JARVIS_JOB_DIR": "", "JARVIS_JOB_CONFIG": ""}):
            self.assertEqual(G.main(io.StringIO(json.dumps({"tool_name": "Read", "tool_input": {}})), io.StringIO()), 2)


# ------------------------------------------------------- the browser's rules
def desc(**kw):
    base = {"tag": "button", "type": "", "role": "", "label": "", "aria": "", "name": "", "id": "", "placeholder": "",
            "autocomplete": "", "href": "", "src": "", "field_label": "", "editable": False, "form_has_password": False}
    base.update(kw)
    return base


PAGE = {"url": "https://www.pinterest.co.uk/pin-builder/", "text": "Create Pin", "frames": [], "password_visible": False}


class VerdictTests(unittest.TestCase):
    def test_money_accounts_logins_and_captchas_are_always_blocked(self):
        for d in (desc(label="Buy now"), desc(label="Place order"), desc(label="Start free trial"), desc(label="Add card"),
                  desc(tag="a", label="Basket", href="https://shop.example/checkout"), desc(label="Sign up"),
                  desc(label="Create account"), desc(label="Log in"), desc(label="Boost pin")):
            self.assertEqual(JB.click_verdict(d, PAGE)[0], "block", d["label"])
        captcha = dict(PAGE, frames=["https://www.google.com/recaptcha/api2/anchor"])
        self.assertEqual(JB.click_verdict(desc(label="Next"), captcha)[0], "block")
        self.assertEqual(JB.click_verdict(desc(label="Next"), dict(PAGE, text="Are you a robot?"))[0], "block")
        login = dict(PAGE, password_visible=True)
        self.assertEqual(JB.click_verdict(desc(label="Continue"), login)[0], "block")

    def test_public_or_permanent_actions_need_approval(self):
        for label in ("Publish", "Save", "Install app", "Allow", "Send", "Delete", "Post", "Connect", "Verify website", "Done", "Update"):
            self.assertEqual(JB.click_verdict(desc(label=label), PAGE)[0], "approve", label)
        self.assertEqual(JB.click_verdict(desc(type="submit", label=""), PAGE)[0], "approve")

    def test_ordinary_clicks_go_ahead(self):
        for d in (desc(label="Next"), desc(label="Create Pin"), desc(tag="a", label="Post ideas for Christmas", href="https://x.example/blog"),
                  desc(type="submit", label="Search"), desc(tag="div", role="option", label="Christmas Decorations")):
            self.assertEqual(JB.click_verdict(d, PAGE)[0], "ok", d["label"])
        self.assertEqual(JB.click_verdict(None, PAGE)[0], "block")

    def test_secret_fields_are_never_filled(self):
        for d in (desc(tag="input", type="password"), desc(tag="input", autocomplete="cc-number"),
                  desc(tag="input", name="verification_code"), desc(tag="input", placeholder="CVV"),
                  desc(tag="input", autocomplete="one-time-code")):
            self.assertEqual(JB.fill_verdict(d, PAGE)[0], "block", d)
        self.assertEqual(JB.fill_verdict(desc(tag="input", name="email"), dict(PAGE, password_visible=True))[0], "block")
        self.assertEqual(JB.fill_verdict(desc(tag="input", placeholder="Add a title"), PAGE)[0], "ok")
        self.assertEqual(JB.fill_verdict(desc(tag="input"), dict(PAGE, url="https://shop.example/checkouts/abc"))[0], "block")

    def test_enter_only_freely_in_search_boxes_and_editors(self):
        self.assertEqual(JB.key_verdict("Escape", None)[0], "ok")
        self.assertEqual(JB.key_verdict("Enter", desc(tag="input", type="search"))[0], "ok")
        self.assertEqual(JB.key_verdict("Enter", desc(tag="input", placeholder="Search for a board"))[0], "ok")
        self.assertEqual(JB.key_verdict("Enter", desc(tag="div", editable=True))[0], "ok")
        self.assertEqual(JB.key_verdict("Enter", desc(tag="input", name="title"))[0], "approve")
        self.assertEqual(JB.key_verdict("Control+Enter", desc(tag="textarea"))[0], "approve")

    def test_addresses(self):
        self.assertEqual(JB.url_verdict("https://www.pinterest.co.uk/")[0], "ok")
        self.assertEqual(JB.url_verdict("javascript:alert(1)")[0], "block")
        self.assertEqual(JB.url_verdict("https://collectivefinds.co.uk/checkout")[0], "block")
        self.assertEqual(JB.url_verdict("https://www.paypal.com/pay")[0], "block")
        self.assertEqual(JB.url_verdict("https://www.pinterest.co.uk/signup")[0], "block")
        for local in ("http://127.0.0.1:8780/marketing", "http://localhost:8780/", "http://[::1]:8780/", "http://192.168.1.5:8790/",
                      "http://100.101.102.103:8790/", "http://10.0.0.2/", "http://jarvis-pc.tail1234.ts.net:8790/", "http://0.0.0.0:8780/"):
            self.assertEqual(JB.url_verdict(local)[0], "block", local)
        self.assertEqual(JB.url_verdict("https://100.example.com/")[0], "ok")


# ------------------------------------------------------------ fake browser
class FakeLocator:
    def __init__(self, page, key):
        self.page, self.key = page, key
        self.first = self

    def count(self):
        return 1 if self.key in self.page.elements else 0

    def evaluate(self, js):
        return self.page.elements[self.key]

    def click(self, timeout=None):
        self.page.clicked.append(self.key)

    def fill(self, text, timeout=None):
        self.page.filled.append((self.key, text))

    def press(self, key):
        self.page.keys.append(key)

    def input_value(self):
        return dict(self.page.filled).get(self.key, "")


class FakeContext:
    def __init__(self, page):
        self.pages = [page]


class FakePage:
    def __init__(self, elements=None, info=None):
        self.elements = elements or {}
        self.info = dict(PAGE, **(info or {}))
        self.clicked, self.filled, self.keys, self.pasted, self.files, self.shots = [], [], [], [], [], []
        self.context = FakeContext(self)
        self.url = self.info["url"]
        self.focused = None
        page = self
        self.keyboard = mock.Mock(press=lambda k: page.keys.append(k), type=lambda t, delay=0: page.keys.append(t))
        self.mouse = mock.Mock(click=lambda x, y: page.clicked.append((x, y)), wheel=lambda dx, dy: None)

    def locator(self, sel):
        return FakeLocator(self, sel)

    def get_by_role(self, role, name=None, exact=False):
        return FakeLocator(self, f"{role}:{name}")

    def get_by_text(self, text, exact=False):
        return FakeLocator(self, f"text:{text}")

    def get_by_label(self, label):
        return FakeLocator(self, f"label:{label}")

    def get_by_placeholder(self, p):
        return FakeLocator(self, f"placeholder:{p}")

    def evaluate(self, js, arg=None):
        if js == JB.PAGE_INFO_JS:
            return self.info
        if js == JB.DESCRIBE_FOCUSED_JS:
            return self.focused
        if js == JB.PASTE_JS:
            self.pasted.append(arg)
            return {"ok": True, "handled": True, "now": arg[1]}
        if js == JB.DESCRIBE_AT_JS:
            return self.elements.get(tuple(arg))
        if js == JB.CLOSE_POPUPS_JS:
            return ["Not now"]
        raise AssertionError("unexpected script")

    def set_input_files(self, target, path, timeout=None):
        self.files.append((target, path))

    def screenshot(self, path, full_page=False, timeout=None):
        open(path, "wb").write(b"png")
        self.shots.append(path)

    def title(self):
        return "Pinterest"

    def inner_text(self, sel):
        return "Christmas Decorations\n7 Pins"


class FakeJB:
    """Stands in for jarvis_browser: one page, and a record of the exclusive hold."""

    def __init__(self, page):
        self.page = page
        self.holds = 0
        jb = self

        class Hold:
            def __init__(self, wait=300):
                pass

            def __enter__(self):
                jb.holds += 1
                return self

            def __exit__(self, *a):
                jb.holds -= 1
        self.exclusive = Hold
        self._worker = mock.Mock(run=lambda fn, timeout=None: fn(self.page), _page=page)


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.job = os.path.join(self.tmp.name, "job")
        os.makedirs(self.job)
        self.log = []
        self.grants = {}

    def tearDown(self):
        self.tmp.cleanup()

    def kit(self, page):
        self.jb = FakeJB(page)
        return JB.Toolkit(self.job, config.REPO_DIR, jb=self.jb, approved=lambda a: self.grants.get(a), log=self.log.append, sleep=lambda s: None)

    def test_a_job_that_used_the_browser_leaves_one_blank_tab_and_lets_go(self):
        page = FakePage({})
        kit = self.kit(page)
        self.jb.tidy_tabs = mock.Mock(return_value=2)
        kit.read() if hasattr(kit, "read") else kit._run(lambda p: "x")
        self.assertEqual(self.jb.holds, 1)
        kit.finish()
        self.jb.tidy_tabs.assert_called_once()
        self.assertEqual(self.jb.holds, 0)

    def test_a_job_that_never_used_the_browser_leaves_it_alone(self):
        kit = self.kit(FakePage({}))
        self.jb.tidy_tabs = mock.Mock()
        kit.finish()
        self.jb.tidy_tabs.assert_not_called()

    def test_publish_is_held_until_danny_approves_and_the_approval_is_logged(self):
        page = FakePage({"button:Publish": desc(label="Publish")})
        kit = self.kit(page)
        out = kit.click(text="Publish")
        self.assertIn("NEEDS DANNY'S APPROVAL", out)
        self.assertEqual(page.clicked, [])
        self.assertIn("Publish", kit.click(text="Publish", approval_id=99))        # an id Danny never approved
        self.assertEqual(page.clicked, [])
        self.grants[5] = {"id": 5, "what": "Publish 7 pins"}
        self.assertIn("Clicked 'Publish'", kit.click(text="Publish", approval_id=5))
        self.assertEqual(page.clicked, ["button:Publish"])
        self.assertEqual(self.log[-1]["covered_by_approval"], 5)
        self.assertEqual(self.jb.holds, 1)                                         # the browser is held for the job
        kit.release()
        self.assertEqual(self.jb.holds, 0)

    def test_payment_is_blocked_even_with_an_approval(self):
        page = FakePage({"button:Buy now": desc(label="Buy now")})
        self.grants[1] = {"id": 1, "what": "anything"}
        out = self.kit(page).click(text="Buy now", approval_id=1)
        self.assertIn("BLOCKED", out)
        self.assertEqual(page.clicked, [])

    def test_click_by_position_reads_what_is_there(self):
        page = FakePage({(100, 200): desc(label="Next")})
        self.assertIn("Clicked 'Next'", self.kit(page).click(x=100, y=200))
        self.assertEqual(page.clicked, [(100, 200)])

    def test_password_fields_are_never_filled(self):
        page = FakePage({'[data-jref="3"]': desc(tag="input", type="password")})
        self.assertIn("BLOCKED", self.kit(page).fill("hunter2", ref=3))
        self.assertEqual(page.filled, [])

    def test_fill_and_search(self):
        page = FakePage({"placeholder:Search": desc(tag="input", type="search", placeholder="Search")})
        out = self.kit(page).fill("christmas baubles", placeholder="Search", submit=True)
        self.assertIn("pressed Enter", out)
        self.assertEqual(page.keys, ["Enter"])

    def test_paste_uses_a_real_paste_event(self):
        page = FakePage({".public-DraftEditor-content": desc(tag="div", editable=True)})
        out = self.kit(page).paste("Gold glitter baubles 🎄", selector=".public-DraftEditor-content")
        self.assertIn("Gold glitter baubles", out)
        self.assertEqual(page.pasted, [[".public-DraftEditor-content", "Gold glitter baubles 🎄"]])

    def test_uploads_only_from_safe_places(self):
        page = FakePage()
        kit = self.kit(page)
        pic = os.path.join(self.job, "pin.png")
        open(pic, "wb").write(b"x")
        self.assertIn("Gave the page", kit.upload(pic))
        outside = os.path.join(self.tmp.name, "secret.txt")
        open(outside, "w").write("x")
        self.assertIn("BLOCKED", kit.upload(outside))
        self.assertIn("BLOCKED", kit.upload(os.path.join(self.job, "missing.png")))
        self.assertEqual(len(page.files), 1)

    def test_enter_in_a_form_needs_approval(self):
        page = FakePage()
        page.focused = desc(tag="input", name="title")
        kit = self.kit(page)
        self.assertIn("NEEDS DANNY'S APPROVAL", kit.press("Enter"))
        self.assertEqual(page.keys, [])
        self.assertIn("Pressed Escape", kit.press("Escape"))

    def test_screenshot_goes_in_the_job_folder(self):
        page = FakePage()
        out = self.kit(page).screenshot()
        self.assertTrue(page.shots[0].startswith(os.path.join(self.job, "screens")))
        self.assertIn("Read that file", out)

    def test_a_browser_error_comes_back_as_a_sentence(self):
        page = FakePage()
        page.goto = mock.Mock(side_effect=RuntimeError("net::ERR_NAME_NOT_RESOLVED"))
        self.assertIn("didn't work", self.kit(page).open("https://nope.invalid/"))

    def test_a_security_check_that_never_clears_is_handed_to_danny_s_own_browser(self):
        wall = {"url": "https://www.redbubble.com/portfolio/images/new", "text": "www.redbubble.com\nPerforming security verification\nJust a moment...",
                "frames": []}
        page = FakePage({"button:Upload": desc(label="Upload")}, info=wall)
        page.goto = mock.Mock()
        kit = self.kit(page)
        out = kit.open("https://www.redbubble.com/portfolio/images/new")
        self.assertIn("BLOCKED BY THE SITE", out)
        self.assertIn("own_browser", out)
        self.assertIn("BLOCKED BY THE SITE", kit.click(text="Upload"))
        # a real tick-box challenge is still Danny's job in Jarvis's window, not a wall
        boxed = dict(wall, frames=["https://challenges.cloudflare.com/cdn-cgi/challenge-platform/turnstile"])
        self.assertFalse(JB.site_wall(boxed))
        self.assertTrue(JB.page_blocked(boxed))
        # one that clears by itself is fine
        cleared = [wall, wall, dict(PAGE, url=wall["url"], text="Upload new work")]
        page2 = FakePage()
        page2.goto = mock.Mock()
        page2.evaluate = lambda js, arg=None: cleared.pop(0) if js == JB.PAGE_INFO_JS else None
        self.assertIn("Opened", self.kit(page2).open(wall["url"]))

    def test_after_letting_go_the_browser_is_taken_again_on_next_use(self):
        page = FakePage()
        kit = self.kit(page)
        kit.read()
        self.assertEqual(self.jb.holds, 1)
        kit.release()
        self.assertEqual(self.jb.holds, 0)
        kit.read()
        self.assertEqual(self.jb.holds, 1)

    def test_busy_browser_is_reported(self):
        page = FakePage()
        kit = self.kit(page)

        class Busy:
            def __init__(self, wait=0):
                pass

            def __enter__(self):
                raise TimeoutError("busy with another job")
        self.jb.exclusive = Busy
        self.assertIn("busy", kit.read())


# -------------------------------------------------------------- job tools
class FakeServer:
    """The window's side of approvals, scripted."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, path, body=None, timeout=60):
        self.calls.append((path, body))
        if path.endswith("/ask"):
            return {"approval_id": 7}
        if path.endswith("/say"):
            return {"ok": True}
        if path.startswith("/api/approvals/"):
            status = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            return {"id": 7, "run_id": 3, "status": status, "what": "Publish 7 pins", "note": "",
                    "expires_at": time.time() + 600 if status == "approved" else None}
        return {"error": "unknown"}


class JobToolsTests(unittest.TestCase):
    def make(self, answers, clock=None):
        srv = FakeServer(answers)
        t = job_mcp.JobTools(3, tempfile.gettempdir(), config.REPO_DIR, server=srv, toolkit=mock.Mock(),
                             sleep=lambda s: None, clock=clock or time.time)
        return t, srv

    def test_approval_waits_for_danny_then_grants_for_this_job_only(self):
        t, srv = self.make(["pending", "pending", "approved"])
        out = t.request_approval("Publish 7 pins to Christmas Decorations", "Danny asked for the range on Pinterest")
        self.assertIn("APPROVED", out)
        self.assertIn("approval_id=7", out)
        self.assertEqual(t.grant(7)["what"], "Publish 7 pins")
        self.assertIsNone(t.grant(8))
        self.assertEqual(srv.calls[0][1]["kind"], "approval")

    def test_rejected_and_closed_questions(self):
        t, _ = self.make(["rejected"])
        self.assertIn("REJECTED", t.request_approval("Install the app", "needed"))
        self.assertIsNone(t.grant(7))
        t, _ = self.make(["expired"])
        self.assertIn("closed", t.ask_danny_to_do("Click Install in Jarvis's browser"))

    def test_hand_requests_say_done(self):
        t, srv = self.make(["done"])
        self.assertIn("done it", t.ask_danny_to_do("Click the green Install button", "The store ignores automated clicks"))
        self.assertEqual(srv.calls[0][1]["kind"], "hand")

    def test_no_answer_yet_returns_so_the_job_can_wait_again(self):
        now = [1000.0]

        def clock():
            now[0] += 120
            return now[0]
        t, _ = self.make(["pending"], clock=clock)
        self.assertIn("hasn't answered yet", t.request_approval("Publish the pins", "x"))

    def test_waiting_on_danny_lets_go_of_the_browser_after_a_minute(self):
        now = [1000.0]

        def clock():
            now[0] += 30
            return now[0]
        t, _ = self.make(["pending", "pending", "pending", "pending", "approved"], clock=clock)
        t.request_approval("Publish the pins", "x")
        t.browser.release.assert_called_once()

    def test_a_step_in_jarvis_s_window_keeps_the_browser_for_danny(self):
        now = [1000.0]

        def clock():
            now[0] += 30
            return now[0]
        t, _ = self.make(["pending", "pending", "pending", "pending", "done"], clock=clock)
        t.ask_danny_to_do("Sign in to Pinterest in Jarvis's browser window")
        t.browser.release.assert_not_called()

    def test_own_browser_hand_off_opens_his_edge_and_lets_go_at_once(self):
        t, srv = self.make(["done"])
        opened = []
        t.open_own = lambda url: opened.append(url) or "Opened redbubble.com in Danny's normal browser."
        out = t.ask_danny_to_do("Click Upload new work, choose C:\\x\\design.png, title it 'Robin'", "Redbubble blocks Jarvis's browser",
                                where="own_browser", url="https://www.redbubble.com/portfolio/images/new")
        self.assertIn("done it", out)
        self.assertEqual(opened, ["https://www.redbubble.com/portfolio/images/new"])
        t.browser.release.assert_called()
        self.assertTrue(srv.calls[0][1]["what"].startswith("In YOUR OWN browser"))
        self.assertIn("url must", t.ask_danny_to_do("x y z", where="own_browser", url="file:///C:/x"))

    def test_an_approval_runs_out(self):
        now = [time.time()]
        t, _ = self.make(["approved"], clock=lambda: now[0])
        t.request_approval("Publish the pins", "x")
        self.assertIsNotNone(t.grant(7))
        now[0] += 3600
        self.assertIsNone(t.grant(7))

    def test_progress_is_spoken_but_not_too_often(self):
        t, srv = self.make(["pending"])
        self.assertEqual(t.say_progress("Pinterest is open, sir."), "Said.")
        self.assertIn("moment ago", t.say_progress("Another"))
        self.assertEqual(sum(1 for p, _ in srv.calls if p.endswith("/say")), 1)

    def test_the_job_gets_the_right_tools_only(self):
        t, _ = self.make(["pending"])
        names = set(t.TOOLS)
        self.assertTrue({"browser_click", "browser_paste", "browser_upload", "request_approval", "say_progress", "shop_orders"} <= names)
        for forbidden in ("pc_command", "start_job", "spawn_run", "improve_jarvis", "approve_document", "say_now"):
            self.assertNotIn(forbidden, names)
        for name in names:
            self.assertIsNone(tools.FORBIDDEN_NAMES.search(name), name)

    def test_served_over_mcp(self):
        t, _ = self.make(["pending"])
        from jarvis_v2 import mcp_server
        reply = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, t)
        self.assertIn("browser_open", [x["name"] for x in reply["result"]["tools"]])
        reply = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": "say_progress", "arguments": {"text": "hi"}}}, t)
        self.assertEqual(reply["result"]["content"][0]["text"], "Said.")


# ------------------------------------------------------------ the executor
class JobRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (config.DATA_DIR, config.USAGE_FILE)
        config.DATA_DIR = self.tmp.name
        config.USAGE_FILE = os.path.join(self.tmp.name, "usage.json")
        self.project = os.path.join(self.tmp.name, "Demo")
        os.makedirs(self.project)

    def tearDown(self):
        config.DATA_DIR, config.USAGE_FILE = self.old
        self.tmp.cleanup()

    def executor(self, scripts):
        started = []

        def popen(cmd, **kw):
            p = scripts.pop(0)
            started.append((cmd, kw, p))
            return p
        ex = runs.Executor(store=runs.RunStore(os.path.join(self.tmp.name, "runs.db")), popen=popen, exe="claude.exe",
                           claude_ok=lambda: True, poll_seconds=0.05)
        ex.started = started
        return ex

    def test_a_job_runs_on_opus_with_its_rules_in_its_own_folder(self):
        ex = self.executor([FakeRunProc([{"type": "system", "subtype": "init", "session_id": "j1"}, _result("Done. The board has 7 pins.")])])
        with mock.patch.object(runs, "claude_available", lambda: True):
            rid, text = jobs.start(ex, "Read the Pinterest board Christmas Decorations and count its pins")
        self.assertIn(f"job {rid}", text)
        self.assertEqual(ex.tick(), rid)
        run = wait_for(lambda: (r := ex.store.get(rid))["status"] in runs.TERMINAL and r)
        self.assertEqual(run["status"], "succeeded")
        cmd, kw, _ = ex.started[0]
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(kw["cwd"], run["cwd"])
        self.assertTrue(run["cwd"].startswith(os.path.join(self.tmp.name, "jobs")))
        self.assertEqual(kw["env"]["JARVIS_JOB_ID"], str(rid))
        self.assertFalse(any(k.startswith("ANTHROPIC_") for k in kw["env"]))
        settings = json.load(open(cmd[cmd.index("--settings") + 1], encoding="utf-8"))
        hook = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "*")
        self.assertIn("job_guard.py", hook["hooks"][0]["command"])
        self.assertIn("Bash(git push*)", settings["permissions"]["deny"])
        mcp = json.load(open(cmd[cmd.index("--mcp-config") + 1], encoding="utf-8"))
        self.assertEqual(mcp["mcpServers"]["jobkit"]["args"], ["-m", "jarvis_v2.job_mcp"])
        self.assertFalse(cmd[cmd.index("--settings") + 1].startswith(run["cwd"]))      # its rules live outside its folder
        brief = open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8").read()
        self.assertIn("request_approval", brief)
        self.assertIn("count its pins", brief)

    def test_jobs_and_runs_have_separate_lanes(self):
        hold = threading.Event()
        ex = self.executor([FakeRunProc([], hold=hold), FakeRunProc([], hold=threading.Event())])
        with mock.patch.object(runs, "claude_available", lambda: True):
            job, _ = jobs.start(ex, "A long browser job for the store")
        run = ex.submit("Demo", self.project, "fix a typo")
        self.assertEqual(ex.tick(), job)
        self.assertEqual(ex.tick(), run)                     # the run isn't stuck behind the job
        ex.cancel(job)
        ex.cancel(run)
        ex.join(job, 5)
        ex.join(run, 5)

    def test_out_of_allowance_pauses_the_job_and_it_carries_on_later(self):
        resets = time.time() + 3600
        ex = self.executor([FakeRunProc([{"type": "system", "subtype": "init", "session_id": "sess-9"},
                                         {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": resets}},
                                         _result("Claude usage limit reached", is_error=True)], code=1),
                            FakeRunProc([_result("Finished the job.")])])
        with mock.patch.object(runs, "claude_available", lambda: True), \
                mock.patch("jarvis_v2.brains.claude.record_usage", lambda info: None):
            rid, _ = jobs.start(ex, "Put the Christmas range on Pinterest")
            ex.tick()
            run = wait_for(lambda: (r := ex.store.get(rid))["status"] == "waiting_for_claude" and r)
        self.assertEqual(run["meta"]["resume_session"], "sess-9")
        self.assertAlmostEqual(run["meta"]["resume_at"], resets + 60, delta=2)
        self.assertIsNone(ex.tick())                         # not before the allowance renews
        ex.store.update(rid, meta=dict(run["meta"], resume_at=time.time() - 1))
        self.assertEqual(ex.tick(), rid)
        done = wait_for(lambda: (r := ex.store.get(rid))["status"] in runs.TERMINAL and r)
        self.assertEqual(done["status"], "succeeded")
        cmd = ex.started[1][0]
        self.assertEqual(cmd[cmd.index("--resume") + 1], "sess-9")
        self.assertIn("Carry on", cmd[cmd.index("-p") + 1])
        self.assertAlmostEqual(done["cost_usd"], 0.02, places=3)            # both parts counted

    def test_a_restart_carries_jobs_on_but_fails_runs(self):
        store = runs.RunStore(os.path.join(self.tmp.name, "runs.db"))
        job = store.create("Job", self.project, "x", kind="job", status="running")
        store.update(job, session_id="s1")
        fresh = store.create("Job", self.project, "y", kind="job", status="running")
        run = store.create("Demo", self.project, "z", status="running")
        runs.Executor(store=store, popen=None)._recover()
        self.assertEqual((store.get(job)["status"], store.get(job)["meta"]["resume_session"]), ("waiting_for_claude", "s1"))
        self.assertEqual(store.get(fresh)["status"], "waiting_for_claude")
        self.assertNotIn("resume_session", store.get(fresh)["meta"])
        self.assertEqual(store.get(run)["status"], "failed")

    def test_every_job_ending_stops_its_helpers_so_the_browser_is_freed(self):
        stopped = []
        with mock.patch.object(jobs, "stop_helpers", lambda rid, proc=None, **k: stopped.append((rid, proc is not None))):
            hold = threading.Event()
            ex = self.executor([FakeRunProc([], hold=hold), FakeRunProc([_result("done")])])
            with mock.patch.object(runs, "claude_available", lambda: True):
                a, _ = jobs.start(ex, "A job Danny will cancel")
                ex.tick()
                wait_for(lambda: ex.store.get(a)["status"] == "running")
                ex.cancel(a)
                ex.join(a, 5)
                self.assertIn((a, True), stopped)              # cancel: the live process tree
                b, _ = jobs.start(ex, "A job that finishes normally")
                ex.tick()
                ex.join(b, 5)
        self.assertIn((b, False), stopped)                       # a normal finish: any orphaned tool server
        self.assertGreaterEqual(sum(1 for rid, _ in stopped if rid == a), 2)

    def test_stop_helpers_kills_the_tree_and_only_a_real_orphaned_tool_server(self):
        killed = []
        proc = mock.Mock(pid=4242, poll=lambda: None)
        os.makedirs(jobs.config_dir(9), exist_ok=True)
        with open(jobs.helper_pid_file(9), "w") as f:
            f.write("5151")
        jobs.stop_helpers(9, proc, cmdline=lambda pid: "python -m jarvis_v2.job_mcp", kill=killed.append)
        self.assertEqual(killed, [4242, 5151])
        self.assertFalse(os.path.exists(jobs.helper_pid_file(9)))
        with open(jobs.helper_pid_file(9), "w") as f:
            f.write("6161")                                      # a stale pid now used by something else
        killed.clear()
        jobs.stop_helpers(9, None, cmdline=lambda pid: "notepad.exe", kill=killed.append)
        self.assertEqual(killed, [])
        jobs.stop_helpers(10, None, cmdline=lambda pid: "", kill=killed.append)   # nothing recorded: nothing to do
        self.assertEqual(killed, [])

    def test_time_waiting_for_danny_doesnt_count(self):
        ex = self.executor([FakeRunProc([], hold=threading.Event())])
        ex.extra_time = lambda rid: 10_000
        with mock.patch.object(runs, "claude_available", lambda: True):
            rid, _ = jobs.start(ex, "A job that waits for Danny")
        ex.store.update(rid, meta=dict(ex.store.get(rid)["meta"], timeout=0.1))
        ex.tick()
        time.sleep(2.5)
        self.assertEqual(ex.store.get(rid)["status"], "running")
        ex.cancel(rid)
        ex.join(rid, 5)


class AllowanceTests(unittest.TestCase):
    def test_warning_and_resume_time(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "usage.json")
            now = time.time()
            json.dump({"windows": {"five_hour": {"utilization": 0.8, "resets_at": now + 1800},
                                   "seven_day": {"utilization": 0.2, "resets_at": now + 86400}}}, open(path, "w"))
            self.assertIn("80%", jobs.allowance_note(path, now))
            json.dump({"windows": {"five_hour": {"utilization": 1.0, "resets_at": now + 1800}}}, open(path, "w"))
            self.assertAlmostEqual(jobs.resume_time(None, path, now), now + 1860, delta=1)
            self.assertAlmostEqual(jobs.resume_time(now + 99, path, now), now + 159, delta=1)
            json.dump({"windows": {"five_hour": {"utilization": 0.1}}}, open(path, "w"))
            self.assertEqual(jobs.allowance_note(path, now), "")
            self.assertAlmostEqual(jobs.resume_time(None, path, now), now + 1800, delta=1)


class AnnouncementTests(unittest.TestCase):
    def test_finish_failure_and_pause_are_each_said_once(self):
        said = []
        ann = mock.Mock(urgent=said.append)
        with tempfile.TemporaryDirectory() as d:
            approvals = jobs.ApprovalStore(os.path.join(d, "runs.db"))
            listen = jobs.on_change(ann, approvals)
            run = {"kind": "job", "status": "succeeded", "result": "I added 7 pins to your board. Details below.", "meta": {}}
            listen(1, run)
            listen(1, run)
            self.assertEqual(said, ["Job 1 is done, sir. I added 7 pins to your board."])
            listen(2, {"kind": "job", "status": "waiting_for_claude", "meta": {"resume_session": "s", "resume_at": time.time() + 600}})
            self.assertIn("carry on", said[-1])
            listen(3, {"kind": "run", "status": "succeeded", "meta": {}})
            self.assertEqual(len(said), 2)


# --------------------------------------------------------------- the server
class JobServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (config.DATA_DIR, config.BRAIN_STATE_FILE, config.USAGE_FILE, config.ENGINE_URL, server.TOKEN_FILE, server.ENGINE_FLAG_FILE)
        config.DATA_DIR = self.tmp.name
        config.BRAIN_STATE_FILE = os.path.join(self.tmp.name, "brain_state.json")
        config.USAGE_FILE = os.path.join(self.tmp.name, "usage.json")
        config.ENGINE_URL = f"http://127.0.0.1:{_free_port()}"
        server.TOKEN_FILE = os.path.join(self.tmp.name, "server_token")
        server.ENGINE_FLAG_FILE = os.path.join(self.tmp.name, "use_v2_brain")
        self.said = []
        self.port = _free_port()
        router = R.Router([FakeBrain("claude", ["ok"]), FakeBrain("openai", []), FakeBrain("local", [])], state_path=config.BRAIN_STATE_FILE)
        self.executor = runs.Executor(store=runs.RunStore(os.path.join(self.tmp.name, "runs.db")), popen=None)
        self.srv, self.state = server.start(router, port=self.port, executor=self.executor,
                                            announcer=mock.Mock(urgent=self.said.append, history=[]))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        (config.DATA_DIR, config.BRAIN_STATE_FILE, config.USAGE_FILE, config.ENGINE_URL, server.TOKEN_FILE, server.ENGINE_FLAG_FILE) = self.old
        self.tmp.cleanup()

    def call(self, path, body=None, ui=False):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET")
        req.add_header("X-Jarvis-Token", self.state.token)
        if ui:
            req.add_header("X-Jarvis-UI", self.state.ui_token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_the_whole_approval_round_trip(self):
        code, out = self.call("/api/jobs", {"goal": "Add the Christmas range to Pinterest"})
        self.assertEqual(code, 200, out)
        rid = out["run_id"]
        self.assertIn("On it, sir", out["text"])
        self.executor.store.update(rid, status="running", started_at=time.time())        # as if the fake claude started

        code, out = self.call(f"/api/jobs/{rid}/ask", {"kind": "approval", "what": "Publish 7 pins", "why": "the Christmas range"})
        aid = out["approval_id"]
        self.assertIn("needs your OK", self.said[-1])
        self.assertIn("Publish 7 pins", jobs.pending_text(os.path.join(self.tmp.name, "runs.db")))
        self.assertEqual(self.call("/api/approvals")[1]["waiting_for_you"][0]["id"], aid)

        # Only the window's own buttons can answer: the job (and the brain) only have the server token.
        code, out = self.call(f"/api/approvals/{aid}/approve", {})
        self.assertEqual(code, 403)
        self.assertEqual(self.call(f"/api/approvals/{aid}")[1]["status"], "pending")
        code, out = self.call(f"/api/approvals/{aid}/approve", {}, ui=True)
        self.assertEqual((code, out["approval"]["status"]), (200, "approved"))
        self.assertGreater(out["approval"]["expires_at"], time.time())
        self.assertEqual(self.call(f"/api/approvals/{aid}/reject", {}, ui=True)[0], 400)     # can't change the answer

        code, out = self.call(f"/api/jobs/{rid}")
        self.assertEqual(out["approvals"][0]["status"], "approved")
        self.assertIn("Job " + str(rid), self.call(f"/api/jobs/{rid}/describe")[1]["text"])

    def test_say_and_stop(self):
        rid = self.call("/api/jobs", {"goal": "Check every product page for broken images"})[1]["run_id"]
        self.assertEqual(self.call(f"/api/jobs/{rid}/say", {"text": "hi"})[0], 400)           # not running yet
        self.executor.store.update(rid, status="running")
        self.assertEqual(self.call(f"/api/jobs/{rid}/say", {"text": "Halfway through the products, sir."})[0], 200)
        self.assertEqual(self.said[-1], "Halfway through the products, sir.")
        aid = self.call(f"/api/jobs/{rid}/ask", {"kind": "hand", "what": "Click Install in Jarvis's browser"})[1]["approval_id"]
        self.assertIn("Press Done", self.said[-1])
        self.assertIn("Stopped", self.call(f"/api/jobs/{rid}/cancel", {})[1]["text"])
        self.assertEqual(self.call(f"/api/approvals/{aid}")[1]["status"], "expired")
        self.assertEqual(self.call("/api/approvals")[1]["waiting_for_you"], [])

    def test_a_tiny_goal_is_refused(self):
        self.assertEqual(self.call("/api/jobs", {"goal": "x"})[0], 400)

    def test_window_has_the_jobs_tab_and_cards(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as r:
            page = r.read().decode()
        self.assertIn('data-pane="jobs"', page)
        self.assertIn("approvalCards", page)
        self.assertIn("renderApprovals", page)


if __name__ == "__main__":
    unittest.main()
