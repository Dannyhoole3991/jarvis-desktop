"""
Paying CJ for a shop order: matching, caps, one tap = one payment, and stopping for anything only Danny
can do. Everything runs against a fake CJ site and a fake Shopify page; nothing touches the internet.

    py -3 -m unittest jarvis_v2.tests.test_cjpay -v
"""

import contextlib
import json
import os
import tempfile
import time
import unittest

from jarvis_v2 import cjpay as C

SHOPIFY_PAGE = """Orders
Jane Doe
1 order
jane@example.com
Jane Doe
1 Test Street
Darfield
Barnsley
S70 1AA
United Kingdom
View map
PaidIn progress
cjdropshipping
Ceramic Christmas Tree Light - 17cm

Star Topper / 8X8X17CM

SKU
CJYS211498101AZ

Unit price
£9.49

×

2

Total
£18.98

Subtotal
2 items
£18.98
Total
£18.98
Paid
£18.98
"""

ROW = """#1001
CJ Order No. DP2610142124070665600
Ceramic Christmas Tree Light
CJYS211498101AZ
x {qty}
Product: $3.44
Shipping: $12.18
Total: ${total}
Jane Doe
1 Test Street, Darfield, Barnsley {postcode} United Kingdom
GBP 18.98{extra}"""

# CJ's real order-list row (layout seen live 2026-10-15; made-up customer): no SKUs, no postcode.
REAL_ROW = ("\t\n#1001\nDP2610142124070665600\n \n\t\nmwrfta-mh\n\t\nAwaiting Payment\n\t\nOct. 15, 2026 18:14:46\n\t\n"
            "Jane Doe\nGB\n{street}\n\t\n\nCJPacket Special line\n\n---\n\nDongguan, GD, CN\n\t{qty}\t${total}\nCOGS: \n62.38%\n")
PAY_URL = "https://www.cjdropshipping.com/mine/payment?pid=abc123"
PAY_PAGE = """Payment
Order Number: CJPA2610150001
Payable Amount: ${amount}
WorldFirst
Pay with CJ wallet
PayPal Checkout
Pay with Card
I have read and agree to the Terms of Service
PAY"""


