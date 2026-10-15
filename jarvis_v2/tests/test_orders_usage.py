"""
Store tab orders (read from Shopify admin text) and the free Usage tab refresh.

    py -3 -m unittest jarvis_v2.tests.test_orders_usage -v
"""

import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

import jarvis_money as M
from jarvis_v2 import server, usage_check as U

ORDERS_PAGE = ("Select all 1 on page\nOrder\nFlags\nDate\nCustomer\nFulfill by\nChannel\nTotal\nPayment status\n"
               "Fulfillment status\nItems\nDelivery status\nDelivery method\nTags\nSelect gid://shopify/Order/7991019995464\n"
               "#1001\nYesterday at 10:23\xa0pm\nJoanne Hoole\nOnline Store\n£18.98\nPaid\nIn progress\n2 items\nStandard\n\n"
               "Learn more about orders")
PAYOUTS_PAGE = ("Settings\nPending\n£18.35\nNo payouts yet\nPending\nTotal\n£18.35\nPending amounts may change\n"
                "Showing 1 transaction\nAll\nToday\nDate\nType\nOrder\nPayout date\nCharge\nFee\nNet\n"
                "Oct 14, 2026\nCharge\n#1001\nOct 23, 2026\n£18.98\n-£0.63\n£18.35 GBP")
USAGE_TEXT = ("You are currently using your subscription to power your Claude Code usage\n\n"
              "Current session: 6% used · resets Oct 15, 12pm (Europe/London)\n"
              "Current week (all models): 24% used · resets Oct 17, 7pm (Europe/London)\n"
              "Current week (Sonnet only): 100% used · resets Oct 17, 7:30pm (Europe/London)\n")


class OrdersTests(unittest.TestCase):
    def test_orders_list(self):
        self.assertEqual(M.parse_orders(ORDERS_PAGE), [{
            "number": "#1001", "id": "7991019995464", "date": "Yesterday at 10:23 pm", "customer": "Joanne Hoole", "total": "£18.98",
            "payment": "Paid", "fulfilment": "In progress", "items": "2 items"}])
        self.assertEqual(M.parse_orders("Your orders will show here"), [])

    def test_payouts(self):
        p = M.parse_payouts(PAYOUTS_PAGE)
        self.assertEqual(p["pending"], "£18.35")
        self.assertEqual(p["transactions"], [{"date": "Oct 14, 2026", "type": "Charge", "order": "#1001",
                                              "payout_date": "Oct 23, 2026", "charge": "£18.98", "fee": "-£0.63", "net": "£18.35"}])

    def test_money_check_summary(self):
        with mock.patch.object(M, "_read", return_value=("https://admin.shopify.com/store/x/orders", ORDERS_PAGE)):
            r = M.shopify_check()
        self.assertTrue(r["ok"])
        self.assertIn("£18.98 paid", r["summary"])
        self.assertIn("still to ship: #1001", r["summary"])

    def test_store_view_starts_one_background_read(self):
        state = server.State(router=mock.Mock(), executor=mock.Mock())
        with mock.patch.object(server, "_cached", return_value={"products": [], "collections": []}), \
             mock.patch("jarvis_money.shopify_orders", return_value={"ok": True, "orders": [], "payouts": {}}) as read:
            server.store_view(state)
            for _ in range(50):
                if state.orders:
                    break
                import time; time.sleep(0.05)
            self.assertEqual(read.call_count, 1)
            self.assertTrue(state.orders["ok"])
            server.store_view(state)               # fresh reading: no second browser trip
            self.assertEqual(read.call_count, 1)

    def test_orders_wait_while_tiktok_uses_the_browser(self):
        state = server.State(router=mock.Mock(), executor=mock.Mock())
        state.posting_now = 3
        self.assertFalse(server._refresh_orders(state))


class UsageTests(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 15, 7, 0)

    def test_parse_usage(self):
        w = U.parse(USAGE_TEXT, now=self.NOW)
        self.assertEqual(set(w), {"five_hour", "seven_day", "seven_day_sonnet"})
        self.assertAlmostEqual(w["five_hour"]["utilization"], 0.06)
        self.assertEqual(datetime.datetime.fromtimestamp(w["five_hour"]["resets_at"]), datetime.datetime(2026, 10, 15, 12, 0))
        self.assertEqual(datetime.datetime.fromtimestamp(w["seven_day_sonnet"]["resets_at"]), datetime.datetime(2026, 10, 17, 19, 30))
        self.assertEqual(w["seven_day_sonnet"]["status"], "rejected")

    def test_reset_without_a_date_is_the_next_time_it_comes_round(self):
        self.assertEqual(datetime.datetime.fromtimestamp(U.parse_reset("3am", now=self.NOW)), datetime.datetime(2026, 10, 16, 3, 0))

    def test_save_overwrites_stale_readings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.json")
            with open(path, "w") as f:
                json.dump({"windows": {"five_hour": {"utilization": 1, "status": "rejected"}}, "status": "rejected"}, f)
            U.save(U.parse(USAGE_TEXT, now=self.NOW), path)
            saved = json.load(open(path))
        self.assertAlmostEqual(saved["windows"]["five_hour"]["utilization"], 0.06)
        self.assertEqual(saved["source"], "claude /usage")

    def test_read_uses_the_free_slash_command(self):
        run = mock.Mock(return_value=mock.Mock(stdout=json.dumps({"result": USAGE_TEXT})))
        self.assertIn("seven_day", U.read(run=run))
        self.assertEqual(run.call_args[0][0][1:3], ["-p", "/usage"])


if __name__ == "__main__":
    unittest.main()


