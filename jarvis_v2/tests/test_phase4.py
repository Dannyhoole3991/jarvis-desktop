"""
Phase 4 tests: watching sessions, alerts, the screen. Temp folders and fakes only.

    py -3 -m unittest jarvis_v2.tests.test_phase4 -v
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from jarvis_v2 import alerts, config, screen, sessions as S


class PidTests(unittest.TestCase):
    def test_checking_a_process_never_kills_it(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(S.pid_alive(p.pid))
            self.assertTrue(S.pid_alive(p.pid))
            self.assertIsNone(p.poll(), "the check must not have terminated it")
        finally:
            p.kill()
            p.wait()
        self.assertFalse(S.pid_alive(p.pid))
        for bad in (0, -1, None, "x"):
            self.assertFalse(S.pid_alive(bad))
        self.assertTrue(S.pid_alive(os.getpid()))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.roster_dir = os.path.join(self.tmp.name, "sessions")
        self.projects_dir = os.path.join(self.tmp.name, "projects")
        os.makedirs(self.roster_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, pid, sid, cwd, name="", transcript=None, **extra):
        entry = {"pid": pid, "sessionId": sid, "cwd": cwd, "name": name, "updatedAt": int(time.time() * 1000), **extra}
        with open(os.path.join(self.roster_dir, f"{pid}.json"), "w") as f:
            json.dump(entry, f)
        with open(os.path.join(self.roster_dir, f"{pid}.secret.key"), "w") as f:
            f.write("PRIVATE")                      # must never be read or parsed
        if transcript is not None:
            d = os.path.join(self.projects_dir, S.encode_cwd(cwd))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{sid}.jsonl"), "w") as f:
                for obj in transcript:
                    f.write(json.dumps(obj) + "\n")

    def snap(self, alive=lambda pid: pid < 900):
        return S.snapshot(self.roster_dir, self.projects_dir, alive=alive)

    def test_states(self):
        talk = [{"type": "user", "message": {"role": "user", "content": "fix the blog footer"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done, I fixed it."}]}}]
        ask = talk[:1] + [{"type": "assistant", "message": {"content": [{"type": "text", "text": "Which colour should the footer be?"}]}}]
        url = talk[:1] + [{"type": "assistant", "message": {"content": [{"type": "text", "text": "Limit hit, see https://x.com/usage?from=cli"}]}}]
        self.add(101, "s-busy", r"C:\p\blog", "Blog fix", talk, status="busy")
        self.add(102, "s-perm", r"C:\p\shop", "Shop", talk, status="waiting", waitingFor="permission prompt")
        self.add(103, "s-ask", r"C:\p\app", "App", ask, status="idle")
        self.add(104, "s-idle", r"C:\p\done", "Done", talk, status="idle")
        self.add(105, "s-url", r"C:\p\url", "Url", url, status="idle")
        self.add(999, "s-dead", r"C:\p\old", "Old", talk, status="idle")
        self.add(106, "s-fresh", r"C:\p\new", "New", None, status="idle")
        self.add(107, "s-brain", config.HOME_DIR, "brain", talk, status="busy")
        by = {s["session_id"]: s for s in self.snap()}
        self.assertEqual(by["s-busy"]["state"], "working")
        self.assertEqual((by["s-perm"]["state"], by["s-perm"]["reason"], by["s-perm"]["human_hand"]), ("needs_you", "permission prompt", True))
        self.assertEqual(by["s-ask"]["state"], "needs_you")
        self.assertEqual(by["s-idle"]["state"], "idle")
        self.assertEqual(by["s-url"]["state"], "idle", "a '?' inside a link is not a question")
        self.assertEqual(by["s-dead"]["state"], "gone")
        self.assertEqual(by["s-fresh"]["state"], "fresh")
        self.assertNotIn("s-brain", by, "Jarvis's own brain is not one of Danny's sessions")
        self.assertEqual(by["s-busy"]["last_user"], "fix the blog footer")
        self.assertEqual(self.snap()[0]["state"], "needs_you", "needs-you sessions come first")
        self.assertIn("needs you: permission prompt", S.describe(self.snap()))
        self.assertEqual(S.find("blog fix", self.snap())["session_id"], "s-busy")
        self.assertEqual(S.find("shop", self.snap())["session_id"], "s-perm")

    def test_tool_results_are_not_mistaken_for_danny(self):
        t = [{"type": "user", "message": {"role": "user", "content": "real question"}},
             {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "file text"}]}}]
        self.add(110, "s1", r"C:\p\x", "X", t, status="idle")
        self.assertEqual(self.snap()[0]["last_user"], "real question")

    def test_half_written_files_are_tolerated(self):
        with open(os.path.join(self.roster_dir, "200.json"), "w") as f:
            f.write('{"pid": 200, "sessionId": ')
        self.assertEqual(self.snap(), [])

    def test_posting_to_a_missing_pipe_is_not_live(self):
        self.assertEqual(S.post_message(r"\\.\pipe\definitely-not-here-jarvis-test", "hi"), S.NOT_LIVE)
        self.assertEqual(S.post_message(None, "hi"), S.NOT_LIVE)
        self.assertEqual(S.post_message("x", "  "), S.REFUSED)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class AnnouncerTests(unittest.TestCase):
    def make(self, busy=False, speak_fails=False):
        self.spoken, self.notes, self.clock = [], [], Clock()

        def speak(text):
            if speak_fails:
                raise OSError("engine down")
            self.spoken.append(text)
        return alerts.Announcer(speak=speak, notify=lambda t, b: self.notes.append(b), busy=lambda: busy, clock=self.clock)

    def test_urgent_speaks_now_and_notifies_only_when_hidden(self):
        a = self.make()
        a.urgent("Session X needs you.")
        self.assertEqual((self.spoken, self.notes), (["Session X needs you."], []))
        a.window_visible = False
        a.urgent("Another.")
        self.assertEqual(self.notes, ["Another."])

    def test_urgent_falls_back_to_a_notification_when_the_engine_is_down(self):
        a = self.make(speak_fails=True)
        a.urgent("Needs you.")
        self.assertEqual(self.notes, ["Needs you."])

    def test_routine_news_waits_for_quiet_and_is_batched(self):
        a = self.make()
        a.urgent("first")
        a.routine("the blog job is done")
        a.routine("'Shop' has finished")
        self.assertIsNone(a.flush())                      # just spoke: not quiet yet
        self.clock.t += alerts.QUIET_SECONDS + 1
        text = a.flush()
        self.assertEqual(text, "While you were busy: the blog job is done; 'Shop' has finished.")
        self.assertIsNone(a.flush())

    def test_routine_waits_while_jarvis_is_talking_or_thinking(self):
        a = self.make(busy=True)
        a.routine("done")
        self.clock.t += 100
        self.assertIsNone(a.flush())
        a.busy = lambda: False
        a.thinking = lambda: True
        self.assertIsNone(a.flush())
        a.thinking = lambda: False
        self.assertEqual(a.flush(), "done")


class WatcherTests(unittest.TestCase):
    def test_announces_changes_once_and_never_old_news(self):
        spoken = []
        clock = Clock()
        ann = alerts.Announcer(speak=spoken.append, notify=lambda *a: None, busy=lambda: False, clock=clock)
        frames = [
            [{"session_id": "a", "name": "Blog", "project": "blog", "state": "needs_you", "reason": "permission prompt", "human_hand": True}],
            [{"session_id": "a", "name": "Blog", "project": "blog", "state": "needs_you", "reason": "permission prompt", "human_hand": True},
             {"session_id": "b", "name": "Shop", "project": "shop", "state": "working", "reason": None, "human_hand": False}],
            [{"session_id": "a", "name": "Blog", "project": "blog", "state": "working", "reason": None, "human_hand": False},
             {"session_id": "b", "name": "Shop", "project": "shop", "state": "needs_you", "reason": "it asked you a question", "human_hand": False}],
            [{"session_id": "a", "name": "Blog", "project": "blog", "state": "idle", "reason": None, "human_hand": False},
             {"session_id": "b", "name": "Shop", "project": "shop", "state": "needs_you", "reason": "it asked you a question", "human_hand": False}],
        ]
        props = [[], [], [{"id": 1, "goal": "fix x", "status": "working"}], [{"id": 1, "goal": "fix x", "status": "ready_for_review"}]]
        w = alerts.Watcher(ann, snapshot=lambda: frames.pop(0), proposals=lambda: props.pop(0))
        w.tick()
        self.assertEqual(spoken, [], "something that already needed you at startup isn't announced as new")
        w.tick()
        self.assertEqual(spoken, [])
        w.tick()
        self.assertEqual(len(spoken), 1)
        self.assertIn("'Shop' in shop needs you: it asked you a question", spoken[0])
        w.tick()
        self.assertIn("ready for you to review", spoken[1])
        self.assertEqual(len(spoken), 2, "routine news waits for a quiet moment after an urgent announcement")
        clock.t += alerts.QUIET_SECONDS + 1
        ann.flush()
        self.assertIn("'Blog' in blog has finished", spoken[2])

    def test_run_listener(self):
        ann = alerts.Announcer(speak=lambda t: None, notify=lambda *a: None, busy=lambda: True)
        listen = alerts.on_run_change(ann)
        listen(1, {"status": "succeeded", "kind": "run", "project": "blog", "prompt": "fix footer", "meta": {}})
        self.assertEqual(ann.pending, ["the job in blog ('fix footer') is done"])
        listen(2, {"status": "failed", "kind": "run", "project": "blog", "prompt": "x", "meta": {}})
        self.assertIn("stopped with a problem", ann.history[-1]["text"])
        n = len(ann.history)
        listen(3, {"status": "failed", "kind": "self_improve", "project": "Jarvis (copy)", "prompt": "x", "meta": {}})
        self.assertEqual(len(ann.history), n, "self-improvement outcomes are announced by the proposal check instead")


class ScreenTests(unittest.TestCase):
    def test_lists_windows_without_a_picture(self):
        wins = screen.list_windows()
        self.assertIsInstance(wins, list)
        for w in wins[:5]:
            self.assertTrue({"title", "app", "foreground", "minimised"} <= set(w))
        self.assertIsInstance(screen.describe_windows(), str)


if __name__ == "__main__":
    unittest.main()
