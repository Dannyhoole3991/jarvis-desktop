"""
Offline tests for Jarvis v2 (fake brains, fake Claude process, temp folders; nothing real is touched).

    py -3 -m unittest jarvis_v2.tests.test_v2 -v
"""

import io
import json
import os
import queue
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from jarvis_v2 import claude_env, config, mcp_server, memory, router as R, tools
from jarvis_v2.brains import BrainUnavailable
from jarvis_v2.brains import claude as CB
from jarvis_v2.brains.local import LocalBrain
from jarvis_v2.brains.openai_brain import OpenAIBrain


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeBrain:
    def __init__(self, name, script):
        self.name, self.script, self.calls = name, list(script), []

    def ask(self, text, history=None, preamble=""):
        self.calls.append({"text": text, "preamble": preamble, "history": list(history or [])})
        step = self.script.pop(0) if self.script else "ok"
        if isinstance(step, BrainUnavailable):
            raise step
        return step


class TempDataMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (config.DATA_DIR, config.MEMORY_DIR, config.BRAIN_STATE_FILE, config.USAGE_FILE, config.CLAUDE_CONTEXT_FILE)
        config.DATA_DIR = self.tmp.name
        config.MEMORY_DIR = os.path.join(self.tmp.name, "memory")
        config.BRAIN_STATE_FILE = os.path.join(self.tmp.name, "brain_state.json")
        config.USAGE_FILE = os.path.join(self.tmp.name, "usage.json")
        config.CLAUDE_CONTEXT_FILE = os.path.join(self.tmp.name, "claude_context.md")

    def tearDown(self):
        config.DATA_DIR, config.MEMORY_DIR, config.BRAIN_STATE_FILE, config.USAGE_FILE, config.CLAUDE_CONTEXT_FILE = self.old
        self.tmp.cleanup()


class RouterTests(TempDataMixin, unittest.TestCase):
    def make(self, claude, openai, local, clock):
        return R.Router([FakeBrain("claude", claude), FakeBrain("openai", openai), FakeBrain("local", local)], clock=clock,
                        state_path=config.BRAIN_STATE_FILE)

    def test_claude_answers_when_available(self):
        r = self.make(["hello sir"], [], [], Clock())
        reply = r.ask("hi")
        self.assertEqual((reply.brain, reply.text, reply.announcement), ("claude", "hello sir", ""))
        self.assertEqual(r.brains["openai"].calls, [])

    def test_usage_limit_switches_to_openai_then_back_to_claude_when_it_renews(self):
        clock = Clock()
        resets = clock.t + 3600
        r = self.make([BrainUnavailable("usage_limit", resets), "Claude here"], ["openai 1", "openai 2"], [], clock)
        first = r.ask("open steam")
        self.assertEqual(first.brain, "openai")
        self.assertIn("allowance is used up", first.announcement)
        second = r.ask("what's next")
        self.assertEqual(second.brain, "openai")
        self.assertEqual(len(r.brains["claude"].calls), 1, "a blocked Claude must not be called again before it renews")
        self.assertEqual(second.announcement, "")
        clock.t = resets + 1
        back = r.ask("and now?")
        self.assertEqual((back.brain, back.announcement), ("claude", "Claude is back, sir."))
        handover = r.brains["claude"].calls[-1]["preamble"]
        self.assertIn("open steam", handover)
        self.assertIn("openai 2", handover)
        self.assertEqual(r.away, [])

    def test_no_openai_credit_drops_to_local(self):
        clock = Clock()
        r = self.make([BrainUnavailable("usage_limit", clock.t + 60)], [BrainUnavailable("no_credit", clock.t + 1800)], ["local here"], clock)
        reply = r.ask("hi")
        self.assertEqual((reply.brain, reply.text), ("local", "local here"))
        self.assertIn("my local AI", reply.announcement)

    def test_everything_down_says_so_without_crashing(self):
        clock = Clock()
        r = self.make([BrainUnavailable("auth", clock.t + 60)], [BrainUnavailable("offline", clock.t + 60)],
                      [BrainUnavailable("offline", clock.t + 60)], clock)
        reply = r.ask("hi")
        self.assertEqual(reply.brain, "none")
        self.assertIn("None of my brains", reply.text)

    def test_blocks_survive_a_restart(self):
        clock = Clock()
        r = self.make([BrainUnavailable("usage_limit", clock.t + 600)], ["openai"], [], clock)
        r.ask("hi")
        r2 = self.make(["claude"], ["openai again"], [], clock)
        self.assertEqual(r2.ask("hi").brain, "openai")
        self.assertEqual(r2.brains["claude"].calls, [])

    def test_force_and_back_to_auto(self):
        clock = Clock()
        r = self.make(["claude"], [], ["local"], clock)
        r.force("local")
        self.assertEqual(r.ask("hi").brain, "local")
        r.force(None)
        self.assertEqual(r.ask("hi").brain, "claude")
        with self.assertRaises(ValueError):
            r.force("gemini")

    def test_status_text_mentions_block_and_usage(self):
        clock = Clock()
        r = self.make([BrainUnavailable("usage_limit", clock.t + 600)], ["x"], [], clock)
        r.ask("hi")
        CB.record_usage({"status": "rejected", "rateLimitType": "five_hour", "resetsAt": clock.t + 600, "utilization": 1.0})
        text = R.status_text(now=clock.t)
        self.assertIn("Active brain: OpenAI", text)
        self.assertIn("Claude is unavailable until", text)
        self.assertIn("five hour window: 100% used", text)


