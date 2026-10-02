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
