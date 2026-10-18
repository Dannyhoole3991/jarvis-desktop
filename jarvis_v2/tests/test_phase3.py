"""
Phase 3 tests: runs, builds/specs, self-improvement and their server endpoints.
Fake Claude processes and a throwaway git repository; Danny's real repo and accounts are never touched.

    py -3 -m unittest jarvis_v2.tests.test_phase3 -v
"""

import json
import os
import queue
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from jarvis_v2 import builds, config, projects as P, runs, selfimprove, server
from jarvis_v2 import router as R
from jarvis_v2.tests.test_v2 import FakeBrain


class FakeRunProc:
    """A finished-on-demand claude -p process: emits scripted stream-json lines."""

    def __init__(self, lines, code=0, hold=None):
        self.q = queue.Queue()
        for line in lines:
            self.q.put(json.dumps(line) + "\n")
        self.hold = hold
        if hold is None:
            self.q.put(None)
        self.code = code
        self.returncode = None
        self.stdout = iter(self.q.get, None)

    def finish(self):
        self.q.put(None)

    def poll(self):
        return self.returncode

    def wait(self):
        if self.hold is not None:
            self.hold.wait(5)
        self.returncode = self.code if self.returncode is None else self.returncode
        return self.returncode

    def kill(self):
        self.returncode = -9
        self.q.put(None)
        if self.hold is not None:
            self.hold.set()