class FakeProc:
    """Stands in for the claude process: each user line written to stdin releases the next scripted turn."""

    def __init__(self, turns):
        self.turns = turns  # shared across restarts: a restarted process continues the script
        self.lines = queue.Queue()
        self.returncode = None
        self.stdin = self
        self.stdout = iter(self.lines.get, None)
        self.written = []
        self.lines.put(json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}))

    def write(self, data):
        self.written.append(json.loads(data))
        events = self.turns.pop(0) if self.turns else []
        for ev in events:
            if ev == "EOF":
                self.lines.put(None)
                self.returncode = 1
                return
            self.lines.put(json.dumps(ev))

    def flush(self):
        pass

    def close(self):
        self.lines.put(None)
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.returncode = -9


def _assistant(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


class ClaudeBrainTests(TempDataMixin, unittest.TestCase):
    def brain(self, turns, clock=None):
        procs = []
        turns = list(turns)

        def popen(cmd, **kw):
            p = FakeProc(turns)
            procs.append((cmd, kw, p))
            return p
        b = CB.ClaudeBrain(exe="claude.exe", popen=popen, clock=clock or Clock(), turn_timeout=5)
        return b, procs

    def test_normal_turn_returns_the_result_text(self):
        with mock.patch.object(CB.persona, "write_claude_home"), mock.patch.object(CB, "write_mcp_config", return_value="mcp.json"):
            b, procs = self.brain([[_assistant("Good evening"), {"type": "result", "result": "Good evening, sir.", "is_error": False}]])
            self.assertEqual(b.ask("hello"), "Good evening, sir.")
            cmd, kw, proc = procs[0]
            self.assertIn("--strict-mcp-config", cmd)
            self.assertEqual(proc.written[0]["message"]["content"], "hello")
            self.assertFalse(any(k.startswith("ANTHROPIC_") for k in kw["env"]))

    def test_rate_limit_rejection_raises_with_the_reset_time_and_records_usage(self):
        clock = Clock()
        resets = clock.t + 7200
        turn = [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": resets, "rateLimitType": "five_hour", "utilization": 1.0}},
                {"type": "result", "result": "Claude usage limit reached", "is_error": True}]
        with mock.patch.object(CB.persona, "write_claude_home"), mock.patch.object(CB, "write_mcp_config", return_value="mcp.json"):
            b, _ = self.brain([turn], clock)
            with self.assertRaises(BrainUnavailable) as ctx:
                b.ask("hi")
        self.assertEqual((ctx.exception.reason, ctx.exception.retry_at), ("usage_limit", resets))
        usage = json.load(open(config.USAGE_FILE, encoding="utf-8"))
        self.assertEqual(usage["windows"]["five_hour"]["status"], "rejected")

    def test_allowed_warning_is_not_a_block(self):
        turn = [{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning", "utilization": 0.9, "rateLimitType": "seven_day"}},
                {"type": "result", "result": "fine", "is_error": False}]
        with mock.patch.object(CB.persona, "write_claude_home"), mock.patch.object(CB, "write_mcp_config", return_value="mcp.json"):
            b, _ = self.brain([turn])
            self.assertEqual(b.ask("hi"), "fine")

    def test_expired_login_is_auth_not_usage(self):
        turn = [{"type": "result", "result": "Failed to authenticate: OAuth session expired and could not be refreshed", "is_error": True}]
        with mock.patch.object(CB.persona, "write_claude_home"), mock.patch.object(CB, "write_mcp_config", return_value="mcp.json"):
            b, _ = self.brain([turn])
            with self.assertRaises(BrainUnavailable) as ctx:
                b.ask("hi")
        self.assertEqual(ctx.exception.reason, "auth")

    def test_a_crashed_process_is_restarted_once(self):
        with mock.patch.object(CB.persona, "write_claude_home"), mock.patch.object(CB, "write_mcp_config", return_value="mcp.json"):
            b, procs = self.brain([["EOF"], [{"type": "result", "result": "recovered", "is_error": False}]])
            self.assertEqual(b.ask("hi"), "recovered")
            self.assertEqual(len(procs), 2)


class EnvTests(unittest.TestCase):
    def test_api_keys_and_session_identity_are_removed(self):
        env = claude_env.child_env({"ANTHROPIC_API_KEY": "x", "anthropic_base_url": "y", "CLAUDE_CODE_SSE_PORT": "1",
                                    "CLAUDECODE": "1", "PATH": "p", "OPENAI_API_KEY": "keep"})
        self.assertEqual(set(env) - {"PYTHONIOENCODING"}, {"PATH", "OPENAI_API_KEY"})


class McpTests(unittest.TestCase):
    def run_lines(self, *msgs):
        out = io.StringIO()
        mcp_server.main(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
        return [json.loads(l) for l in out.getvalue().splitlines()]

    def test_handshake_list_and_call(self):
        replies = self.run_lines({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                 {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope", "arguments": {}}})
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertIn("money_check", names)
        self.assertTrue(replies[2]["result"]["isError"])

    def test_a_tool_that_prints_does_not_corrupt_the_protocol(self):
        tools.TOOLS["_noisy"] = (lambda: print("chatter") or "clean", "test", tools._schema())
        try:
            replies = self.run_lines({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "_noisy", "arguments": {}}})
        finally:
            del tools.TOOLS["_noisy"]
        self.assertEqual(replies[0]["result"]["content"][0]["text"], "clean")


class ToolSafetyTests(unittest.TestCase):
    def test_no_dangerous_tool_is_exposed(self):
        for name in tools.TOOLS:
            self.assertIsNone(tools.FORBIDDEN_NAMES.search(name), name)

    def test_tools_never_raise(self):
        self.assertIn("wrong details", tools.call("remember", {"bogus": 1}))
        self.assertIn("no tool", tools.call("missing"))

    def test_read_page_refuses_non_web_addresses(self):
        self.assertIn("http", tools.read_page("file:///C:/secret.txt"))


class MemoryTests(TempDataMixin, unittest.TestCase):
    def test_remember_recall_and_index(self):
        memory.remember("Store name", "The Shopify store is called Collective.")
        memory.remember("Store name", "The Shopify store is called Collective (collectivefinds.co.uk).")
        self.assertIn("collectivefinds", memory.recall("store collective"))
        self.assertEqual(memory.index_text().count("(store-name.md)"), 1)
        memory.write_journal("Built phase 1.")
        self.assertIn("Built phase 1.", memory.recent_journal())


class FallbackBrainTests(TempDataMixin, unittest.TestCase):
    def test_openai_tool_loop_and_no_credit(self):
        call = SimpleNamespace(id="c1", function=SimpleNamespace(name="brain_status", arguments="{}"))
        responses = [SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]))]),
                     SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="On OpenAI, sir.", tool_calls=None))])]
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: responses.pop(0))))
        used = []
        b = OpenAIBrain(client=client, call_tool=lambda n, a: used.append(n) or "status")
        self.assertEqual(b.ask("which brain?"), "On OpenAI, sir.")
        self.assertEqual(used, ["brain_status"])

        def broke(**kw):
            raise Exception("Error code: 429 - insufficient_quota: You exceeded your current quota")
        b2 = OpenAIBrain(client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broke))))
        with self.assertRaises(BrainUnavailable) as ctx:
            b2.ask("hi")
        self.assertEqual(ctx.exception.reason, "no_credit")

    def test_local_tool_loop_and_offline(self):
        replies = [{"message": {"content": "", "tool_calls": [{"function": {"name": "recall", "arguments": {"query": "store"}}}]}},
                   {"message": {"content": "Local here."}}]
        used = []
        b = LocalBrain(post=lambda url, payload, timeout=300: replies.pop(0), call_tool=lambda n, a: used.append((n, a)) or "x")
        self.assertEqual(b.ask("hi"), "Local here.")
        self.assertEqual(used, [("recall", {"query": "store"})])

        def down(*a, **k):
            raise OSError("refused")
        with self.assertRaises(BrainUnavailable) as ctx:
            LocalBrain(post=down).ask("hi")
        self.assertEqual(ctx.exception.reason, "offline")


