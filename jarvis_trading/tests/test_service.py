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

    def test_unrelated_commands_pass_through(self):
        for phrase in ("open fiverr", "what time is it", "check my messages", "run a business cycle"):
            self.assertIsNone(V.chat_reply(phrase), phrase)

    def test_status_phrases_return_summary(self):
        with mock.patch.object(V, "summary", return_value={"text": "STATUS", "running": False}):
            self.assertEqual(V.chat_reply("trading lab status"), "STATUS")
            self.assertEqual(V.chat_reply("how's the trading"), "STATUS")

    def test_run_phrase_starts_research(self):
        with mock.patch.object(V, "start_research", return_value=(True, "Research started on 6 template(s).")) as m:
            self.assertIn("Research started", V.chat_reply("run trading research"))
            m.assert_called_once()

    def test_start_research_refuses_unknown_templates_and_missing_env(self):
        with mock.patch.object(V.A, "FT_VENV_PY", "Z:/nope/python.exe"):
            ok, message = V.start_research(["sma_cross"])
        self.assertFalse(ok)
        self.assertIn("isn't installed", message)


if __name__ == "__main__":
    unittest.main()
