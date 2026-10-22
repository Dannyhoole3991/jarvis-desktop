"""
Cloud first for coding jobs (cloud.py, selfimprove.py, builds.py).
The cloud CLI is faked; "GitHub" is a throwaway bare git repository, so pushes, ls-remote and fetches are real git
but nothing leaves the machine. Danny's real repo, accounts and cloud credit are never touched.

    py -3 -m unittest jarvis_v2.tests.test_cloud_jobs -v
"""

import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from jarvis_v2 import builds, cloud, config, projects as P, selfimprove

SESSION_OUT = "Created cloud session session_01AbCdEfGhIj\nhttps://claude.ai/code/session_01AbCdEfGhIj\n"


def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _init(path, files):
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-b", "master")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    for rel, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(path, rel)) or path, exist_ok=True)
        with open(os.path.join(path, rel), "w") as f:
            f.write(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")


class FakeCloud:
    """Stands in for `claude --cloud`: records the brief and, like a cloud session, can push a branch to origin."""

    def __init__(self, test, code=0, output=SESSION_OUT, branch_re=r"claude/jarvis-(?:improve|build)-\d+"):
        self.test, self.code, self.output, self.branch_re = test, code, output, branch_re
        self.calls = []

    def __call__(self, brief, cwd):
        self.calls.append((brief, cwd))
        return self.code, self.output

    def push_work(self, files, message, done=True):
        brief, _ = self.calls[-1]
        branch = re.search(self.branch_re, brief).group(0)
        key = re.search(cloud.DONE_MARK + r" (\d+)", brief).group(1)
        work = tempfile.mkdtemp(dir=self.test.dir)
        _git(work, "clone", "-q", self.test.origin, ".")
        _git(work, "config", "user.email", "c@c")
        _git(work, "config", "user.name", "cloud")
        _git(work, "checkout", "-q", "-b", branch)
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(work, rel)) or work, exist_ok=True)
            with open(os.path.join(work, rel), "w") as f:
                f.write(text)
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", message + (f"\n\n{cloud.DONE_MARK} {key}" if done else ""))
        _git(work, "push", "-q", "origin", branch)
        return branch


class CloudTmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.origin = os.path.join(self.dir, "origin.git")
        _git(self.dir, "init", "-q", "--bare", "-b", "master", self.origin)
        self.submitted = []
        self.ex = mock.Mock(submit=lambda *a, **k: self.submitted.append((a, k)) or 7)
        self.patches = [mock.patch.object(config, "CODE_JOBS", "cloud"),
                        mock.patch.object(selfimprove, "PROPOSALS_FILE", os.path.join(self.dir, "proposals.json")),
                        mock.patch.object(selfimprove, "WORKTREE_ROOT", os.path.join(self.dir, "worktrees")),
                        mock.patch.object(builds, "BUILDS_FILE", os.path.join(self.dir, "builds.json")),
                        mock.patch.object(builds, "APPROVALS_FILE", os.path.join(self.dir, "approvals.jsonl"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def fake_cloud(self, **kw):
        fake = FakeCloud(self, **kw)
        p = mock.patch.object(cloud, "cloud_cli", fake)
        p.start()
        self.patches.append(p)
        return fake


class CloudSelfImproveTests(CloudTmp):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.dir, "repo")
        _init(self.repo, {"app.py": "x = 1\n"})
        _git(self.repo, "remote", "add", "origin", self.origin)

    def propose(self, goal="make x two"):
        return selfimprove.propose(goal, self.ex, repo=self.repo, background=False)

    def test_cloud_success_is_fetched_checked_and_then_reviewed_like_a_local_change(self):
        fake = self.fake_cloud()
        p = self.propose()
        self.assertEqual((p["status"], p["where"]), ("working", "cloud"))
        self.assertEqual(self.submitted, [], "nothing ran on this PC")
        self.assertEqual(_git(self.origin, "rev-parse", "master"), _git(self.repo, "rev-parse", "master"), "master was pushed first")
        brief = fake.calls[0][0]
        self.assertIn(f"claude/jarvis-improve-{p['id']}", brief)
        self.assertIn("jarvis_trading/live/", brief)
        self.assertIn("Never push to master", brief)
        self.assertEqual(p["cloud"]["session_id"], "session_01AbCdEfGhIj")
        self.assertIn("claude.ai/code/session_01AbCdEfGhIj", p["cloud"]["url"])

        self.assertEqual(selfimprove.check_cloud(p["id"], repo=self.repo)["status"], "working", "not there yet: keep waiting")
        fake.push_work({"app.py": "x = 2\n"}, "Changed x to 2 so it's two.")
        tests_run = []
        out = selfimprove.check_cloud(p["id"], repo=self.repo, run_tests=lambda wt, files: tests_run.append(files) or (True, "OK"))
        self.assertEqual(out["status"], "ready_for_review")
        self.assertEqual(out["files"], ["app.py"])
        self.assertEqual(tests_run, [["app.py"]], "Jarvis ran the tests himself")
        self.assertIn("Changed x to 2", out["summary"])
        self.assertNotIn(cloud.DONE_MARK, out["summary"])
        self.assertTrue(os.path.isdir(out["worktree"]))
        self.assertIn("in the cloud", selfimprove.describe(out))
        self.assertEqual(open(os.path.join(self.repo, "app.py")).read(), "x = 1\n", "nothing applied until Danny approves")
        selfimprove.approve(p["id"], repo=self.repo)
        self.assertEqual(open(os.path.join(self.repo, "app.py")).read(), "x = 2\n")

    def test_protected_files_from_the_cloud_are_still_blocked(self):
        fake = self.fake_cloud()
        p = self.propose("tweak trading")
        fake.push_work({"jarvis_trading/live/trader.py": "LIVE = True\n"}, "Turned it on.")
        out = selfimprove.check_cloud(p["id"], repo=self.repo, run_tests=lambda *a: (True, ""))
        self.assertEqual(out["status"], "blocked")
        with self.assertRaises(ValueError):
            selfimprove.approve(p["id"], repo=self.repo)

    def test_failing_tests_from_the_cloud_can_never_be_applied(self):
        fake = self.fake_cloud()
        p = self.propose()
        fake.push_work({"app.py": "x = 3\n"}, "Oops.")
        out = selfimprove.check_cloud(p["id"], repo=self.repo, run_tests=lambda *a: (False, "FAILED (1)"))
        self.assertEqual(out["status"], "tests_failed")

    def test_cloud_cli_failure_falls_back_to_a_local_run(self):
        fake = self.fake_cloud(code=1, output="Error: You have no cloud credit remaining.\n")
        p = self.propose()
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual((p["status"], p["where"], p["run_id"]), ("working", "this PC", 7))
        self.assertIn("can't take it", p["cloud_fallback"])
        (project, cwd, prompt), kw = self.submitted[0]
        self.assertEqual((cwd, kw["kind"]), (p["worktree"], "self_improve"))
        self.assertIn("Never push.", prompt)
        self.assertTrue(os.path.isdir(p["worktree"]))
        self.assertIn("on this PC", selfimprove.describe(p))

    def test_no_session_in_the_output_falls_back_too(self):
        self.fake_cloud(code=0, output="Welcome to Claude Code!\n")
        p = self.propose()
        self.assertEqual(p["where"], "this PC")
        self.assertIn("didn't say which session", p["cloud_fallback"])

    def test_push_failure_falls_back_to_local_without_starting_the_cloud(self):
        fake = self.fake_cloud()
        _git(self.repo, "remote", "set-url", "origin", os.path.join(self.dir, "no-such-remote.git"))
        p = self.propose()
        self.assertEqual(fake.calls, [], "no cloud session without master on GitHub")
        self.assertEqual((p["where"], p["run_id"]), ("this PC", 7))
        self.assertIn("GitHub", p["cloud_fallback"])

    def test_nothing_arriving_in_time_is_run_failed_and_can_be_run_locally(self):
        self.fake_cloud()
        p = self.propose()
        out = selfimprove.check_cloud(p["id"], repo=self.repo, now=p["cloud"]["deadline"] + 1)
        self.assertEqual(out["status"], "run_failed")
        self.assertIn("never arrived", out["detail"])
        self.assertIn("this PC instead", out["detail"])
        self.assertEqual(self.submitted, [])
        selfimprove.run_locally(p["id"], self.ex, repo=self.repo)
        p = selfimprove.get(p["id"])
        self.assertEqual((p["status"], p["where"], p["run_id"]), ("working", "this PC", 7))
        self.assertTrue(os.path.isdir(p["worktree"]))

    def test_work_pushed_but_never_marked_done_is_checked_when_time_is_up(self):
        fake = self.fake_cloud()
        p = self.propose()
        fake.push_work({"app.py": "x = 2\n"}, "Half done.", done=False)
        self.assertEqual(selfimprove.check_cloud(p["id"], repo=self.repo, run_tests=lambda *a: (True, ""))["status"], "working")
        out = selfimprove.check_cloud(p["id"], repo=self.repo, now=p["cloud"]["deadline"] + 1, run_tests=lambda *a: (True, ""))
        self.assertEqual(out["status"], "ready_for_review")
        self.assertIn("never said it had finished", out["summary"])

    def test_the_watcher_polls_until_the_work_arrives(self):
        fake = self.fake_cloud()
        p = self.propose()
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                fake.push_work({"app.py": "x = 2\n"}, "Done.")
        with mock.patch.object(selfimprove, "_run_tests", lambda wt, files: (True, "OK")):
            selfimprove.watch_cloud(p["id"], repo=self.repo, sleep=sleep)
        self.assertEqual(selfimprove.get(p["id"])["status"], "ready_for_review")
        self.assertEqual(sleeps, [config.CLOUD_POLL_SECONDS] * 2)

    def test_setting_local_behaves_as_today(self):
        with mock.patch.object(config, "CODE_JOBS", "local"), \
                mock.patch.object(cloud, "start_session", side_effect=AssertionError("cloud must not be used")):
            p = selfimprove.propose("make x two", self.ex, repo=self.repo)      # background default: still synchronous
        self.assertEqual((p["status"], p["where"], p["run_id"]), ("working", "this PC", 7))
        self.assertTrue(os.path.isdir(p["worktree"]))
        self.assertNotIn("cloud", p)
        self.assertFalse(_git(self.origin, "branch", "--list", "master"), "nothing pushed")


class CloudBuildTests(CloudTmp):
    def setUp(self):
        super().setUp()
        self.project = os.path.join(self.dir, "Demo")
        _init(self.project, {"README.md": "demo\n"})
        _git(self.project, "remote", "add", "origin", self.origin)
        p = mock.patch.object(P, "discover", lambda: [{"name": "Demo", "path": self.project, "source": "test"}])
        p.start()
        self.patches.append(p)

    def approve(self):
        b = builds.start_build("demo", "Shopping list", "A tiny CLI shopping list.")
        return b, builds.approve_document("demo", b["spec"], self.ex, background=False)

    def test_cloud_build_arrives_and_is_put_in_place(self):
        fake = self.fake_cloud()
        b, msg = self.approve()
        self.assertIn("cloud", msg)
        self.assertEqual(self.submitted, [])
        self.assertIn(b["spec"], _git(self.origin, "ls-tree", "-r", "--name-only", "master"), "the approved spec went to GitHub")
        self.assertIn(f"claude/jarvis-build-{b['id']}", fake.calls[0][0])
        fake.push_work({b["plan"]: "# Plan\n- [x] one\n- [ ] two\n", "shop.py": "print('list')\n"}, "Built the list.")
        out = builds.check_cloud(b["id"])
        self.assertEqual(out["status"], "finished")
        self.assertTrue(out["cloud"]["merged"])
        self.assertTrue(os.path.exists(os.path.join(self.project, "shop.py")))
        self.assertIn("1 of 2 plan tasks done", builds.build_status("demo"))
        self.assertIn("in the cloud", builds.build_status("demo"))

    def test_cloud_failure_runs_the_build_on_this_pc(self):
        self.fake_cloud(code=1, output="Cloud sessions are unavailable right now.")
        b, _ = self.approve()
        self.assertEqual(len(self.submitted), 1)
        (_, cwd, brief), kw = self.submitted[0]
        self.assertEqual((cwd, kw["kind"]), (self.project, "build"))
        b = builds.builds_list()[0]
        self.assertEqual((b["status"], b["where"], b["run_ids"]), ("building", "this PC", [7]))

    def test_cloud_build_that_never_arrives_can_be_run_locally(self):
        self.fake_cloud()
        b, _ = self.approve()
        out = builds.check_cloud(b["id"], now=builds.builds_list()[0]["cloud"]["deadline"] + 1)
        self.assertTrue(out["status"].startswith("stopped"))
        builds.run_locally(b["id"], self.ex)
        self.assertEqual(len(self.submitted), 1)
        self.assertEqual(builds.builds_list()[0]["where"], "this PC")


class CloudCliTests(unittest.TestCase):
    def ok_git(self, *a, **k):
        return 0, ""

    def test_session_found_even_when_the_cli_is_still_attached_at_the_timeout(self):
        info, why = cloud.start_session(".", "brief", cli=lambda b, c: (None, SESSION_OUT), git=self.ok_git)
        self.assertIsNone(why)
        self.assertEqual(info["session_id"], "session_01AbCdEfGhIj")

    def test_timeout_without_a_session_is_a_fallback(self):
        info, why = cloud.start_session(".", "brief", cli=lambda b, c: (None, ""), git=self.ok_git)
        self.assertIsNone(info)
        self.assertIn("in time", why)

    def test_mode_setting(self):
        for value, expected in (("local", "local"), ("LOCAL", "local"), ("cloud", "cloud"), ("", "cloud")):
            with mock.patch.object(config, "CODE_JOBS", value):
                self.assertEqual(cloud.mode(), expected)

    def test_the_cli_call_is_claude_cloud_with_the_brief(self):
        with mock.patch.object(cloud.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=SESSION_OUT, stderr="")) as run, \
                mock.patch.object(config, "find_claude_exe", lambda: "claude.exe"):
            self.assertEqual(cloud.cloud_cli("do it", "C:/x"), (0, SESSION_OUT))
        cmd = run.call_args[0][0]
        self.assertEqual(cmd, ["claude.exe", "--cloud", "do it"])
        self.assertFalse(any(k.startswith("ANTHROPIC_") for k in run.call_args[1]["env"]))


if __name__ == "__main__":
    unittest.main()