class PhoneSyncTests(unittest.TestCase):
    def test_push_sends_every_section_with_the_secret(self):
        from jarvis_v2 import phone_sync
        state = server.State(router=mock.Mock(), executor=mock.Mock())
        sections = {"/api/business": {"revenue": 0}, "/api/trading": {"mode": "paper"},
                    "/api/advertising": {"tiktok": {}}, "/api/posts": {"posts": []}}
        post = mock.Mock(return_value=mock.Mock(status_code=200))
        with mock.patch.dict(os.environ, {"JARVIS_PHONE_BACKEND_URL": "https://phone.example/", "JARVIS_PHONE_SECRET": "s"}), \
             mock.patch.object(server, "api_state", return_value={"usage": {}}), \
             mock.patch.object(server, "api_panel", return_value={"text": "Pay CJ", "at": 1}), \
             mock.patch.object(server, "store_view", return_value={"products": [{"title": "Bow", "image": "x", "secret": 1}]}), \
             mock.patch.object(server, "api_get", side_effect=lambda st, path, q: sections[path]):
            self.assertTrue(phone_sync.push(state, post=post))
        url = post.call_args[0][0]
        import base64, gzip
        body = json.loads(gzip.decompress(base64.b64decode(json.loads(post.call_args[1]["data"])["gz"])))
        self.assertEqual(url, "https://phone.example/api/dashboard_push")
        self.assertEqual(post.call_args[1]["headers"]["Authorization"], "Bearer s")
        self.assertEqual(body["needs_you"]["text"], "Pay CJ")
        self.assertEqual(body["store"]["products"], [{"title": "Bow", "url": None, "price_min": None, "price_max": None,
                                                      "christmas": None, "image": "x"}])
        self.assertEqual(body["trading"], {"mode": "paper"})

    def test_no_backend_configured_means_no_push(self):
        from jarvis_v2 import phone_sync
        with mock.patch.dict(os.environ, {"JARVIS_PHONE_BACKEND_URL": "", "JARVIS_PHONE_SECRET": ""}):
            self.assertFalse(phone_sync.push(mock.Mock(), post=mock.Mock()))


class BrowserLockTests(unittest.TestCase):
    def test_a_second_program_waits_for_the_first(self):
        import subprocess, sys, textwrap
        import jarvis_browser as jb
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "b.lock")
            holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
                import time, jarvis_browser as jb
                lock = jb._CrossProcessLock({path!r}); lock.acquire(5); print("held", flush=True); time.sleep(3); lock.release()
            """)], stdout=subprocess.PIPE, text=True, cwd=os.path.dirname(jb.__file__))
            self.assertEqual(holder.stdout.readline().strip(), "held")
            mine = jb._CrossProcessLock(path)
            with self.assertRaises(jb.BrowserBusy):
                mine.acquire(0.5)                    # still held by the other program
            mine.acquire(10)                         # free once it lets go
            mine.acquire(1)                          # re-entrant in this program
            mine.release(); mine.release()
            holder.wait(10)


class SingleWindowTests(unittest.TestCase):
    def test_a_second_server_cannot_share_the_port(self):
        st = server.State(router=mock.Mock(), executor=mock.Mock())
        first = server._Server(("127.0.0.1", 0), server.make_handler(st))
        try:
            with self.assertRaises(OSError):
                server._Server(("127.0.0.1", first.server_address[1]), server.make_handler(st))
        finally:
            first.server_close()

    def test_closing_window_only_switches_off_its_own_brain(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_file, flag = os.path.join(tmp, "token"), os.path.join(tmp, "flag")
            with mock.patch.object(server, "TOKEN_FILE", token_file), mock.patch.object(server, "ENGINE_FLAG_FILE", flag):
                server.set_engine_brain(True)
                with open(token_file, "w") as f:
                    f.write("the-other-window")
                mine = mock.Mock(token="mine", watcher=None, router=mock.Mock(brains={}, quick=None))
                server.stop(mock.Mock(), mine)
                self.assertEqual(open(flag).read(), "1", "another window's brain stays on")
                with open(token_file, "w") as f:
                    f.write("mine")
                server.stop(mock.Mock(), mine)
                self.assertEqual(open(flag).read(), "0")


class OpenWebsiteTests(unittest.TestCase):
    def test_opens_and_confirms_a_new_window(self):
        from jarvis_v2 import tools
        titles = [["Inbox - Personal - Microsoft Edge"]]
        opened = []
        def launch(u):
            opened.append(u)
            titles.append(["Inbox - Personal - Microsoft Edge", "Account Settings | Redbubble - Personal - Microsoft Edge"])
        out = tools.open_website("https://www.redbubble.com/account/settings", launch=launch,
                                 windows=lambda: titles[-1], sleep=lambda s: None)
        self.assertEqual(opened, ["https://www.redbubble.com/account/settings"])
        self.assertIn("Opened www.redbubble.com", out)

    def test_an_old_tab_is_not_taken_as_proof(self):
        from jarvis_v2 import tools
        same = ["Account Settings | Redbubble - Personal - Microsoft Edge"]
        out = tools.open_website("https://www.redbubble.com/account/settings", launch=lambda u: None,
                                 windows=lambda: same, sleep=lambda s: None)
        self.assertIn("couldn't confirm", out)

    def test_only_web_addresses(self):
        from jarvis_v2 import tools
        for bad in ("redbubble settings page", "file:///C:/Windows", "javascript:alert(1)", "https://a b.com"):
            self.assertIn("full web address", tools.open_website(bad, launch=lambda u: self.fail("launched"), windows=lambda: [], sleep=lambda s: None))