def _result(text, is_error=False):
    return {"type": "result", "result": text, "is_error": is_error, "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01}


def wait_for(fn, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    return fn()


class TmpMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.project = os.path.join(self.dir, "Demo")
        os.makedirs(self.project)
        self.patches = [
            mock.patch.object(builds, "BUILDS_FILE", os.path.join(self.dir, "builds.json")),
            mock.patch.object(builds, "APPROVALS_FILE", os.path.join(self.dir, "approvals.jsonl")),
            mock.patch.object(P, "discover", lambda: [{"name": "Demo", "path": self.project, "source": "test"}]),
            mock.patch.object(config, "CODE_JOBS", "local"),     # the run-on-this-PC path (cloud: test_cloud_jobs)
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def executor(self, scripts, claude_ok=lambda: True):
        procs = []

        def popen(cmd, **kw):
            p = scripts.pop(0)
            procs.append((cmd, kw, p))
            return p
        ex = runs.Executor(store=runs.RunStore(os.path.join(self.dir, "runs.db")), popen=popen, exe="claude.exe",
                           claude_ok=claude_ok, poll_seconds=0.05)
        ex.procs_started = procs
        return ex


class RunTests(TmpMixin, unittest.TestCase):
    def test_a_run_is_recorded_from_start_to_finish(self):
        ex = self.executor([FakeRunProc([{"type": "system", "subtype": "init", "session_id": "s9"},
                                         {"type": "assistant", "message": {"content": [{"type": "text", "text": "Fixing it"}]}},
                                         _result("Fixed the footer typo.")])])
        rid = ex.submit("Demo", self.project, "fix the typo")
        self.assertEqual(ex.store.get(rid)["status"], "queued")
        self.assertEqual(ex.tick(), rid)
        run = wait_for(lambda: (r := ex.store.get(rid))["status"] in runs.TERMINAL and r)
        self.assertEqual((run["status"], run["session_id"], run["output_tokens"]), ("succeeded", "s9", 5))
        self.assertIn("Fixed the footer", run["result"])
        events = ex.store.events(rid)
        self.assertEqual([e["type"] for e in events], ["system", "assistant", "result"])
        cmd, kw, _ = ex.procs_started[0]
        self.assertIn("acceptEdits", cmd)
        self.assertIn("Bash(git push*)", cmd)
        self.assertEqual(kw["cwd"], self.project)
        self.assertFalse(any(k.startswith("ANTHROPIC_") for k in kw["env"]))

    def test_error_result_is_a_failure_and_usage_limit_is_explained(self):
        ex = self.executor([FakeRunProc([_result("Claude usage limit reached", is_error=True)], code=1)])
        rid = ex.submit("Demo", self.project, "x")
        ex.tick()
        run = wait_for(lambda: (r := ex.store.get(rid))["status"] in runs.TERMINAL and r)
        self.assertEqual(run["status"], "failed")
        self.assertIn("allowance ran out", run["error"])

    def test_waits_while_claude_is_out_of_allowance(self):
        allowed = {"ok": False}
        ex = self.executor([FakeRunProc([_result("done")])], claude_ok=lambda: allowed["ok"])
        rid = ex.submit("Demo", self.project, "x")
        self.assertIsNone(ex.tick())
        self.assertEqual(ex.store.get(rid)["status"], "waiting_for_claude")
        allowed["ok"] = True
        self.assertEqual(ex.tick(), rid)
        self.assertEqual(wait_for(lambda: ex.store.get(rid)["status"] == "succeeded"), True)

    def test_cancel_stops_it(self):
        hold = __import__("threading").Event()
        proc = FakeRunProc([{"type": "system", "subtype": "init", "session_id": "s"}], hold=hold)
        ex = self.executor([proc])
        rid = ex.submit("Demo", self.project, "long job")
        ex.tick()
        wait_for(lambda: ex.store.get(rid)["status"] == "running")
        self.assertTrue(ex.cancel(rid))
        run = wait_for(lambda: (r := ex.store.get(rid))["finished_at"] and r)
        self.assertEqual(run["status"], "cancelled")
        self.assertFalse(ex.cancel(rid))

    def test_one_at_a_time(self):
        hold = __import__("threading").Event()
        ex = self.executor([FakeRunProc([], hold=hold), FakeRunProc([_result("b")])])
        a = ex.submit("Demo", self.project, "a")
        b = ex.submit("Demo", self.project, "b")
        self.assertEqual(ex.tick(), a)
        self.assertIsNone(ex.tick())
        self.assertEqual(ex.store.get(b)["status"], "queued")
        ex.cancel(a)
        ex.join(a, 5)                             # let its thread finish writing before the temp folder goes

    def test_runs_left_running_by_a_restart_are_marked_failed(self):
        store = runs.RunStore(os.path.join(self.dir, "runs.db"))
        rid = store.create("Demo", self.project, "x", status="running")
        runs.Executor(store=store, popen=None)._recover()
        self.assertEqual(store.get(rid)["status"], "failed")

    def test_missing_folder_is_refused(self):
        ex = self.executor([])
        with self.assertRaises(ValueError):
            ex.submit("Demo", os.path.join(self.dir, "nope"), "x")


class BuildTests(TmpMixin, unittest.TestCase):
    def test_spec_then_approval_starts_the_build_and_progress_is_read_from_the_plan(self):
        b = builds.start_build("demo", "Shopping list app", "A tiny CLI shopping list.", "Python only", "No web UI", now=1_790_000_000)
        spec = os.path.join(self.project, b["spec"])
        text = open(spec, encoding="utf-8").read()
        self.assertIn("## 1. Goal", text)
        self.assertIn("awaiting Danny's approval", text)
        self.assertEqual(b["status"], "awaiting_approval")
        self.assertIn("waiting for your approval", builds.build_status("demo"))

        submitted = []
        fake_ex = mock.Mock(submit=lambda *a, **k: submitted.append((a, k)) or 42)
        msg = builds.approve_document("demo", b["spec"], fake_ex)
        self.assertIn("run 42", msg)
        (name, cwd, brief), kw = submitted[0]
        self.assertEqual(cwd, self.project)
        self.assertIn(b["plan"], brief)
        self.assertIn("test-driven", brief)
        self.assertIn("_Approved by Danny", open(spec, encoding="utf-8").read())

        plan = os.path.join(self.project, b["plan"])
        os.makedirs(os.path.dirname(plan), exist_ok=True)
        with open(plan, "w", encoding="utf-8") as f:
            f.write("# Plan\n- [x] one\n- [x] two\n- [ ] three\n")
        self.assertIn("2 of 3 plan tasks done", builds.build_status("demo"))
        builds.on_run_change(42, {"status": "succeeded", "meta": {"build_id": b["id"]}})
        self.assertEqual(builds.builds_list()[0]["status"], "finished")

    def test_sections_are_numbered_and_readable(self):
        b = builds.start_build("demo", "X", "Goal text here.")
        out = builds.review_document("demo", b["spec"])
        self.assertIn("2. 1. Goal", out)
        self.assertIn("Goal text here.", builds.review_document("demo", b["spec"], 2))

    def test_jarvis_himself_is_never_built_in_place(self):
        with mock.patch.object(P, "discover", lambda: [{"name": "Jarvis", "path": config.REPO_DIR, "source": "t"}]):
            with self.assertRaises(ValueError):
                builds.start_build("jarvis", "x", "y")


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class SelfImproveTests(TmpMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.dir, "repo")
        os.makedirs(self.repo)
        _git(self.repo, "init", "-b", "master")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        with open(os.path.join(self.repo, "app.py"), "w") as f:
            f.write("x = 1\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "init")
        for p in (mock.patch.object(selfimprove, "PROPOSALS_FILE", os.path.join(self.dir, "proposals.json")),
                  mock.patch.object(selfimprove, "WORKTREE_ROOT", os.path.join(self.dir, "worktrees"))):
            p.start()
            self.patches.append(p)
        self.ex = mock.Mock(submit=lambda *a, **k: 7)

    def _change(self, rel, content, commit=True):
        p = selfimprove.listing()[0]
        path = os.path.join(p["worktree"], rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        if commit:
            _git(p["worktree"], "add", "-A")
            _git(p["worktree"], "commit", "-m", "change")
        return p

    def test_full_cycle_propose_check_apply_undo(self):
        p = selfimprove.propose("make x two", self.ex, repo=self.repo)
        self.assertEqual(p["status"], "working")
        self.assertTrue(os.path.isdir(p["worktree"]))
        self.assertEqual(open(os.path.join(self.repo, "app.py")).read(), "x = 1\n", "the live code is untouched while he works")
        self._change("app.py", "x = 2\n", commit=False)   # left uncommitted: finalise commits it for review
        out = selfimprove.finalise(p["id"], {"status": "succeeded", "result": "Changed x to 2."}, repo=self.repo,
                                   run_tests=lambda wt, files: (True, "OK"))
        self.assertEqual(out["status"], "ready_for_review")
        self.assertEqual(out["files"], ["app.py"])
        self.assertIn("x = 2", selfimprove.diff(p["id"], repo=self.repo))
        selfimprove.approve(p["id"], repo=self.repo)
        self.assertEqual(open(os.path.join(self.repo, "app.py")).read(), "x = 2\n")
        self.assertFalse(os.path.isdir(p["worktree"]))
        selfimprove.undo(p["id"], repo=self.repo)
        self.assertEqual(open(os.path.join(self.repo, "app.py")).read(), "x = 1\n")
        self.assertEqual(selfimprove.get(p["id"])["status"], "undone")

    def test_protected_files_block_the_change(self):
        p = selfimprove.propose("tweak trading", self.ex, repo=self.repo)
        self._change("jarvis_trading/live/trader.py", "LIVE = True\n")
        out = selfimprove.finalise(p["id"], {"status": "succeeded", "result": ""}, repo=self.repo, run_tests=lambda *a: (True, ""))
        self.assertEqual(out["status"], "blocked")
        with self.assertRaises(ValueError):
            selfimprove.approve(p["id"], repo=self.repo)

    def test_failing_tests_can_never_be_applied(self):
        p = selfimprove.propose("break it", self.ex, repo=self.repo)
        self._change("app.py", "x = 3\n")
        out = selfimprove.finalise(p["id"], {"status": "succeeded", "result": ""}, repo=self.repo, run_tests=lambda *a: (False, "FAILED (1)"))
        self.assertEqual(out["status"], "tests_failed")
        with self.assertRaises(ValueError):
            selfimprove.approve(p["id"], repo=self.repo)
        selfimprove.reject(p["id"], repo=self.repo)
        self.assertEqual(open(os.path.join(self.repo, "app.py")).read(), "x = 1\n")

    def test_unsaved_live_changes_must_be_saved_first(self):
        with open(os.path.join(self.repo, "app.py"), "w") as f:
            f.write("x = 99\n")
        with self.assertRaises(ValueError) as ctx:
            selfimprove.propose("anything", self.ex, repo=self.repo)
        self.assertIn("unsaved", str(ctx.exception))

    def test_a_failed_run_is_never_reviewed(self):
        p = selfimprove.propose("x", self.ex, repo=self.repo)
        out = selfimprove.finalise(p["id"], {"status": "timed_out", "error": "too long"}, repo=self.repo)
        self.assertEqual(out["status"], "run_failed")


class Phase3ServerTests(TmpMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.old = (config.DATA_DIR, config.BRAIN_STATE_FILE, server.TOKEN_FILE, server.ENGINE_FLAG_FILE, config.ENGINE_URL)
        config.DATA_DIR = self.dir
        config.BRAIN_STATE_FILE = os.path.join(self.dir, "brain_state.json")
        server.TOKEN_FILE = os.path.join(self.dir, "server_token")
        server.ENGINE_FLAG_FILE = os.path.join(self.dir, "flag")
        config.ENGINE_URL = "http://127.0.0.1:9"
        self.ex = self.executor([FakeRunProc([_result("ok")])])
        router = R.Router([FakeBrain("claude", ["fine"])], state_path=config.BRAIN_STATE_FILE)
        self.srv, self.state = server.start(router, port=self.port, executor=self.ex)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        config.DATA_DIR, config.BRAIN_STATE_FILE, server.TOKEN_FILE, server.ENGINE_FLAG_FILE, config.ENGINE_URL = self.old
        super().tearDown()

    def call(self, path, body=None, ui=False):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                     method="POST" if body is not None else "GET")
        req.add_header("X-Jarvis-Token", self.state.token)
        if ui:
            req.add_header("X-Jarvis-UI", self.state.ui_token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_run_endpoints(self):
        status, r = self.call("/api/runs", {"project": "demo", "prompt": "do x"})
        self.assertEqual(status, 200)
        self.ex.tick()
        wait_for(lambda: self.ex.store.get(r["run_id"])["status"] == "succeeded")
        status, detail = self.call(f"/api/runs/{r['run_id']}")
        self.assertEqual(detail["status"], "succeeded")
        self.assertTrue(any("Finished" in l["text"] for l in detail["transcript"]))
        self.assertIn("succeeded", self.call("/api/runs/describe")[1]["text"])

    def test_runs_on_jarvis_himself_are_refused(self):
        with mock.patch.object(P, "discover", lambda: [{"name": "Jarvis", "path": config.REPO_DIR, "source": "t"}]):
            status, r = self.call("/api/runs", {"project": "jarvis", "prompt": "edit yourself"})
        self.assertEqual(status, 400)
        self.assertIn("improve_jarvis", r["error"])

    def test_voice_approval_needs_dannys_own_words(self):
        b = builds.start_build("demo", "T", "G")
        self.state.current_user_text = "what's the weather"
        status, r = self.call("/api/doc/approve", {"project": "demo", "doc": b["spec"], "via": "voice"})
        self.assertEqual(status, 403)
        self.state.current_user_text = "yes approve it, go ahead"
        status, r = self.call("/api/doc/approve", {"project": "demo", "doc": b["spec"], "via": "voice"})
        self.assertEqual(status, 200)
        self.assertIn("build", r["text"])

    def test_only_the_window_can_apply_or_reject_jarvis_changes(self):
        for act in ("approve", "reject", "undo"):
            status, r = self.call(f"/api/proposals/123/{act}", {})
            self.assertEqual(status, 403, act)
        status, r = self.call("/api/proposals/123/approve", {}, ui=True)
        self.assertEqual(status, 400)          # gets past the gate; the change just doesn't exist

    def test_documents_are_confined_to_the_project(self):
        status, r = self.call("/api/doc?project=demo&doc=..%2F..%2Fsecret.txt")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
