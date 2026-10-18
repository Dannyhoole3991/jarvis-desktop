"""Business tab: real earnings from the account checks + Shopify, and honest 'live vs only a file' labels."""
import json
import os
import tempfile
import unittest
from unittest import mock

from jarvis_v2 import config, server, tools


class ListedTest(unittest.TestCase):
    def test_only_published_or_listed_counts_as_live(self):
        self.assertTrue(server._listed({"notes": "Published live at https://jarvis-blog.pages.dev/x"}))
        self.assertTrue(server._listed({"notes": "Listed on Redbubble: https://www.redbubble.com/i/x"}))
        self.assertFalse(server._listed({"notes": "Print-ready design saved at C:/x.png."}))
        self.assertFalse(server._listed({"notes": "Gig listing ready at C:/gig.md."}))
        self.assertFalse(server._listed({"notes": None}))


class EarningsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patch = mock.patch.object(config, "DATA_DIR", self.tmp)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_failed_read_keeps_last_good_figures(self):
        tools._save_money_results([{"name": "Fiverr", "ok": True, "summary": "2 order(s): 1 active, 1 completed"}])
        tools._save_money_results([{"name": "Fiverr", "ok": False, "problem": "it's showing a human check"}])
        acc = tools.money_results()["accounts"]["Fiverr"]
        self.assertTrue(acc["ok"])
        self.assertIn("2 order", acc["summary"])
        self.assertEqual(acc["problem"], "it's showing a human check")

    def test_shop_sales_summed_from_orders(self):
        state = mock.Mock()
        state.panels = {"money": {"running": True}}
        state.orders = {"ok": True, "at": 1, "orders": [{"total": "£25.99"}, {"total": "£1,012.50"}], "payouts": {"pending": "£20.00"}}
        with mock.patch.object(tools, "money_results", return_value={"at": 9e12, "accounts": {}}):
            out = server._earnings(state)
        self.assertEqual(out["shop"]["orders"], 2)
        self.assertAlmostEqual(out["shop"]["total"], 1038.49)
        self.assertEqual(out["shop"]["pending_payout"], "£20.00")
        self.assertTrue(out["checking"])

    def test_stale_reading_starts_a_background_check(self):
        state = mock.Mock()
        state.panels = {}
        state.orders = None
        with mock.patch.object(tools, "money_results", return_value={}), \
             mock.patch.object(server, "_browser_busy", return_value=""), \
             mock.patch.object(server, "api_panel") as panel:
            server._earnings(state)
        panel.assert_called_once_with(state, "money", refresh=True)

    def test_busy_browser_waits(self):
        state = mock.Mock(); state.panels = {}; state.orders = None
        with mock.patch.object(tools, "money_results", return_value={}), \
             mock.patch.object(server, "_browser_busy", return_value="posting a TikTok video"), \
             mock.patch.object(server, "api_panel") as panel:
            server._earnings(state)
        panel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
