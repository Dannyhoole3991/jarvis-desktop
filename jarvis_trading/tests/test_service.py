import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import service as V


class ChatTests(unittest.TestCase):
    def test_real_money_and_live_requests_are_refused(self):
        for phrase in ("start live trading", "trade with real money", "go live with the bot", "connect my binance api key",
                       "use leverage on the strategy"):
            self.assertEqual(V.chat_reply(phrase), V.REFUSAL, phrase)

    def test_commands_to_go_live_are_still_refused(self):
        for phrase in ("go live now", "switch to live trading", "start live trading with the best strategy"):
            self.assertEqual(V.chat_reply(phrase), V.REFUSAL, phrase)

    def test_typed_approval_phrases_go_to_the_approval_layer_and_nothing_else_does(self):
        with mock.patch.object(V, "typed_control", return_value="done") as m:
            for phrase in ("PROMOTE S-0001 v1 REVIEW 12", "ENABLE LIVE TRADING S-0001 v1 WITH 12 USDT", "ROLLBACK TO S-0001 v1",
                           "retire live strategy", "clear emergency stop", "CLEAR RISK HALT"):
                self.assertEqual(V.chat_reply(phrase), "done", phrase)
            self.assertEqual(m.call_count, 6)
            m.reset_mock()
            for phrase in ("please promote S-0001", "enable live trading", "approve everything", "promote S-0001 v1"):
                V.chat_reply(phrase)
            m.assert_not_called()   # vague requests never reach the approval layer

    def test_typed_control_reports_the_required_phrase_on_a_wrong_confirmation(self):
        import tempfile
        from jarvis_trading import registry as R
        with tempfile.TemporaryDirectory() as tmp:
            real = R.Registry
            with mock.patch.object(V.R, "Registry", lambda: real(os.path.join(tmp, "x.db"))):
                reply = V.typed_control("PROMOTE S-0001 v1 REVIEW 5")
        self.assertIn("review", reply.lower())

    def test_emergency_stop_phrase_triggers_the_stop_without_confirmation(self):
        with mock.patch.object(V, "emergency_stop_request", return_value="STOPPING") as m:
            self.assertEqual(V.chat_reply("emergency stop"), "STOPPING")
            self.assertEqual(V.chat_reply("kill switch now"), "STOPPING")
            self.assertEqual(m.call_count, 2)

    def test_disarm_needs_no_confirmation(self):
        with mock.patch.object(V, "disarm_request", return_value="DISARMED"):
            self.assertEqual(V.chat_reply("disarm live trading"), "DISARMED")

    def test_explain_questions_use_the_status_explainer(self):
        with mock.patch.object(V.LSTATUS, "explain", return_value=["Line one.", "Line two."]):
            for phrase in ("what strategy is live", "have you traded today", "why no trade today", "which challenger is strongest"):
                self.assertEqual(V.chat_reply(phrase), "Line one. Line two.", phrase)

    def test_start_live_trader_phrase(self):
        with mock.patch.object(V, "start_live_trader", return_value=(True, "Starting.")):
            self.assertEqual(V.chat_reply("start the live trader"), "Starting.")

    def _revive(self, running_trials, daemon_up=False, stop=False):
        import tempfile
        from jarvis_trading import registry as R
        with tempfile.TemporaryDirectory() as tmp:
            reg = R.Registry(os.path.join(tmp, "x.db"))
            real = R.Registry
            for i in range(running_trials):
                reg.create_forward_trial("S", f"S-{i}", 1, "h", "s", {}, 1.0, {}, "f", {})
                reg.log_forward(i + 1, "status", {"status": "RUNNING"})
            flag = os.path.join(tmp, "STOP")
            if stop:
                open(flag, "w").close()
            with mock.patch.object(V.R, "Registry", lambda: real(os.path.join(tmp, "x.db"))), mock.patch.object(V, "research_running", return_value=daemon_up),                     mock.patch.object(V, "STOP_FLAG", flag), mock.patch.object(V, "start_research", return_value=(True, "started")) as start:
                result = V.revive_background_services(delay=0)
            return result, start.called

    def test_engine_start_revives_the_daemon_only_when_trials_are_running_and_it_is_down(self):
        self.assertEqual(self._revive(2), ("started", True))
        self.assertEqual(self._revive(0), ("no forward trials running; nothing to revive", False))
        self.assertEqual(self._revive(2, daemon_up=True), ("research daemon already running", False))
        self.assertEqual(self._revive(2, stop=True), ("a stop was requested; not reviving", False))

    def test_unrelated_commands_pass_through(self):
        for phrase in ("open fiverr", "what time is it", "check my messages", "run a business cycle", "when will my fiverr gig go live on the site"):
            self.assertIsNone(V.chat_reply(phrase), phrase)

    def test_status_phrases_return_summary(self):
        with mock.patch.object(V, "summary", return_value={"text": "STATUS", "running": False}),                 mock.patch.object(V, "_leaderboard_text", return_value="BOARD"):
            self.assertEqual(V.chat_reply("trading lab status"), "STATUS BOARD")
            self.assertEqual(V.chat_reply("how's the trading"), "STATUS BOARD")

    def test_run_phrase_starts_research(self):
        with mock.patch.object(V, "start_research", return_value=(True, "Research daemon started.")) as m:
            self.assertIn("started", V.chat_reply("run trading research"))
            m.assert_called_once()

    def test_stop_phrase_sets_the_stop_flag_not_a_kill(self):
        with mock.patch.object(V, "stop_research", return_value="Stop requested.") as m:
            self.assertEqual(V.chat_reply("stop the trading research"), "Stop requested.")
            m.assert_called_once()

    def test_readiness_and_leaderboard_phrases(self):
        with mock.patch.object(V, "_readiness_text", return_value="READY?"), mock.patch.object(V, "_leaderboard_text", return_value="BOARD"):
            self.assertEqual(V.chat_reply("are we ready to go live"), "READY?")
            self.assertEqual(V.chat_reply("show the trading leaderboard"), "BOARD")

    def test_start_research_refuses_unknown_templates_and_missing_env(self):
        with mock.patch.object(V.A, "FT_VENV_PY", "Z:/nope/python.exe"), mock.patch.object(V, "research_running", return_value=False):
            ok, message = V.start_research(["sma_cross"])
        self.assertFalse(ok)
        self.assertIn("isn't installed", message)


if __name__ == "__main__":
    unittest.main()