class FakeCJ:
    """CJ's order list (tabs of rows), its payment page, and what pressing PAY does."""

    def __init__(self, qty=2, total="15.62", postcode="S70 1AA", extra="", pay_amount=None, tab="Awaiting Payment",
                 outcome="success", wallet="50.00", card_inputs=None, logged_in=True, wallet_page_says=None,
                 real_layout=False, street="1 Test Street", pay_button=True):
        self.tabs = {t: [] for t in C.UNPAID_TABS + C.PAID_TABS}
        self.row = (REAL_ROW.format(qty=qty, total=total, street=street) if real_layout
                    else ROW.format(qty=qty, total=total, postcode=postcode, extra=extra))
        self.pay_button = pay_button
        self.tabs[tab].append(self.row)
        self.pay_amount = pay_amount or total
        self.outcome, self.wallet, self.card_inputs, self.logged_in = outcome, float(wallet), card_inputs or [], logged_in
        self.wallet_page_says = wallet_page_says          # the payment page's own view of the wallet, if different
        self.method = None
        self.tab, self.url, self.presses, self.clicks, self.shots = None, "", 0, [], []
        self.method_chosen = self.terms = False

    @contextlib.contextmanager
    def hold(self):
        yield

    def _snap(self, text, **extra):
        url = self.url if "/mine/payment" in self.url or self.tab is None else f"{C.CJ_LIST_URL}?tab={self.tab}"
        return dict({"url": url, "title": "", "text": text, "inputs": [], "frames": [], "tabs": [self.url]}, **extra)

    def _pay_text(self):
        text = PAY_PAGE.format(amount=self.pay_amount)
        if not self.method_chosen:
            return text
        enough = self.wallet_page_says if self.wallet_page_says is not None else self.wallet >= float(self.pay_amount)
        return text + ("\nWallet balance $%.2f" % self.wallet if enough else "\n$%.2f Insufficient balance" % self.wallet)

    def open(self, url):
        if not self.logged_in:
            self.url = "https://www.cjdropshipping.com/login.html?target=x"
            return self._snap("Sign in")
        self.url, self.method_chosen, self.terms = url, False, False
        if url == C.WALLET_URL:
            return self._snap("My Wallet\nAccount Balance ($)\n%.2f\nRecharge" % self.wallet)
        return self._snap(self._pay_text() if "/mine/payment" in url else "My CJ\nOrders")

    def click_tab(self, name):
        if name not in self.tabs:
            return None
        self.tab = name
        return self._snap("\n".join(self.tabs[name]))

    def row_texts(self, number):
        return [r for r in self.tabs.get(self.tab, []) if C.order_re(number).search(r)]

    def click_row_button(self, number, label_re):
        import re
        self.clicks.append(label_re)
        rows = self.row_texts(number)
        if len(rows) != 1:
            return False, "couldn't find the order's row", self._snap("")
        if re.search(label_re, "Add to Orders Picking", re.I) and self.tab == "Imported":
            self.tabs["Imported"].remove(rows[0])
            self.tabs["Orders Picking"].append(rows[0])
            return True, "Add to Orders Picking", self._snap("")
        if self.pay_button and re.search(label_re, "Pay", re.I) and self.tab in ("Awaiting Payment", "Orders Picking"):
            self.url = PAY_URL
            return True, "Pay", self._snap(self._pay_text())
        return False, "no such button in the order's row", self._snap("")

    def choose_method(self, label):
        assert label == C.WALLET_LABEL, "Jarvis only ever chooses the CJ wallet"
        if "/mine/payment" not in self.url:
            return None
        self.method_chosen, self.method = True, label
        return self._snap(self._pay_text(), inputs=list(self.card_inputs))

    def tick_terms(self):
        self.terms = True
        return True, "ticked", self._snap(self._pay_text(), inputs=list(self.card_inputs))

    def press_pay(self):
        assert self.method_chosen and self.terms, "PAY pressed before choosing the card and ticking the terms"
        self.presses += 1
        return True, "PAY"

    def wait_result(self, tabs_before, seconds=40):
        if self.outcome == "success":
            self.wallet -= float(self.pay_amount)
            self._move("Pending")
            return self._snap("Payment successful")
        if self.outcome == "otp":
            return self._snap("Payment", frames=["https://acs.examplebank.com/3ds/challenge"])
        if self.outcome == "declined":
            return self._snap("Your card was declined")
        if self.outcome == "crash":
            raise RuntimeError("the browser went away")
        return self._snap(self._pay_text())               # "silent": nothing visibly changed

    def _move(self, to):
        for rows in self.tabs.values():
            if self.row in rows:
                rows.remove(self.row)
        self.tabs[to].append(self.row)

    def screenshot(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"png")
        self.shots.append(os.path.basename(path))
        return path


class Speaker:
    def __init__(self):
        self.said = []

    def urgent(self, text):
        self.said.append(text)


ORDER = {"number": "#1001", "id": "7991019995464", "payment": "Paid", "fulfilment": "In progress", "net_gbp": 18.35}


class PayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.limits_path = os.path.join(self.tmp.name, "limits.json")
        self.shopify_page = SHOPIFY_PAGE
        self.reads = 0

    def tearDown(self):
        self.tmp.cleanup()

    def payer(self, cj=None, **limits):
        if limits:
            with open(self.limits_path, "w") as f:
                json.dump(limits, f)
        self.cj = cj or FakeCJ()
        self.speaker = Speaker()

        def shopify(order_id):
            self.reads += 1
            return self.shopify_page
        return C.Payer(store=C.PayStore(os.path.join(self.tmp.name, "cj_pay.db")), driver=self.cj, shopify=shopify,
                       announcer=self.speaker, folder=os.path.join(self.tmp.name, "jobs"), limits_path=self.limits_path)

    def offered(self, payer):
        out = payer.prepare(dict(ORDER))
        self.assertTrue(out["ok"], out)
        return out["request"]

    def audit(self, r):
        with open(os.path.join(r["folder"], "audit.jsonl"), encoding="utf-8") as f:
            return [json.loads(line) for line in f]

    # ---------------------------------------------------------------- reading
    def test_reads_the_shopify_order(self):
        shop = C.parse_shopify_order(SHOPIFY_PAGE)
        self.assertEqual(shop["items"], [{"sku": "CJYS211498101AZ", "qty": 2, "title": "Ceramic Christmas Tree Light - 17cm",
                                          "variant": "Star Topper / 8X8X17CM"}])
        self.assertEqual((shop["name"], shop["town"], shop["postcode"], shop["paid_gbp"], shop["cancelled"]),
                         ("Jane Doe", "Barnsley", "S701AA", 18.98, False))

    def test_reads_the_cj_row_and_payment_page(self):
        row = C.parse_cj_row(ROW.format(qty=2, total="15.62", postcode="S70 1AA", extra=""))
        self.assertEqual((row["cj_orders"], row["skus"], row["total_usd"], row["postcode"]),
                         (["DP2610142124070665600"], {"CJYS211498101AZ": 2}, 15.62, "S701AA"))
        pay = C.parse_payment_page(PAY_PAGE.format(amount="15.62"))
        self.assertEqual(pay, {"orders": ["CJPA2610150001"], "amount_usd": 15.62})

    def test_card_numbers_are_never_written_down(self):
        self.assertEqual(C.redact("card 4111 1111 1111 1111 ok, order DP2610142124070665600"),
                         "card [number hidden] ok, order DP2610142124070665600")

    def test_wallet_balance(self):
        self.assertEqual(C.parse_wallet("Account Balance ($)\n20.00"), 20.0)
        self.assertEqual(C.parse_wallet("Account Balance ($): 1,250.5"), 1250.5)
        self.assertIsNone(C.parse_wallet("Sign in"))

    def test_the_cj_driver_cannot_type(self):
        for name in ("fill", "type", "type_text", "press_key", "paste", "keyboard"):
            self.assertFalse(hasattr(C.CJBrowser, name), name)

    def test_limits_never_go_above_the_card(self):
        with open(self.limits_path, "w") as f:
            json.dump({"max_per_order_usd": 500, "max_per_day_usd": 1000}, f)
        lim = C.limits(self.limits_path)
        self.assertEqual((lim["max_per_order_usd"], lim["max_per_day_usd"]), (C.ABSOLUTE_MAX_USD, C.ABSOLUTE_MAX_USD))
        self.assertEqual(C.limits(os.path.join(self.tmp.name, "none.json"))["max_per_order_usd"], 40.0)

    # ---------------------------------------------------------------- the happy path
    def test_offer_then_one_tap_pays_once(self):
        p = self.payer()
        r = self.offered(p)
        self.assertEqual(r["status"], "offered")
        self.assertIn("Pay CJ $15.62 for order #1001: 2 × Ceramic Christmas Tree Light - 17cm, to J Doe, Barnsley.", r["summary"])
        self.assertIn("You make about £5.85.", r["summary"])          # 18.35 - 15.62 * 0.80
        self.assertEqual(r["method"], "wallet")
        self.assertIn("tap Pay", self.speaker.said[-1])
        self.assertEqual(self.cj.presses, 0, "preparing never pays")

        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        done = p.store.get(r["id"])
        self.assertEqual((done["status"], done["decided_by"], self.cj.presses), ("paid", "window", 1))
        self.assertIn("CJ is paid $15.62 for order #1001 from your wallet", self.speaker.said[-1])
        self.assertNotIn("wallet is down", self.speaker.said[-1])
        self.assertEqual(self.cj.method, C.WALLET_LABEL)
        steps = [e["step"] for e in self.audit(done)]
        for step in ("shopify", "cj_row", "payment_page", "offered", "approved", "recheck_shopify", "status_before",
                     "pressing_pay", "after_pay", "status_after", "finished"):
            self.assertIn(step, steps)
        self.assertEqual(next(e for e in self.audit(done) if e["step"] == "status_after")["tab"], "Pending")
        self.assertTrue(any("before-pay" in s for s in self.cj.shots) and any("after-pay" in s for s in self.cj.shots))
        self.assertTrue(any("cj-order" in s for s in self.cj.shots))

    def test_double_tap_pays_once(self):
        p = self.payer()
        r = self.offered(p)
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        with self.assertRaises(ValueError):
            p.approve(r["id"], "phone", nonce=r["nonce"], amount_usd=15.62, background=False)
        p.pay(r["id"])                                            # even a stray second call does nothing
        self.assertEqual(self.cj.presses, 1)
        self.assertFalse(p.prepare(dict(ORDER), force=True)["ok"], "a paid order is never offered again")

    def test_order_moved_on_from_imported(self):
        p = self.payer(FakeCJ(tab="Imported"))
        r = self.offered(p)
        self.assertEqual(self.cj.tabs["Orders Picking"], [self.cj.row])
        self.assertEqual(r["amount_usd"], 15.62)

    def test_already_paid_on_cj_is_left_alone(self):
        p = self.payer(FakeCJ(tab="Processing"))
        out = p.prepare(dict(ORDER))
        self.assertEqual((out["ok"], out["outcome"]), (False, "already_paid"))
        self.assertFalse(p.wanted(dict(ORDER)), "and never looked at again")
        self.assertEqual(self.speaker.said, [])

    # ---------------------------------------------------------------- the phone
    def test_phone_answer_must_carry_the_code_and_amount(self):
        p = self.payer()
        r = self.offered(p)
        self.assertEqual(p.take_phone_answers([{"id": r["id"], "action": "pay", "nonce": "guess", "amount_usd": 15.62}]), [])
        self.assertEqual(p.take_phone_answers([{"id": r["id"], "action": "pay", "nonce": r["nonce"], "amount_usd": 1.00}]), [])
        self.assertEqual(p.store.get(r["id"])["status"], "offered")
        p.approve = lambda *a, **k: C.Payer.approve(p, *a, **dict(k, background=False))
        good = {"id": r["id"], "action": "pay", "nonce": r["nonce"], "amount_usd": 15.62}
        self.assertEqual(p.take_phone_answers([good, good]), [r["id"]])
        self.assertEqual((p.store.get(r["id"])["status"], p.store.get(r["id"])["decided_by"], self.cj.presses), ("paid", "phone", 1))

    def test_phone_not_now(self):
        p = self.payer()
        r = self.offered(p)
        p.take_phone_answers([{"id": r["id"], "action": "reject", "nonce": r["nonce"]}])
        self.assertEqual((p.store.get(r["id"])["status"], self.cj.presses), ("rejected", 0))
        self.assertFalse(p.wanted(dict(ORDER)), "not offered again straight away")

    def test_offer_expires(self):
        p = self.payer()
        r = self.offered(p)
        with self.assertRaises(ValueError):
            p.store.approve(r["id"], "window", amount_usd=15.62, now=time.time() + C.OFFER_SECONDS + 60)
        self.assertEqual(p.store.get(r["id"])["status"], "expired")
        self.assertEqual(self.cj.presses, 0)

    # ---------------------------------------------------------------- mismatches and caps
    def assertNotOffered(self, p, words):
        out = p.prepare(dict(ORDER))
        self.assertFalse(out["ok"], out)
        self.assertIn(words, out["why"])
        self.assertEqual(p.store.offered(), [])
        self.assertEqual(self.cj.presses, 0)

    def test_wrong_quantity_blocks(self):
        self.assertNotOffered(self.payer(FakeCJ(qty=3)), "CJ has 3 of CJYS211498101AZ but the customer ordered 2")

    def test_wrong_postcode_blocks(self):
        self.assertNotOffered(self.payer(FakeCJ(postcode="M1 1AA")), "postcode on CJ doesn't match")

    def test_extra_item_blocks(self):
        self.assertNotOffered(self.payer(FakeCJ(extra="\nCJXX999999 x 1")), "items the customer didn't order")

    def test_payment_page_amount_must_match_the_order(self):
        self.assertNotOffered(self.payer(FakeCJ(pay_amount="31.24")), "payment page says $31.24")

    def test_per_order_cap(self):
        self.assertNotOffered(self.payer(FakeCJ(total="15.62"), max_per_order_usd=10), "over the $10 limit per order")

    def test_cost_must_be_below_what_the_customer_paid(self):
        self.assertNotOffered(self.payer(FakeCJ(total="24.00"), max_per_order_usd=40), "isn't less than the £18.98")

    def test_daily_cap(self):
        p = self.payer(max_per_day_usd=20)
        earlier = p.store.create(shop_order="#1000", amount_usd=10.0, summary="x", folder=self.tmp.name)
        p.store.approve(earlier, "window")
        p.store.update(earlier, status="paid")
        self.assertNotOffered(p, "over the $20 limit for today ($10.00 already)")

    def test_daily_cap_is_checked_again_at_the_tap(self):
        p = self.payer(max_per_day_usd=20)
        r = self.offered(p)
        other = p.store.create(shop_order="#1002", amount_usd=10.0, summary="x", folder=self.tmp.name)
        p.store.approve(other, "window")
        p.store.update(other, status="paid")
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        self.assertEqual((p.store.get(r["id"])["status"], self.cj.presses), ("blocked", 0))

    def test_cancelled_shopify_order_blocks_at_the_tap(self):
        p = self.payer()
        r = self.offered(p)
        self.shopify_page = SHOPIFY_PAGE.replace("PaidIn progress", "Refunded")
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        self.assertEqual((p.store.get(r["id"])["status"], self.cj.presses), ("blocked", 0))

    def test_cj_amount_changed_since_the_offer_blocks(self):
        p = self.payer()
        r = self.offered(p)
        self.cj.pay_amount = "19.99"
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        self.assertEqual((p.store.get(r["id"])["status"], self.cj.presses), ("blocked", 0))
        self.assertIn("doesn't show the same order and amount", self.speaker.said[-1])

    # ---------------------------------------------------------------- stopping for Danny
    def test_card_details_or_password_asked_stops_before_pay(self):
        cvv = {"tag": "input", "type": "text", "name": "cvv", "placeholder": "CVV", "id": "", "aria": "", "autocomplete": "",
               "field_label": ""}
        p = self.payer(FakeCJ(card_inputs=[cvv]))
        r = self.offered(p)
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        done = p.store.get(r["id"])
        self.assertEqual((done["status"], self.cj.presses), ("blocked", 0))
        self.assertIn("card details", done["result"])

    def test_wallet_short_means_danny_pays_on_cj(self):
        p = self.payer(FakeCJ(wallet="0.00"))
        r = self.offered(p)
        self.assertEqual(r["method"], "handoff")
        self.assertIn("card ending 9336", self.speaker.said[-1])
        self.assertNotIn("top up", " ".join(self.speaker.said).lower())
        self.assertEqual(C.public(r)["pay_url"], PAY_URL)
        with self.assertRaises(ValueError):
            p.approve(r["id"], "window", amount_usd=15.62, background=False)
        self.assertEqual(p.take_phone_answers([{"id": r["id"], "action": "pay", "nonce": r["nonce"], "amount_usd": 15.62}]), [])
        self.assertEqual((p.store.get(r["id"])["status"], self.cj.presses), ("offered", 0))
        self.assertGreater(r["expires_at"] - r["created_at"], 23 * 3600, "the hand-off stays up for a day")

    def test_handoff_is_marked_paid_once_cj_shows_it(self):
        p = self.payer(FakeCJ(wallet="0.00"))
        r = self.offered(p)
        self.assertEqual(p.verify_handoffs(), [], "not paid yet")
        self.assertEqual(p.store.get(r["id"])["status"], "offered")
        self.cj._move("Pending")                           # Danny paid it on CJ himself
        self.assertEqual(p.verify_handoffs(), ["#1001"])
        done = p.store.get(r["id"])
        self.assertEqual((done["status"], done["decided_by"], self.cj.presses), ("paid", "danny_on_cj", 0))
        self.assertIn("CJ shows order #1001 as paid", self.speaker.said[-1])
        self.assertEqual(p.verify_handoffs(), [])
        self.assertEqual(p.store.spent_today(), 15.62)
        self.cj._move("Dispatched")
        self.cj.tabs["Dispatched"][0] += "\nTracking No.: LP00123456789CN"
        self.cj.row = self.cj.tabs["Dispatched"][0]
        self.assertEqual(p.check_shipping(), ["#1001"])
        self.assertIn("tracking LP00123456789CN", self.speaker.said[-1])

    def test_wallet_dropped_since_the_offer_blocks(self):
        p = self.payer()
        r = self.offered(p)
        self.cj.wallet = 5.0
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        done = p.store.get(r["id"])
        self.assertEqual((done["status"], self.cj.presses), ("blocked", 0))
        self.assertIn("only has $5.00", done["result"])

    def test_payment_page_says_insufficient_blocks(self):
        p = self.payer(FakeCJ(wallet_page_says=False))
        r = self.offered(p)
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        self.assertEqual((p.store.get(r["id"])["status"], self.cj.presses), ("blocked", 0))

    def test_low_wallet_note_after_paying(self):
        p = self.payer(FakeCJ(wallet="30.00"))
        r = self.offered(p)
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        self.assertIn("Your CJ wallet is down to $14.38.", self.speaker.said[-1])

    def test_bank_check_after_pay_stops_without_retry(self):
        p = self.payer(FakeCJ(outcome="otp"))
        r = self.offered(p)
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        self.assertEqual((p.store.get(r["id"])["status"], self.cj.presses), ("needs_danny", 1))
        self.assertIn("finish it yourself in Jarvis's browser window", self.speaker.said[-1])
        self.assertFalse(p.prepare(dict(ORDER), force=True)["ok"])

    def test_unclear_result_stops_without_retry(self):
        p = self.payer(FakeCJ(outcome="silent"))
        r = self.offered(p)
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        done = p.store.get(r["id"])
        self.assertEqual((done["status"], self.cj.presses), ("unclear", 1))
        self.assertIn("still lists it under Awaiting Payment", done["result"])
        self.assertIn("check the order on CJ yourself", self.speaker.said[-1])
        self.assertFalse(p.wanted(dict(ORDER)))
        self.assertFalse(p.prepare(dict(ORDER), force=True)["ok"], "never offered again: it may have been paid")
        self.assertEqual(self.cj.presses, 1)

    def test_declined(self):
        p = self.payer(FakeCJ(outcome="declined"))
        r = self.offered(p)
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        self.assertEqual((p.store.get(r["id"])["status"], self.cj.presses), ("failed", 1))

    def test_a_crash_after_pressing_pay_counts_as_unclear(self):
        p = self.payer(FakeCJ(outcome="crash"))
        r = self.offered(p)
        p.approve(r["id"], "window", amount_usd=15.62, background=False)
        self.assertEqual((p.store.get(r["id"])["status"], self.cj.presses), ("unclear", 1))

    def test_restart_mid_payment_is_unclear(self):
        p = self.payer()
        r = self.offered(p)
        p.store.approve(r["id"], "window", amount_usd=15.62)
        p.store.start_paying(r["id"])
        self.assertEqual([x["id"] for x in p.store.interrupted()], [r["id"]])
        self.assertEqual(p.store.get(r["id"])["status"], "unclear")
        self.assertEqual(p.store.spent_today(), 15.62, "an unclear payment still counts against the day")

    def test_logged_out_of_cj(self):
        self.assertNotOffered(self.payer(FakeCJ(logged_in=False)), "logged out of CJdropshipping")

    def test_real_cj_layout_matches_on_quantity_name_and_street(self):
        p = self.payer(FakeCJ(real_layout=True))
        r = self.offered(p)
        self.assertEqual((r["amount_usd"], r["cj_order"]), (15.62, "DP2610142124070665600"))

    def test_real_cj_layout_wrong_quantity_blocks(self):
        self.assertNotOffered(self.payer(FakeCJ(real_layout=True, qty=3)), "CJ has 3 item(s) but the customer ordered 2")

    def test_real_cj_layout_wrong_street_blocks(self):
        self.assertNotOffered(self.payer(FakeCJ(real_layout=True, street="9 Other Road")), "delivery address on CJ doesn't match")

    def test_no_pay_button_still_gives_a_checked_handoff_to_the_list(self):
        p = self.payer(FakeCJ(real_layout=True, pay_button=False))
        r = self.offered(p)
        self.assertEqual(r["method"], "handoff", "never a wallet payment without CJ's payment page")
        self.assertIn("orderList", r["pay_url"])
        self.assertIn("find order #1001 there", self.speaker.said[-1])

    def test_an_order_recorded_as_paid_is_never_touched(self):
        p = self.payer()
        p.store.mark_seen("#1001", "already_paid", "Danny paid it himself")
        self.assertFalse(p.wanted(dict(ORDER)))
        self.assertFalse(p.prepare(dict(ORDER), force=True)["ok"], "not even when asked to offer it")
        self.assertEqual((self.reads, self.cj.clicks, self.cj.tab), (0, [], None), "nothing was opened or clicked")

    def test_only_paid_unshipped_orders_are_looked_at(self):
        p = self.payer()
        self.assertFalse(p.wanted(dict(ORDER, payment="Pending")))
        self.assertFalse(p.wanted(dict(ORDER, fulfilment="Fulfilled")))
        self.assertFalse(p.wanted(dict(ORDER, id="")))
        self.assertTrue(p.wanted(dict(ORDER)))


