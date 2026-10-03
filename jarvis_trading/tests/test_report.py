import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import registry as R
from jarvis_trading import report as P
from jarvis_trading import strategies as S


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = R.Registry(os.path.join(self.tmp.name, "r.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_empty_lab_says_so_and_mentions_no_real_money(self):
        text = P.chat_summary(self.reg)
        self.assertIn("no strategies", text)
        self.assertIn("no real money", text)

    def test_rejected_strategy_shows_reason_and_never_claims_an_edge(self):
        name, source, params = S.generate("sma_cross")
        sid, ver, _ = self.reg.create_strategy("h", "template:sma_cross", "m", "1h", params, source)
        self.reg.set_status(sid, ver, "rejected", "REJECTED -- <script>x</script> poor")
        text = P.chat_summary(self.reg)
        self.assertIn("no edge has been found", text)
        self.assertIn("poor", text)
        page = P.render_html(P.snapshot(self.reg))
        self.assertNotIn("<script>x</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("No real money", page)

    def test_chat_summary_does_not_build_the_heavy_snapshot(self):
        # at ~1,600 strategy versions the full snapshot took ~10s and made the HUD time out; the chat path must stay cheap
        from unittest import mock
        name, source, params = S.generate("sma_cross")
        self.reg.create_strategy("h", "template:sma_cross", "m", "1h", params, source)
        with mock.patch.object(P, "snapshot", side_effect=AssertionError("snapshot must not be used")):
            self.assertIn("1 strategy versions tested", P.chat_summary(self.reg))

    def test_candidate_is_labelled_candidate_not_proven(self):
        name, source, params = S.generate("sma_cross")
        sid, ver, _ = self.reg.create_strategy("h", "template:sma_cross", "m", "1h", params, source)
        self.reg.set_status(sid, ver, "backtested", "a")
        self.reg.set_status(sid, ver, "candidate", "b")
        self.assertIn("Surviving", P.chat_summary(self.reg))
        self.assertIn("not a proven edge", P.render_html(P.snapshot(self.reg)))


if __name__ == "__main__":
    unittest.main()