if __name__ == "__main__":
    unittest.main()


class QuickVoiceTests(TempDataMixin, unittest.TestCase):
    """Haiku answers small talk; real jobs go to the full brain, with a heads-up spoken at once."""

    def make(self, quick, claude, clock=None):
        r = R.Router([FakeBrain("claude", claude), FakeBrain("openai", ["openai"]), FakeBrain("local", ["local"])],
                     clock=clock or Clock(), state_path=config.BRAIN_STATE_FILE, quick=FakeBrain("quick", quick))
        r.spoken = []
        r.on_handoff = r.spoken.append
        return r

    def test_small_talk_is_answered_by_the_quick_voice(self):
        r = self.make(["Very well, sir."], [])
        reply = r.ask("how are you")
        self.assertEqual((reply.brain, reply.text), ("quick", "Very well, sir."))
        self.assertEqual(r.brains["claude"].calls, [])

    def test_obvious_jobs_skip_the_quick_voice(self):
        r = self.make([], ["No orders yet, sir."])
        self.assertEqual(r.ask("have we had any orders").brain, "claude")
        self.assertEqual(r.quick.calls, [])

    def test_handoff_speaks_the_heads_up_and_the_full_brain_does_it(self):
        r = self.make(["HANDOFF: On it, sir."], ["Done, sir."])
        reply = r.ask("what's the best thing to do next")
        self.assertEqual((reply.brain, reply.text), ("claude", "Done, sir."))
        self.assertEqual(r.spoken, ["On it, sir."])
        self.assertIn("already been told", r.brains["claude"].calls[0]["preamble"])

    def test_each_voice_hears_what_the_other_said(self):
        r = self.make(["Very well, sir.", "Glad to hear it."], ["No orders yet, sir."])
        r.ask("how are you")
        r.ask("any orders on the store")
        self.assertIn("Very well, sir.", r.brains["claude"].calls[0]["preamble"])
        r.ask("great thanks")
        self.assertIn("No orders yet, sir.", r.quick.calls[1]["preamble"])

    def test_quick_voice_out_of_allowance_blocks_claude_and_falls_back(self):
        clock = Clock()
        r = self.make([BrainUnavailable("usage_limit", clock.t + 600)], ["never"], clock)
        reply = r.ask("hello")
        self.assertEqual(reply.brain, "openai")
        self.assertEqual(r.brains["claude"].calls, [], "same allowance: don't try the full Claude brain either")
        self.assertEqual(r.ask("hello again").brain, "openai")
        self.assertEqual(len(r.quick.calls), 1, "no quick voice while Claude is blocked")

    def test_forced_brain_skips_the_quick_voice(self):
        r = self.make(["quick"], [])
        r.force("local")
        self.assertEqual(r.ask("hi").brain, "local")
        self.assertEqual(r.quick.calls, [])