class ServerPayTests(unittest.TestCase):
    """Only the window's own button (UI token) can approve; the brain's token can't."""

    def setUp(self):
        from jarvis_v2 import config, runs, server
        from jarvis_v2.tests.test_server import _free_port
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (config.DATA_DIR, server.TOKEN_FILE)
        config.DATA_DIR = self.tmp.name
        server.TOKEN_FILE = os.path.join(self.tmp.name, "server_token")
        self.port = _free_port()
        executor = runs.Executor(store=runs.RunStore(os.path.join(self.tmp.name, "runs.db")), popen=None)
        self.srv, self.state = server.start(port=self.port, executor=executor, announcer=Speaker())
        self.cj = FakeCJ()
        self.state.cjpay = C.Payer(store=C.PayStore(os.path.join(self.tmp.name, "cj_pay.db")), driver=self.cj,
                                   shopify=lambda _id: SHOPIFY_PAGE, announcer=Speaker(), folder=os.path.join(self.tmp.name, "jobs"),
                                   limits_path=os.path.join(self.tmp.name, "none.json"))
        self.r = self.state.cjpay.prepare(dict(ORDER))["request"]

    def tearDown(self):
        from jarvis_v2 import config, server
        self.srv.shutdown()
        self.srv.server_close()
        config.DATA_DIR, server.TOKEN_FILE = self.old
        self.tmp.cleanup()

    def call(self, path, body=None, ui=False):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("X-Jarvis-Token", self.state.token)
        if ui:
            req.add_header("X-Jarvis-UI", self.state.ui_token)
            req.add_header("Origin", f"http://127.0.0.1:{self.port}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_brain_token_cannot_pay(self):
        status, out = self.call(f"/api/cj_pay/{self.r['id']}/pay", {"amount_usd": 15.62})
        self.assertEqual(status, 403)
        self.assertEqual(self.state.cjpay.store.get(self.r["id"])["status"], "offered")

    def test_window_button_pays_once(self):
        status, view = self.call("/api/cj_pay")
        self.assertEqual([o["id"] for o in view["offers"]], [self.r["id"]])
        status, out = self.call(f"/api/cj_pay/{self.r['id']}/pay", {"amount_usd": 15.62}, ui=True)
        self.assertEqual(status, 200, out)
        for _ in range(100):
            if self.state.cjpay.store.get(self.r["id"])["status"] == "paid":
                break
            time.sleep(0.05)
        self.assertEqual(self.state.cjpay.store.get(self.r["id"])["status"], "paid")
        status, out = self.call(f"/api/cj_pay/{self.r['id']}/pay", {"amount_usd": 15.62}, ui=True)
        self.assertEqual(status, 400)
        self.assertEqual(self.cj.presses, 1)

    def test_ui_token_without_the_window_origin_cannot_pay(self):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/cj_pay/{self.r['id']}/pay", method="POST",
                                     data=json.dumps({"amount_usd": 15.62}).encode())
        req.add_header("X-Jarvis-Token", self.state.token)
        req.add_header("X-Jarvis-UI", self.state.ui_token)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        self.assertEqual(self.state.cjpay.store.get(self.r["id"])["status"], "offered")

    def test_wrong_amount_from_the_window_is_refused(self):
        status, out = self.call(f"/api/cj_pay/{self.r['id']}/pay", {"amount_usd": 99}, ui=True)
        self.assertEqual(status, 403)
        self.assertEqual(self.cj.presses, 0)


if __name__ == "__main__":
    unittest.main()
