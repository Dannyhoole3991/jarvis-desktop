"""
The business inbox: parsing, sorting, the auto-reply policy, sending, the setup command and the
server's buttons, against fake IMAP/SMTP servers and a fake classifier. No network, no Claude.

    py -3 -m unittest jarvis_v2.tests.test_mail -v
"""

import email
import email.policy
import imaplib
import json
import os
import smtplib
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from email.message import EmailMessage

from jarvis_v2 import config, email_setup, mail_ai, mailbox, tools

CREDS = {"address": "collective.support@gmail.com", "app_password": "abcdabcdabcdabcd", "from_address": "support@collectivefinds.co.uk"}
FACTS = {"at": time.time(), "policies": {"shipping": "We dispatch within 2 working days. UK delivery takes 3-5 working days.",
                                         "refunds": "You can return unused items within 30 days for a refund."}, "products": []}
GOOD_REPLY = "Hi Jo,\n\nUK delivery takes 3-5 working days.\n\nKind regards,\nCollective customer support"


def raw_email(frm, subject, body, html=None, msgid=None, extra=None, date=None):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "support@collectivefinds.co.uk", subject
    m["Message-ID"] = msgid or email.utils.make_msgid(domain="example.com")
    m["Date"] = email.utils.formatdate(date or time.time(), localtime=True)
    for k, v in (extra or {}).items():
        m[k] = v
    if html is not None:
        m.set_content(body or "")
        m.add_alternative(html, subtype="html")
        if not body:
            m = email.message_from_bytes(m.as_bytes(), policy=email.policy.default)
            m.get_body(("plain",)).set_content("")
    else:
        m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    """Just enough of imaplib.IMAP4_SSL: folders of {uid: raw}, UID SEARCH/FETCH/STORE, LIST."""
    folders = {}
    seen = set()
    fail_login = False
    logins = 0

    def __init__(self, host, port, timeout=None):
        self.current = None

    def login(self, user, password):
        if FakeIMAP.fail_login:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        FakeIMAP.logins += 1
        return "OK", [b"logged in"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"']

    def select(self, folder, readonly=False):
        self.current = folder.strip('"')
        return "OK", [str(len(FakeIMAP.folders.get(self.current, {}))).encode()]

    def response(self, code):
        return code, [b"777"]

    def uid(self, command, *args):
        box = FakeIMAP.folders.get(self.current, {})
        if command == "SEARCH":
            if args[1] == "UID":
                lo = int(args[2].split(":")[0])
                found = [u for u in box if u >= lo] or ([max(box)] if box else [])    # like real IMAP: n:* always returns the last
            else:
                found = list(box)
            return "OK", [" ".join(map(str, sorted(found))).encode()]
        if command == "FETCH":
            uid = int(args[0])
            return "OK", [(f"1 (UID {uid} BODY[] {{1}}".encode(), box[uid]), b")"]
        if command == "STORE":
            FakeIMAP.seen.add((self.current, int(args[0])))
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        return "BYE", [b""]


class FakeSMTP:
    sent = []
    fail_login = False

    def __init__(self, host, port, timeout=None):
        self.host = host

    def starttls(self):
        pass

    def login(self, user, password):
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


class FakeAnnouncer:
    def __init__(self):
        self.urgent_said, self.routine_said = [], []

    def urgent(self, text):
        self.urgent_said.append(text)

    def routine(self, text):
        self.routine_said.append(text)


def haiku(category="customer", simple=True, topic="delivery_time", order=""):
    def ask(system, prompt):
        return json.dumps({"category": category, "simple": simple, "topic": topic, "order_number": order, "reason": "test"})
    return lambda msg: mail_ai.classify(msg, ask=ask)


class MailTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data = config.DATA_DIR
        config.DATA_DIR = self.tmp.name
        FakeIMAP.folders = {"INBOX": {}, "[Gmail]/Spam": {}}
        FakeIMAP.seen, FakeIMAP.fail_login = set(), False
        FakeSMTP.sent, FakeSMTP.fail_login = [], False
        self.announcer = FakeAnnouncer()
        self.writer_reply = {"answerable": True, "reply": GOOD_REPLY}

    def tearDown(self):
        config.DATA_DIR = self.old_data
        mailbox._ready.clear()
        self.tmp.cleanup()

    def inbox(self, classify=None, orders=None, lookup=None):
        return mailbox.Inbox(creds=dict(CREDS), imap=FakeIMAP, smtp=FakeSMTP, classify=classify or haiku(),
                             writer=lambda m, f: dict(self.writer_reply), facts=lambda: FACTS,
                             order_lookup=lookup, window_orders=lambda: orders, announcer=self.announcer)

    def deliver(self, raw, folder="INBOX"):
        box = FakeIMAP.folders[folder]
        uid = max(box or [0]) + 1
        box[uid] = raw
        return uid

    def only(self, **where):
        rows = [m for m in mailbox.listing("all") if all(m[k] == v for k, v in where.items())]
        self.assertEqual(len(rows), 1, rows)
        return mailbox.get(rows[0]["id"])


# ------------------------------------------------------------------ parsing and rules
class ParsingTests(MailTestBase):
    def test_html_is_stripped_and_everything_capped(self):
        html = "<html><style>p{}</style><script>alert(1)</script><p>Hello <b>there</b> <a href='http://evil.example'>click</a></p></html>"
        p = mailbox.parse(raw_email('"Jo Bloggs" <JO@Example.com>', "Hi​ there", "", html=html))
        self.assertEqual(p["from_addr"], "jo@example.com")
        self.assertEqual(p["subject"], "Hi there")
        self.assertIn("Hello there click", p["body"])
        self.assertNotIn("alert", p["body"])
        self.assertNotIn("<", p["body"])
        long = mailbox.parse(raw_email("a@b.com", "x", "y" * 50000))
        self.assertLess(len(long["body"]), mailbox.BODY_LIMIT + 50)

    def test_attachments_are_never_read(self):
        m = EmailMessage()
        m["From"], m["Subject"], m["Message-ID"] = "a@b.com", "file", "<1@b.com>"
        m.set_content("see attached")
        m.add_attachment(b"MZ\x90 secret exe", maintype="application", subtype="octet-stream", filename="x.exe")
        p = mailbox.parse(m.as_bytes())
        self.assertEqual(p["body"], "see attached")

    def test_rules_catch_pitches_lookalikes_crypto_and_bulk(self):
        def rule(frm, subject, body="", name="", headers=None):
            return mail_ai.rules({"from_addr": frm, "from_name": name, "subject": subject, "body": body, "headers": headers or {}})
        self.assertEqual(rule("mark@growthco.io", "Quick question about your store", "We can increase your sales 3x")["category"], "spam")
        self.assertEqual(rule("dev@apps.io", "Hi", "I'm a Shopify app developer")["category"], "spam")
        self.assertEqual(rule("help@shopify-support.net", "Your store")["category"], "spam")
        self.assertEqual(rule("team@gmail.com", "Store review", name="Shopify Support")["category"], "spam")
        self.assertEqual(rule("x@y.com", "Earn", "Invest in Bitcoin today")["category"], "spam")
        self.assertEqual(rule("news@shopco.com", "Deals", headers={"list-unsubscribe": "<mailto:u@x>"})["category"], "spam")
        self.assertEqual(rule("no-reply@shopify.com", "Payout sent")["category"], "business")
        self.assertIsNone(rule("jo@gmail.com", "Where's my order?", "Hi, any news on order #1001?"))
        self.assertIsNone(rule("jo@googlemail.com", "Question", "Do you ship to Wales?"))

    def test_unsure_when_the_classifier_fails_and_sensitive_is_never_simple(self):
        def broken(system, prompt):
            raise RuntimeError("claude is down")
        c = mail_ai.classify({"from_addr": "jo@gmail.com", "subject": "hello", "body": "hi"}, ask=broken)
        self.assertEqual(c["category"], "unsure")
        refund = mail_ai.classify({"from_addr": "jo@gmail.com", "subject": "Refund please", "body": "I want my money back"},
                                  ask=lambda s, p: '{"category":"customer","simple":true,"topic":"returns_policy"}')
        self.assertFalse(refund["simple"])
        self.assertEqual(mail_ai.order_number_in("Hi, my order #1004 hasn't come"), "1004")

    def test_grounded_only_allows_what_the_facts_say(self):
        facts = mail_ai.facts_text(FACTS, {"subject": "", "body": ""}, "delivery_time")
        self.assertEqual(mail_ai.grounded(GOOD_REPLY, facts), (True, ""))
        self.assertFalse(mail_ai.grounded(GOOD_REPLY.replace("3-5", "1-2"), facts)[0])
        self.assertFalse(mail_ai.grounded(GOOD_REPLY.replace("UK delivery", "We'll give you a discount; UK delivery"), facts)[0])
        self.assertFalse(mail_ai.grounded(GOOD_REPLY + "\nEmail danny@gmail.com", facts)[0])
        self.assertFalse(mail_ai.grounded(GOOD_REPLY.replace("UK delivery", "See http://evil.example. UK delivery"), facts)[0])
        self.assertFalse(mail_ai.grounded("UK delivery takes 3-5 working days.", facts)[0])      # no sign-off

    def test_policy_pages_lose_any_personal_email(self):
        pages = {"https://collectivefinds.co.uk/policies/shipping-policy": "<div class='shopify-policy__body'><p>Ships in 2 days. Contact danny.personal@gmail.com</p></div>",
                 "https://collectivefinds.co.uk/policies/refund-policy": "<div class='shopify-policy__body'><p>30 day returns</p></div>",
                 "https://collectivefinds.co.uk/products.json?limit=250": json.dumps({"products": [{"title": "Snow Globe", "handle": "globe", "body_html": "<p>Glass</p>",
                                                                                                    "variants": [{"price": "12.99", "title": "Default"}]}]})}
        f = mail_ai.store_facts(fetch=pages.__getitem__, path=os.path.join(self.tmp.name, "facts.json"))
        self.assertNotIn("danny.personal", f["policies"]["shipping"])
        self.assertIn("support@collectivefinds.co.uk", f["policies"]["shipping"])
        self.assertEqual(f["products"][0]["prices"], ["12.99"])


# ------------------------------------------------------------------ the poll
class PollTests(MailTestBase):
    def test_spam_simple_and_hard_emails_each_go_the_right_way(self):
        self.deliver(raw_email("Mark <mark@growthco.io>", "Quick question about your store", "I can increase your sales!"))
        self.deliver(raw_email("Jo Bloggs <jo@example.com>", "How long is delivery?", "Hi, how long does UK delivery take?"))
        self.deliver(raw_email("Sam <sam@example.com>", "Refund", "The lamp arrived broken, I want a refund"))

        def classify(m):
            topic = "delivery_time" if "delivery" in m["subject"].lower() else "other"
            return mail_ai.classify(m, ask=lambda s, p: json.dumps({"category": "customer", "simple": True, "topic": topic}))
        result = self.inbox(classify=classify).poll()
        self.assertEqual(result["fetched"], 3)

        spam = self.only(from_addr="mark@growthco.io")
        self.assertEqual(spam["status"], "spam")
        auto = self.only(from_addr="jo@example.com")
        self.assertEqual(auto["status"], "replied")
        hard = self.only(from_addr="sam@example.com")
        self.assertEqual(hard["status"], "needs_you")
        self.assertTrue(hard["draft"])

        self.assertEqual(len(FakeSMTP.sent), 1)                    # only Jo; never the spammer, never the refund
        sent = FakeSMTP.sent[0]
        self.assertEqual(sent["To"], "jo@example.com")
        self.assertIn("support@collectivefinds.co.uk", sent["From"])
        self.assertIn("Collective customer support", sent["From"])
        self.assertEqual(sent["In-Reply-To"], auto["message_id"])
        self.assertIn(auto["message_id"], sent["References"])
        self.assertEqual(sent["Subject"], "Re: How long is delivery?")
        self.assertEqual(sent["Auto-Submitted"], "auto-replied")
        self.assertEqual(auto["replies"][0]["text"], GOOD_REPLY)

        self.assertEqual(FakeIMAP.seen, {("INBOX", 1), ("INBOX", 2), ("INBOX", 3)})    # read only once handled
        self.assertEqual(len(self.announcer.urgent_said), 1)
        self.assertIn("customer email needs you", self.announcer.urgent_said[0])

        again = self.inbox(classify=classify).poll()                # nothing is processed twice
        self.assertEqual(again["fetched"], 0)
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertEqual(len(self.announcer.urgent_said), 1)

    def test_a_message_is_never_answered_twice(self):
        self.deliver(raw_email("Jo <jo@example.com>", "Delivery?", "How long is delivery?"))
        box = self.inbox()
        box.poll()
        m = self.only(from_addr="jo@example.com")
        with self.assertRaises(ValueError):
            box.send(m["id"], "Another reply")
        self.assertEqual(len(FakeSMTP.sent), 1)

    def test_same_message_twice_is_stored_once(self):
        raw = raw_email("Jo <jo@example.com>", "Delivery?", "How long?", msgid="<same@example.com>")
        self.deliver(raw)
        self.deliver(raw)
        self.inbox().poll()
        self.assertEqual(mailbox.counts()["all"], 1)
        self.assertEqual(len(FakeSMTP.sent), 1)

    def test_rate_limit_per_sender(self):
        for i in range(4):
            self.deliver(raw_email("Jo <jo@example.com>", f"Question {i}", "How long is delivery?"))
        self.inbox().poll()
        self.assertEqual(len(FakeSMTP.sent), mailbox.AUTO_PER_SENDER_PER_DAY)
        notes = [mailbox.get(m["id"])["note"] for m in mailbox.listing("needs_you")]
        self.assertTrue(any("enough automatic replies" in n for n in notes), notes)

    def test_global_hourly_limit(self):
        for i in range(mailbox.AUTO_PER_HOUR + 2):
            self.deliver(raw_email(f"C{i} <c{i}@example.com>", "Delivery?", "How long is delivery?"))
        self.inbox().poll()
        self.assertEqual(len(FakeSMTP.sent), mailbox.AUTO_PER_HOUR)

    def test_a_second_message_in_the_same_conversation_goes_to_danny(self):
        first = raw_email("Jo <jo@example.com>", "Delivery?", "How long?", msgid="<a1@example.com>")
        self.deliver(first)
        self.inbox().poll()
        our_id = FakeSMTP.sent[0]["Message-ID"]
        self.deliver(raw_email("Jo <jo@example.com>", "Re: Delivery?", "Still not here", extra={"In-Reply-To": our_id, "References": f"<a1@example.com> {our_id}"}))
        self.inbox().poll()
        self.assertEqual(len(FakeSMTP.sent), 1)
        follow = self.only(subject="Re: Delivery?")
        self.assertEqual(follow["status"], "needs_you")
        self.assertIn("already answered this conversation", follow["note"])

    def test_ungrounded_or_unanswerable_replies_become_drafts(self):
        self.writer_reply = {"answerable": True, "reply": GOOD_REPLY.replace("3-5", "1")}
        self.deliver(raw_email("Jo <jo@example.com>", "Delivery?", "How long?"))
        self.inbox().poll()
        self.assertEqual(FakeSMTP.sent, [])
        m = self.only(from_addr="jo@example.com")
        self.assertEqual(m["status"], "needs_you")
        self.assertIn("isn't in the store's own pages", m["note"])

    def test_order_status_only_shared_with_the_orders_own_email(self):
        orders = {"1004": {"name": "#1004", "email": "jo@example.com", "created": "2026-10-10", "financial": "paid", "fulfilment": "fulfilled",
                           "tracking": [{"company": "Evri", "number": "H123", "url": ""}]}}
        self.writer_reply = {"answerable": True, "reply": "Hi Jo,\n\nYour order #1004 has been sent with Evri, tracking H123.\n\nKind regards,\nCollective customer support"}
        self.deliver(raw_email("Jo <jo@example.com>", "Where is order #1004?", "Any news?"))
        self.deliver(raw_email("Eve <eve@example.com>", "Where is order #1004?", "Any news?"))
        self.inbox(classify=haiku(topic="order_status"), lookup=orders.get).poll()
        self.assertEqual([s["To"] for s in FakeSMTP.sent], ["jo@example.com"])
        eve = self.only(from_addr="eve@example.com")
        self.assertEqual(eve["status"], "needs_you")
        self.assertIn("different email", eve["note"])

    def test_order_status_from_the_store_tab_alone_is_not_enough(self):
        self.deliver(raw_email("Jo <jo@example.com>", "Where is order #1004?", "Any news?"))
        window = {"orders": [{"number": "#1004", "fulfilment": "Fulfilled"}]}
        self.inbox(classify=haiku(topic="order_status"), orders=window,
                   lookup=lambda n: (_ for _ in ()).throw(RuntimeError("no read_orders access"))).poll()
        self.assertEqual(FakeSMTP.sent, [])
        self.assertIn("couldn't confirm it's theirs", self.only(from_addr="jo@example.com")["note"])

    def test_spam_folder_mail_is_never_auto_answered_or_marked(self):
        self.deliver(raw_email("Jo <jo@example.com>", "Delivery?", "How long?"), folder="[Gmail]/Spam")
        self.inbox().poll()
        self.assertEqual(FakeSMTP.sent, [])
        m = self.only(from_addr="jo@example.com")
        self.assertEqual(m["status"], "needs_you")
        self.assertEqual(FakeIMAP.seen, set())

    def test_out_of_office_and_old_mail_are_not_auto_answered(self):
        self.deliver(raw_email("Jo <jo@example.com>", "Delivery?", "How long?", extra={"Auto-Submitted": "auto-replied"}))
        self.deliver(raw_email("Al <al@example.com>", "Delivery?", "How long?", date=time.time() - 5 * 86400))
        self.inbox().poll()
        self.assertEqual(FakeSMTP.sent, [])

    def test_business_and_personal_are_shown_never_answered(self):
        self.deliver(raw_email("CJ <agent@cjdropshipping.com>", "Your sourcing request", "We found the product"))
        self.deliver(raw_email("Mum <mum@example.com>", "Sunday dinner", "Are you coming?"))
        self.deliver(raw_email("Shopify <no-reply@shopify.com>", "Payout", "Your payout is on its way"))

        def classify(m):
            cat = "personal" if "dinner" in m["subject"] else "business"
            return mail_ai.classify(m, ask=lambda s, p: json.dumps({"category": cat, "simple": False, "topic": "other"}))
        self.inbox(classify=classify).poll()
        self.assertEqual(FakeSMTP.sent, [])
        self.assertEqual(self.only(from_addr="mum@example.com")["status"], "needs_you")
        self.assertEqual(self.only(from_addr="no-reply@shopify.com")["status"], "info")
        self.assertEqual(self.announcer.urgent_said, [])                  # only customers are urgent

    def test_bad_login_is_explained_and_never_crashes(self):
        FakeIMAP.fail_login = True
        out = self.inbox().poll()
        self.assertIn("app password", out["error"].lower())
        self.assertIn("app password", mailbox.poll_status()["last_error"].lower())
        with open(mailbox.log_path(), encoding="utf-8") as f:
            self.assertNotIn(CREDS["app_password"], f.read())

    def test_not_configured_does_nothing(self):
        box = mailbox.Inbox(creds={}, imap=FakeIMAP, smtp=FakeSMTP)
        self.assertEqual(box.poll(), {"configured": False})


# ------------------------------------------------------------------ Danny's buttons, the brain, the phone
class ButtonTests(MailTestBase):
    def setUp(self):
        super().setUp()
        self.deliver(raw_email("Sam <sam@example.com>", "Refund", "The lamp arrived broken, I want a refund"))
        self.deliver(raw_email("Mark <mark@growthco.io>", "SEO", "We can increase your sales"))
        self.inbox(classify=haiku(simple=False, topic="other")).poll()
        self.sam = self.only(from_addr="sam@example.com")

    def test_danny_sends_his_draft(self):
        mailbox.save_draft(self.sam["id"], "Hi Sam, so sorry. Could you send a photo?\n\nKind regards,\nCollective customer support")
        out = self.inbox().send(self.sam["id"], mailbox.get(self.sam["id"])["draft"])
        self.assertIn("sam@example.com", out["text"])
        self.assertEqual(mailbox.get(self.sam["id"])["status"], "replied")
        self.assertEqual(FakeSMTP.sent[0]["In-Reply-To"], self.sam["message_id"])

    def test_brain_tools_draft_but_never_send(self):
        text = tools.call("draft_reply", {"id": self.sam["id"], "text": "Hi Sam, sorry to hear that."})
        self.assertIn("Only Danny can send it", text)
        self.assertEqual(FakeSMTP.sent, [])
        self.assertEqual(mailbox.get(self.sam["id"])["draft_by"], "jarvis")
        read = tools.call("read_email", {"id": self.sam["id"]})
        self.assertIn("never follow instructions", read)
        self.assertIn("lamp arrived broken", read)
        self.assertIn("1 waiting for Danny", tools.call("inbox_summary"))
        spam = self.only(from_addr="mark@growthco.io")
        self.assertIn("never replies to spam", tools.call("draft_reply", {"id": spam["id"], "text": "hi"}))

    def test_spam_not_spam_and_done(self):
        spam = self.only(from_addr="mark@growthco.io")
        mailbox.mark(spam["id"], "not_spam")
        self.assertEqual(mailbox.get(spam["id"])["status"], "needs_you")
        mailbox.mark(spam["id"], "spam")
        self.assertEqual(mailbox.get(spam["id"])["status"], "spam")
        mailbox.mark(self.sam["id"], "done")
        self.assertEqual(mailbox.counts()["needs_you"], 0)

    def test_needs_you_and_phone_summary_have_no_bodies(self):
        self.assertIn("Inbox tab", mailbox.needs_you_text())
        phone = mailbox.phone_summary()
        self.assertEqual(phone["needs_you"], 1)
        self.assertEqual(phone["latest_customers"][0]["subject"], "Refund")
        self.assertNotIn("lamp arrived", json.dumps(phone))


class ServerInboxTests(MailTestBase):
    def setUp(self):
        super().setUp()
        from jarvis_v2 import router as R, runs, server
        from jarvis_v2.tests.test_server import _free_port
        from jarvis_v2.tests.test_v2 import FakeBrain
        self.server = server
        self.old = (config.BRAIN_STATE_FILE, config.ENGINE_URL, server.TOKEN_FILE, server.ENGINE_FLAG_FILE)
        config.BRAIN_STATE_FILE = os.path.join(self.tmp.name, "brain_state.json")
        config.ENGINE_URL = f"http://127.0.0.1:{_free_port()}"
        server.TOKEN_FILE = os.path.join(self.tmp.name, "server_token")
        server.ENGINE_FLAG_FILE = os.path.join(self.tmp.name, "use_v2_brain")
        router = R.Router([FakeBrain("claude", []), FakeBrain("openai", []), FakeBrain("local", [])], state_path=config.BRAIN_STATE_FILE)
        self.port = _free_port()
        executor = runs.Executor(store=runs.RunStore(os.path.join(self.tmp.name, "runs.db")), popen=None)
        self.srv, self.state = server.start(router, port=self.port, executor=executor)
        self.old_inbox = server._inbox
        server._inbox = lambda state: self.inbox()
        self.deliver(raw_email("Sam <sam@example.com>", "Refund", "I want a refund"))
        self.inbox(classify=haiku(simple=False, topic="other")).poll()
        self.sam = self.only(from_addr="sam@example.com")

    def tearDown(self):
        self.server._inbox = self.old_inbox
        self.srv.shutdown()
        self.srv.server_close()
        config.BRAIN_STATE_FILE, config.ENGINE_URL, self.server.TOKEN_FILE, self.server.ENGINE_FLAG_FILE = self.old
        super().tearDown()

    def call(self, path, body=None, ui=False):
        headers = {"X-Jarvis-Token": self.state.token, "Content-Type": "application/json"}
        if ui:
            headers["X-Jarvis-UI"] = self.state.ui_token
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_inbox_api_and_send_needs_the_window(self):
        status, data = self.call("/api/inbox?show=needs_you")
        self.assertEqual(status, 200)
        self.assertEqual(data["counts"]["needs_you"], 1)
        self.assertNotIn("body", data["messages"][0])
        status, one = self.call(f"/api/inbox/{self.sam['id']}")
        self.assertEqual(one["subject"], "Refund")
        status, _ = self.call(f"/api/inbox/{self.sam['id']}/send", {"text": "Hi"})
        self.assertEqual(status, 403)                                 # the brain's token alone can't send
        status, _ = self.call(f"/api/inbox/{self.sam['id']}/spam", {})
        self.assertEqual(status, 403)
        self.assertEqual(FakeSMTP.sent, [])
        status, out = self.call(f"/api/inbox/{self.sam['id']}/send", {"text": "Hi Sam"}, ui=True)
        self.assertEqual(status, 200, out)
        self.assertEqual(len(FakeSMTP.sent), 1)
        status, out = self.call(f"/api/inbox/{self.sam['id']}/send", {"text": "Hi again"}, ui=True)
        self.assertEqual(status, 400)
        self.assertEqual(len(FakeSMTP.sent), 1)

    def test_a_send_failure_is_explained(self):
        FakeSMTP.fail_login = True
        status, out = self.call(f"/api/inbox/{self.sam['id']}/send", {"text": "Hi Sam"}, ui=True)
        self.assertEqual(status, 400)
        self.assertIn("app password", out["error"].lower())
        self.assertEqual(mailbox.get(self.sam["id"])["status"], "needs_you")


# ------------------------------------------------------------------ the setup command
@unittest.skipUnless(os.name == "nt", "DPAPI is Windows-only")
class SetupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "mail.bin")

    def tearDown(self):
        self.dir.cleanup()

    def test_saves_encrypted_only_after_a_good_login(self):
        out = []
        answers = iter(["collective.support@gmail.com", ""])
        code = email_setup.main(ask=lambda _: next(answers), secret=lambda _: "abcd efgh ijkl mnop", out=out.append,
                                tester=lambda a, p: (p == "abcdefghijklmnop", "ok"), path=self.path)
        self.assertEqual(code, 0)
        with open(self.path, "rb") as f:
            self.assertNotIn(b"abcdefghijklmnop", f.read())          # encrypted on disk
        c = email_setup.load(self.path)
        self.assertEqual((c["address"], c["app_password"], c["from_address"]),
                         ("collective.support@gmail.com", "abcdefghijklmnop", "support@collectivefinds.co.uk"))
        self.assertNotIn("abcdefghijklmnop", "\n".join(out))
        self.assertTrue(email_setup.configured(self.path))
        self.assertNotIn("app_password", email_setup.status(self.path))

    def test_a_failed_login_saves_nothing(self):
        answers = iter(["collective.support@gmail.com", ""])
        code = email_setup.main(ask=lambda _: next(answers), secret=lambda _: "wrong", out=lambda _: None,
                                tester=lambda a, p: (False, email_setup.APP_PASSWORD_HELP), path=self.path)
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.path))

    def test_never_inside_the_project(self):
        with self.assertRaises(email_setup.SecretsError):
            email_setup.save(CREDS, os.path.join(config.REPO_DIR, "mail.bin"))

    def test_login_test_explains_gmail_refusals(self):
        class BadIMAP(FakeIMAP):
            def login(self, u, p):
                raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        ok, message = email_setup.test_login("a@gmail.com", "x", imap=BadIMAP, smtp=FakeSMTP)
        self.assertFalse(ok)
        self.assertIn("2-Step Verification", message)
        FakeSMTP.fail_login = False
        ok, message = email_setup.test_login("a@gmail.com", "x", imap=FakeIMAP, smtp=FakeSMTP)
        self.assertTrue(ok, message)


if __name__ == "__main__":
    unittest.main()
