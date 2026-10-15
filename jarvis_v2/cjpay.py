"""
Paying CJdropshipping for a shop order, one tap at a time.

When a Shopify order comes in, CJ gets a copy of it but only ships once it's paid. Jarvis checks the
order, gets CJ's payment page ready and asks Danny ("Pay CJ $15.62 for order #1001: 2 x Ceramic
Christmas Tree Light, to J Hoole, Barnsley. You make about £5."), in one of two ways:
  - wallet one-tap: when Danny's CJ wallet balance covers the order, Danny taps Pay (Jarvis window or
    phone) and Jarvis pays it with "Pay with CJ wallet". No card is involved at all.
  - hand-off (the default while the wallet is short): CJ asks for the card's CVV on every card payment
    (seen live 2026-10-15), and Jarvis never types that. So the card opens CJ's payment page for Danny,
    who picks his saved card (ending 9336), types the CVV and presses PAY himself. Jarvis then checks
    CJ's order list until it shows paid, and follows the shipping.

The rules are enforced in this code, not left to a model:
  - Jarvis never types, reads, stores or logs card numbers, CVVs or expiry dates, and never pays by
    card itself. The CJ driver has no way to type at all: it can open pages, click tabs, click one named
    button in one order's row, choose "Pay with CJ wallet", tick the terms box and press PAY. If CJ asks
    for card details, a CVV, a password, a bank check (3-D Secure / one-time code) or a human check,
    Jarvis stops and tells Danny.
  - A payment happens only right after Danny's tap on that exact request (the window's UI token, or
    a phone answer carrying the request's one-off code and amount). One tap = one order, once: the
    request moves offered -> approved -> paying in single database steps, so a second tap does nothing.
    No auto-pay, no batching, no retries: after PAY, CJ's order status must visibly change, else stop.
  - Only an order that matches a real Shopify order: same order number, same items (SKU) and
    quantities, same delivery name and postcode, and a CJ cost below what the customer paid.
    Caps: $40 an order and $60 a day by default (Danny's card limit is £75). Only Danny can change
    them, in ~/.jarvis_v2/cj_pay_limits.json (Jarvis never writes that file), and never above $90.
  - Everything is audited in data/cj_pay/<when>-<order>/: who approved, what, amount, CJ order,
    status before and after, and screenshots of the CJ order and result pages.
"""

import contextlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time

from . import config

CJ_LIST_URL = "https://www.cjdropshipping.com/mine/dropshipping/orderList"
SHOPIFY_ORDER_URL = "https://admin.shopify.com/store/mwrfta-mh/orders/{id}"
LIMITS_FILE = os.path.join(os.path.expanduser("~"), ".jarvis_v2", "cj_pay_limits.json")
DEFAULT_LIMITS = {"enabled": True, "max_per_order_usd": 40.0, "max_per_day_usd": 60.0, "usd_to_gbp": 0.80,
                  "card_last4": "9336", "low_wallet_usd": 20.0}
WALLET_URL = "https://www.cjdropshipping.com/mine/wallet"
ABSOLUTE_MAX_USD = 90.0          # under the £75 card limit whatever the settings file says
OFFER_SECONDS = 30 * 60          # a wallet Pay request expires after half an hour (it can be offered again)
HANDOFF_SECONDS = 24 * 3600      # a "you pay it on CJ" card stays up for a day while Jarvis watches for it being paid
REOFFER_SECONDS = 3 * 3600       # an order whose offer expired or was turned down is offered again after this
RETRY_PREPARE_SECONDS = 30 * 60  # an order that couldn't be prepared is looked at again after this

# CJ's order list tabs, in the order Jarvis looks through them.
UNPAID_TABS = ("Awaiting Payment", "Orders Picking", "Imported")
PAID_TABS = ("Pending", "Processing", "Dispatched", "Completed")
MOVE_ON_LABEL = r"^\s*add to orders? picking\s*$"
TO_PAYMENT_LABEL = r"^\s*(pay|pay now|go to pay|to pay|payment|checkout|check out)\s*$"
WALLET_LABEL = "Pay with CJ wallet"

UK_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b")
USD_RE = re.compile(r"(?:US\s?\$|USD\s?\$?|\$)\s?(\d[\d,]*\.\d{2})")
GBP_RE = re.compile(r"(?:£|GBP\s?)\s?(\d[\d,]*\.\d{2})")
CJ_ORDER_RE = re.compile(r"\b(?:DP|CJ)[A-Z]{0,3}\d{12,}\b")
SKU_RE = re.compile(r"\bCJ[A-Z]{2,}\d+[A-Z0-9]*\b")
QTY_RE = re.compile(r"(?i)(?:^|\s)(?:[x×]\s?(\d{1,3})|qty\.?:?\s*(\d{1,3})|quantity:?\s*(\d{1,3})|(\d{1,3})\s*(?:pcs|pieces|units?))(?:\s|$)")
WALLET_BALANCE_RE = re.compile(r"(?i)account\s*balance\s*\(\s*(?:US)?\$\s*\)\s*[:：]?\s*\$?\s*(\d[\d,]*(?:\.\d{1,2})?)")
INSUFFICIENT_RE = re.compile(r"(?i)insufficient\s+balance")
CARD_ENTRY_TEXT_RE = re.compile(r"(?i)(enter (your )?card (number|details)|cvv|cvc|security code|expir(y|ation) date|"
                                r"3-?d ?secure|one[- ]time (pass)?code|verification code|enter (the )?code|"
                                r"authenticat(e|ion) (this|your|the) (payment|purchase)|confirm (it'?s|it is) you|sms code)")
CARD_FRAME_RE = re.compile(r"(?i)(stripe|checkout\.com|adyen|braintree|worldpay|cardinal|3ds|/acs|securecode|verifiedby|"
                           r"challenge|authenticat|hosted-?fields|card-?(number|cvc|expiry|fields?))")
DECLINED_RE = re.compile(r"(?i)(declined|payment failed|failed to pay|pay(ment)? fail|insufficient|card (was )?(rejected|refused)|"
                         r"unsuccessful|could not be processed|try again)")
SUCCESS_RE = re.compile(r"(?i)(payment success|paid successfully|successfully paid|payment (is )?complete|pay success|"
                        r"payment received|thank you for your payment|order(s)? (has|have) been paid)")
TRACKING_RE = re.compile(r"(?i)tracking\s*(?:no\.?|number|#)?\s*[:：]?\s*([A-Z0-9]{8,30})")
LONG_DIGITS_RE = re.compile(r"(?<![A-Za-z0-9])\d(?:[ -]?\d){11,18}(?![A-Za-z0-9])")


def _money(text):
    return float(str(text).replace(",", ""))


def redact(text):
    """Anything that could be a card number is hidden before it's written anywhere."""
    return LONG_DIGITS_RE.sub("[number hidden]", str(text or ""))


def _norm_postcode(text):
    m = UK_POSTCODE_RE.search((text or "").upper())
    return (m.group(1) + m.group(2)) if m else ""


def _surname(name):
    parts = re.findall(r"[A-Za-z'’-]+", name or "")
    return parts[-1].lower() if parts else ""


def order_re(number):
    """'#1001' but not '#10011'."""
    digits = re.sub(r"\D", "", str(number))
    return re.compile(r"#" + digits + r"(?!\d)")


# ------------------------------------------------------------------ limits
def limits(path=None):
    """Danny's caps. Read only: Jarvis never writes this file. Never above ABSOLUTE_MAX_USD."""
    out = dict(DEFAULT_LIMITS)
    try:
        with open(path or LIMITS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for key in DEFAULT_LIMITS:
            if key in data:
                out[key] = type(DEFAULT_LIMITS[key])(data[key])
    except (OSError, ValueError, TypeError):
        pass
    out["max_per_order_usd"] = max(0.0, min(out["max_per_order_usd"], ABSOLUTE_MAX_USD))
    out["max_per_day_usd"] = max(0.0, min(out["max_per_day_usd"], ABSOLUTE_MAX_USD))
    out["usd_to_gbp"] = max(0.6, min(out["usd_to_gbp"], 1.2))
    out["card_last4"] = re.sub(r"\D", "", out["card_last4"])[-4:]
    return out


def parse_wallet(text):
    """CJ's wallet page: the account balance in dollars, or None if it can't be read."""
    m = WALLET_BALANCE_RE.search(text or "")
    return _money(m.group(1)) if m else None


# ------------------------------------------------------------------ reading pages
def parse_shopify_order(text):
    """The Shopify admin order page: items (title, variant, SKU, quantity), delivery address, amount paid,
    and whether it was cancelled or refunded. Raises ValueError when it can't be read with confidence."""
    lines = [ln.strip() for ln in (text or "").replace("\xa0", " ").splitlines() if ln.strip()]
    items = []
    for k, ln in enumerate(lines):
        if ln != "SKU" or k + 1 >= len(lines):
            continue
        sku = lines[k + 1]
        qty = None
        for j in range(k + 2, min(k + 12, len(lines) - 1)):
            if lines[j] in ("×", "x", "X") and re.fullmatch(r"\d{1,3}", lines[j + 1]):
                qty = int(lines[j + 1])
                break
        variant = lines[k - 1] if k >= 1 and " / " in lines[k - 1] else ""
        title = lines[k - 2] if variant and k >= 2 else (lines[k - 1] if k >= 1 else "")
        if qty is None:
            raise ValueError(f"couldn't read the quantity of {sku} on the Shopify order")
        items.append({"sku": sku, "qty": qty, "title": title, "variant": variant})
    if not items:
        raise ValueError("couldn't find the items (with their SKUs) on the Shopify order")

    pc_at = next((i for i, ln in enumerate(lines) if UK_POSTCODE_RE.fullmatch(ln.upper())), None)
    if pc_at is None:
        raise ValueError("couldn't find a UK delivery postcode on the Shopify order")
    start = max((i for i in range(pc_at) if "@" in lines[i] or lines[i].lower() in ("shipping address", "delivery address")), default=None)
    if start is None or pc_at - start < 3:
        raise ValueError("couldn't read the delivery address on the Shopify order")
    name = lines[start + 1]
    street = lines[start + 2]
    town = lines[pc_at - 1]

    paid = None
    for i, ln in enumerate(lines[:-1]):
        if ln == "Paid":
            m = GBP_RE.fullmatch(lines[i + 1])
            if m:
                paid = _money(m.group(1))
    if paid is None:
        raise ValueError("couldn't read how much the customer paid on the Shopify order")
    head = " ".join(lines[:200])
    cancelled = bool(re.search(r"(?i)\b(cancell?ed|refunded|partially refunded|voided)\b", head))
    return {"items": items, "name": name, "street": street, "town": town, "postcode": _norm_postcode(lines[pc_at]),
            "paid_gbp": paid, "cancelled": cancelled}


def parse_cj_row(text):
    """One order's row on CJ's order list: the CJ order number, SKUs with quantities, money, name/postcode text."""
    text = text or ""
    numbers = sorted(set(CJ_ORDER_RE.findall(text)))
    skus = {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        for sku in SKU_RE.findall(ln):
            if CJ_ORDER_RE.fullmatch(sku):
                continue
            qty = None
            for nxt in [ln[ln.find(sku) + len(sku):]] + lines[i + 1:i + 8]:
                if SKU_RE.search(nxt) and nxt is not ln:
                    break
                m = QTY_RE.search(" " + nxt + " ")
                if m:
                    qty = int(next(g for g in m.groups() if g))
                    break
            skus[sku] = qty if sku not in skus or skus[sku] is None else skus[sku]
    total = None
    m = re.search(r"(?i)total[^$\n]{0,40}\n?\s*(?:US\s?\$|USD\s?\$?|\$)\s?(\d[\d,]*\.\d{2})", text)
    if m:
        total = _money(m.group(1))
    usd = [_money(x) for x in USD_RE.findall(text)]
    qty_total = None
    m = re.search(r"(?m)^\s*(\d{1,3})\s+(?:US\s?\$|\$)\s?(\d[\d,]*\.\d{2})\s*$", text)
    if m:
        qty_total = int(m.group(1))
        if total is None:
            total = _money(m.group(2))
    if total is None and usd:
        total = max(usd)
    return {"cj_orders": numbers, "skus": skus, "total_usd": total, "usd": usd, "qty_total": qty_total,
            "postcode": _norm_postcode(text), "text": text}


def _words(text):
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def parse_payment_page(text):
    """CJ's payment page: which order(s) and how much. Several orders on one page = batching: refused."""
    text = text or ""
    numbers = re.findall(r"(?i)order\s*(?:number|no\.?)\s*[:：]?\s*([A-Z0-9]{8,})", text)
    m = re.search(r"(?i)payable\s*amount\s*[:：]?\s*(?:US\s?\$|USD\s?\$?|\$)?\s?(\d[\d,]*\.\d{2})", text)
    return {"orders": numbers, "amount_usd": _money(m.group(1)) if m else None}


def needs_danny(snap):
    """Why Danny must take over on this page (card details, a bank check, a human check), or None."""
    from .job_browser import page_blocked, secret_field
    if not snap:
        return "the page didn't load"
    if page_blocked(snap):
        return "CJ is showing a human check (\"are you a robot\")"
    for desc in snap.get("inputs") or []:
        if desc.get("type") != "checkbox" and secret_field(desc):
            return "CJ is asking for card details or a code"
    for src in snap.get("frames") or []:
        if CARD_FRAME_RE.search(src or ""):
            return "CJ is showing a card or bank security form"
    if CARD_ENTRY_TEXT_RE.search(snap.get("text") or ""):
        return "CJ is asking for card details or a bank security check"
    if snap.get("password_visible"):
        return "CJ is asking for a password"
    return None


def check_match(shop, row, pay, number, lim, spent_today_usd, need_pay_page=True):
    """Every reason not to offer paying CJ for this order ([] = fine). CJ's order list shows SKUs and the
    postcode on some layouts only; when it doesn't, the total quantity and the first address line must match."""
    problems = []
    if shop.get("cancelled"):
        problems.append(f"order {number} is cancelled or refunded on Shopify")
    want = {}
    for it in shop["items"]:
        want[it["sku"]] = want.get(it["sku"], 0) + it["qty"]
    got = row.get("skus") or {}
    if not got:
        if row.get("qty_total") is None:
            problems.append("couldn't read the quantity on CJ")
        elif row["qty_total"] != sum(want.values()):
            problems.append(f"CJ has {row['qty_total']} item(s) but the customer ordered {sum(want.values())}")
    for sku, qty in (want.items() if got else ()):
        if sku not in got:
            problems.append(f"CJ's order doesn't show the item {sku}")
        elif got[sku] is None:
            problems.append(f"couldn't read the quantity of {sku} on CJ")
        elif got[sku] != qty:
            problems.append(f"CJ has {got[sku]} of {sku} but the customer ordered {qty}")
    extra = [s for s in got if s not in want]
    if extra:
        problems.append(f"CJ's order has items the customer didn't order ({', '.join(extra)})")
    if len(row.get("cj_orders") or []) != 1:
        problems.append("couldn't tell which CJ order this is" if not row.get("cj_orders") else "more than one CJ order matched")
    row_words = _words(row.get("text"))
    if row.get("postcode"):
        if row["postcode"] != shop.get("postcode"):
            problems.append("the delivery postcode on CJ doesn't match Shopify")
    elif not shop.get("street") or _words(shop["street"]) not in row_words:
        problems.append("the delivery address on CJ doesn't match Shopify")
    name = _words(shop.get("name"))
    if not name or f" {name} " not in f" {row_words} ":
        problems.append("the delivery name on CJ doesn't match Shopify")
    amount = pay.get("amount_usd")
    if amount is None and not need_pay_page:
        amount = row.get("total_usd")
    if amount is None:
        problems.append("couldn't read the amount on CJ" + ("'s payment page" if need_pay_page else ""))
    else:
        if pay.get("amount_usd") is not None:
            if len(pay.get("orders") or []) != 1:
                problems.append("CJ's payment page isn't for exactly one order")
            if row.get("total_usd") is None or abs(row["total_usd"] - amount) > 0.005:
                problems.append(f"CJ's payment page says ${amount:.2f} but the order list says "
                                + (f"${row['total_usd']:.2f}" if row.get("total_usd") is not None else "nothing"))
        if amount <= 0:
            problems.append("CJ's amount is zero")
        if amount > lim["max_per_order_usd"]:
            problems.append(f"${amount:.2f} is over the ${lim['max_per_order_usd']:.0f} limit per order")
        if spent_today_usd + amount > lim["max_per_day_usd"]:
            problems.append(f"paying ${amount:.2f} would go over the ${lim['max_per_day_usd']:.0f} limit for today "
                            f"(${spent_today_usd:.2f} already)")
        if amount * lim["usd_to_gbp"] >= shop["paid_gbp"]:
            problems.append(f"CJ's ${amount:.2f} isn't less than the £{shop['paid_gbp']:.2f} the customer paid")
    return problems


def summary_text(number, shop, amount_usd, profit_gbp):
    items = ", ".join(f"{it['qty']} × {it['title']}" for it in shop["items"])
    initial = (shop["name"] or "?")[0]
    who = f"{initial} {shop['name'].split()[-1]}" if shop.get("name") else "the customer"
    text = f"Pay CJ ${amount_usd:.2f} for order {number}: {items}, to {who}, {shop.get('town') or ''}."
    if profit_gbp is not None:
        text += f" You make about £{max(profit_gbp, 0):.2f}."
    return text


# ------------------------------------------------------------------ the requests
SCHEMA = """
CREATE TABLE IF NOT EXISTS cj_pay (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_order TEXT NOT NULL,          -- '#1001'
    shopify_id TEXT,
    cj_order TEXT,
    pay_order TEXT,                    -- the order number CJ's payment page shows
    pay_url TEXT,
    amount_usd REAL NOT NULL,
    paid_gbp REAL,
    summary TEXT NOT NULL,
    check_json TEXT,                   -- what was matched (items, postcode, name) to re-check before paying
    nonce TEXT NOT NULL,               -- one-off code: a phone answer must carry it
    status TEXT NOT NULL,              -- offered | approved | paying | paid | failed | needs_danny | unclear | blocked | rejected | expired
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_at REAL,
    decided_by TEXT,                   -- window | phone
    pay_clicked_at REAL,
    finished_at REAL,
    result TEXT,
    folder TEXT,
    tracking TEXT,
    shipped_told INTEGER DEFAULT 0,
    method TEXT DEFAULT 'wallet'       -- wallet (Jarvis pays on Danny's tap) | handoff (Danny pays on CJ himself)
);
CREATE TABLE IF NOT EXISTS cj_pay_seen (
    shop_order TEXT PRIMARY KEY,
    last_try REAL,
    outcome TEXT,                      -- offered | already_paid | blocked | problem | paid
    detail TEXT
);
"""
OPEN = ("offered", "approved", "paying")
SPENDING = ("approved", "paying", "paid", "unclear", "needs_danny")     # counted against the daily cap
FINAL_FOR_ORDER = ("paying", "paid", "unclear", "needs_danny")          # never offered again automatically


class PayStore:
    def __init__(self, path=None):
        self.path = path or os.path.join(config.DATA_DIR, "cj_pay.db")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            if "method" not in [row["name"] for row in c.execute("PRAGMA table_info(cj_pay)")]:
                c.execute("ALTER TABLE cj_pay ADD COLUMN method TEXT DEFAULT 'wallet'")

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, rid):
        with self._conn() as c:
            row = c.execute("SELECT * FROM cj_pay WHERE id=?", (int(rid),)).fetchone()
        return dict(row) if row else None

    def for_order(self, number):
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM cj_pay WHERE shop_order=? ORDER BY id", (number,)).fetchall()]

    def recent(self, limit=20):
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM cj_pay ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]

    def offered(self, now=None):
        self.expire(now)
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM cj_pay WHERE status='offered' ORDER BY id").fetchall()]

    def create(self, **fields):
        now = fields.pop("now", None) or time.time()
        lasts = HANDOFF_SECONDS if fields.get("method") == "handoff" else OFFER_SECONDS
        with self.lock, self._conn() as c:
            busy = c.execute(f"SELECT id FROM cj_pay WHERE shop_order=? AND status IN ({','.join('?' * len(OPEN + FINAL_FOR_ORDER))})",
                             (fields["shop_order"], *OPEN, *FINAL_FOR_ORDER)).fetchone()
            if busy:
                raise ValueError(f"Order {fields['shop_order']} already has a payment request (#{busy['id']}).")
            cols = dict(fields, nonce=secrets.token_urlsafe(12), status="offered", created_at=now, expires_at=now + lasts)
            cur = c.execute(f"INSERT INTO cj_pay ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(cols.values()))
            return cur.lastrowid

    def expire(self, now=None):
        now = now or time.time()
        with self.lock, self._conn() as c:
            c.execute("UPDATE cj_pay SET status='expired', finished_at=? WHERE status='offered' AND expires_at<=?", (now, now))

    def approve(self, rid, by, nonce=None, amount_usd=None, now=None):
        """Danny's tap. offered -> approved in ONE step, so a second tap (or a second device) does nothing."""
        now = now or time.time()
        self.expire(now)
        r = self.get(rid)
        if not r:
            raise ValueError("No such payment request.")
        if r["status"] != "offered":
            raise ValueError(f"That payment request is already {r['status']}; nothing more will be paid.")
        if r.get("method") == "handoff":
            raise ValueError("This one needs your card's security code, so you pay it on CJ's page yourself; I'll check it went through.")
        if nonce is not None and not secrets.compare_digest(str(nonce), r["nonce"]):
            raise PermissionError("That approval doesn't match this payment request.")
        if amount_usd is not None and abs(float(amount_usd) - r["amount_usd"]) > 0.005:
            raise PermissionError("The amount approved doesn't match this payment request.")
        with self.lock, self._conn() as c:
            n = c.execute("UPDATE cj_pay SET status='approved', decided_at=?, decided_by=? WHERE id=? AND status='offered' AND expires_at>?",
                          (now, by, int(rid), now)).rowcount
        if n != 1:
            raise ValueError("That payment request was already answered or has expired.")
        return self.get(rid)

    def reject(self, rid, by, nonce=None, now=None):
        now = now or time.time()
        r = self.get(rid)
        if not r:
            raise ValueError("No such payment request.")
        if nonce is not None and not secrets.compare_digest(str(nonce), r["nonce"]):
            raise PermissionError("That answer doesn't match this payment request.")
        with self.lock, self._conn() as c:
            n = c.execute("UPDATE cj_pay SET status='rejected', decided_at=?, decided_by=?, finished_at=? WHERE id=? AND status='offered'",
                          (now, by, now, int(rid))).rowcount
        if n != 1:
            raise ValueError(f"That payment request is already {r['status']}.")
        return self.get(rid)

    def start_paying(self, rid):
        """approved -> paying, once. Only the worker that wins this step may touch CJ's Pay button."""
        with self.lock, self._conn() as c:
            return c.execute("UPDATE cj_pay SET status='paying' WHERE id=? AND status='approved'", (int(rid),)).rowcount == 1

    def update(self, rid, **fields):
        with self.lock, self._conn() as c:
            c.execute(f"UPDATE cj_pay SET {','.join(k + '=?' for k in fields)} WHERE id=?", (*fields.values(), int(rid)))
        return self.get(rid)

    def mark_paid_by_danny(self, rid, result):
        """A hand-off order Danny paid on CJ himself (seen on CJ's list). Only from 'offered', once."""
        now = time.time()
        with self.lock, self._conn() as c:
            return c.execute("UPDATE cj_pay SET status='paid', decided_at=?, decided_by='danny_on_cj', finished_at=?, result=? "
                             "WHERE id=? AND status='offered' AND method='handoff'", (now, now, result, int(rid))).rowcount == 1

    def finish(self, rid, status, result):
        return self.update(rid, status=status, result=redact(result)[:1000], finished_at=time.time())

    def spent_today(self, now=None, exclude=None):
        now = now or time.time()
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        with self._conn() as c:
            rows = c.execute(f"SELECT id, amount_usd FROM cj_pay WHERE status IN ({','.join('?' * len(SPENDING))}) AND decided_at>=?",
                             (*SPENDING, midnight)).fetchall()
        return round(sum(r["amount_usd"] for r in rows if r["id"] != exclude), 2)

    def interrupted(self):
        """Requests that were mid-payment when Jarvis stopped: nobody knows if CJ took the money."""
        with self.lock, self._conn() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM cj_pay WHERE status IN ('approved','paying')").fetchall()]
            for r in rows:
                status = "unclear" if r["status"] == "paying" else "expired"
                c.execute("UPDATE cj_pay SET status=?, finished_at=?, result=? WHERE id=?",
                          (status, time.time(), "Jarvis stopped while this was being paid; check CJ yourself."
                           if status == "unclear" else "Jarvis stopped before paying; nothing was paid.", r["id"]))
        return rows

    # -- what has been looked at, so the watcher doesn't redo it every ten minutes
    def seen(self, number):
        with self._conn() as c:
            row = c.execute("SELECT * FROM cj_pay_seen WHERE shop_order=?", (number,)).fetchone()
        return dict(row) if row else None

    def mark_seen(self, number, outcome, detail=""):
        with self.lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO cj_pay_seen (shop_order, last_try, outcome, detail) VALUES (?,?,?,?)",
                      (number, time.time(), outcome, redact(detail)[:600]))


def public(r):
    """A request as the window and the phone see it (the nonce goes to the phone: its answer must carry it)."""
    keys = ("id", "shop_order", "cj_order", "amount_usd", "summary", "status", "created_at", "expires_at", "decided_at",
            "decided_by", "finished_at", "result", "tracking", "nonce", "method", "pay_url")
    out = {k: r.get(k) for k in keys}
    if r.get("status") != "offered":
        out.pop("nonce")
    if r.get("method") != "handoff" or r.get("status") != "offered":
        out.pop("pay_url")
    return out


# ------------------------------------------------------------------ the CJ browser driver
SNAP_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && cs.visibility !== 'hidden' && cs.display !== 'none' && r.bottom > 0 && r.right > 0; };
  const t = s => String(s || '').trim().replace(/\s+/g, ' ');
  const inputs = [...document.querySelectorAll('input, textarea, select, [contenteditable=true]')].filter(vis).slice(0, 80).map(c => {
    const labelEl = c.id ? document.querySelector('label[for="' + CSS.escape(c.id) + '"]') : c.closest('label');
    return {tag: c.tagName.toLowerCase(), type: (c.getAttribute('type') || '').toLowerCase(), name: c.getAttribute('name') || '',
            id: c.id || '', placeholder: c.getAttribute('placeholder') || '', aria: t(c.getAttribute('aria-label')).slice(0, 80),
            autocomplete: (c.getAttribute('autocomplete') || '').toLowerCase(), field_label: labelEl ? t(labelEl.innerText).slice(0, 80) : '',
            checked: c.type === 'checkbox' ? !!c.checked : null};   // never the value
  });
  const frames = [...document.querySelectorAll('iframe')].filter(vis).map(f => f.src || '').slice(0, 30);
  return {url: location.href, title: document.title, text: (document.body ? document.body.innerText : '').slice(0, 30000),
          inputs: inputs, frames: frames,
          password_visible: [...document.querySelectorAll('input[type=password]')].some(vis)};
}"""

# One order's row: walk up from the element showing '#1001' to the smallest box that also shows money
# and an item SKU or a CJ order number, never crossing into a box that shows another order's number.
ROW_JS = r"""([orderRe, mark, labelRe]) => {
  const want = new RegExp(orderRe), other = /#\d{4,}(?!\d)/g;
  const money = /(\$|USD|GBP|£)\s?\d/, id = /\b(DP|CJ)[A-Z]{0,3}\d{12,}\b|\bCJ[A-Z]{2,}\d+/;
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const single = el => new Set((el.innerText || '').match(other) || []).size <= 1;
  const hits = [...document.querySelectorAll('body *')].filter(el => vis(el)
      && [...el.childNodes].some(n => n.nodeType === 3 && want.test(n.textContent || '')));
  const rows = [];
  for (const h of hits) {
    let el = h;
    while (el && el !== document.body && single(el)) {
      const text = el.innerText || '';
      if (money.test(text) && id.test(text)) break;
      el = el.parentElement;
    }
    if (el && el !== document.body && single(el) && !rows.includes(el)) rows.push(el);
  }
  const outer = rows.filter(r => !rows.some(o => o !== r && o.contains(r)));
  if (!labelRe) return {rows: outer.map(r => r.innerText.slice(0, 8000))};
  if (outer.length !== 1) return {ok: false, why: outer.length ? 'the order shows up more than once' : "couldn't find the order's row"};
  const label = new RegExp(labelRe, 'i');
  let scope = outer[0];
  for (let up = 0; up < 3 && scope; up++) {
    const found = [...scope.querySelectorAll('button, a, [role=button], span, div')].filter(b => vis(b)
        && label.test((b.innerText || '').trim()) && ![...b.children].some(c => label.test((c.innerText || '').trim())));
    if (found.length === 1) { found[0].setAttribute('data-cjpay', mark); return {ok: true, label: found[0].innerText.trim()}; }
    if (found.length > 1) return {ok: false, why: 'more than one matching button in the row'};
    const parent = scope.parentElement;
    if (!parent || parent === document.body || !single(parent)) break;
    scope = parent;
  }
  return {ok: false, why: "no such button in the order's row"};
}"""

TERMS_JS = r"""(mark) => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
  const boxes = [...document.querySelectorAll('input[type=checkbox], [role=checkbox], .el-checkbox, .ant-checkbox-wrapper')].filter(vis);
  const near = b => { let el = b; for (let i = 0; i < 4 && el; i++, el = el.parentElement) {
      if (/(agree|terms|read and|have read|policy)/i.test(el.innerText || '')) return true; } return false; };
  const terms = boxes.filter(near);
  const roots = terms.filter(b => !terms.some(o => o !== b && o.contains(b)));    // the outer wrapper, not its hidden input
  if (roots.length !== 1) return {ok: false, why: roots.length ? 'more than one terms box' : "couldn't find the terms box"};
  const b = roots[0];
  const input = b.matches('input') ? b : b.querySelector('input[type=checkbox]');
  const checked = input ? input.checked : (b.getAttribute('aria-checked') === 'true' || /is-checked|checked/.test(b.className));
  b.setAttribute('data-cjpay', mark);
  return {ok: true, checked: checked};
}"""

PAY_BUTTON_JS = r"""([mark, labelRe]) => {
  const vis = el => { const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    return r.width > 2 && r.height > 2 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const label = new RegExp(labelRe, 'i');
  const found = [...document.querySelectorAll('button, [role=button], a, input[type=submit]')].filter(b => vis(b)
      && label.test((b.innerText || b.value || '').trim()));
  if (found.length !== 1) return {ok: false, why: found.length ? 'more than one Pay button' : "couldn't find the Pay button"};
  if (found[0].disabled || found[0].getAttribute('aria-disabled') === 'true') return {ok: false, why: 'the Pay button is greyed out'};
  found[0].setAttribute('data-cjpay', mark);
  return {ok: true, label: (found[0].innerText || found[0].value || '').trim()};
}"""
PAY_LABEL = r"^\s*(pay|pay now)\s*$"


class CJBrowser:
    """Jarvis's own browser, limited to what paying needs. Deliberately has NO way to type or fill anything."""

    def __init__(self, jb=None, sleep=time.sleep):
        self._jb = jb
        self.sleep = sleep

    @property
    def jb(self):
        if self._jb is None:
            import jarvis_browser
            self._jb = jarvis_browser
        return self._jb

    def hold(self):
        return self.jb.exclusive(wait=300)

    def _run(self, fn, timeout=90):
        return self.jb._worker.run(fn, timeout=timeout)

    def _snap(self, page):
        snap = page.evaluate(SNAP_JS)
        snap["tabs"] = [p.url for p in page.context.pages]
        return snap

    def snapshot(self):
        return self._run(self._snap)

    def open(self, url, settle=6.0):
        def act(page):
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            self.sleep(settle)
            return self._snap(page)
        return self._run(act, timeout=90)

    def click_tab(self, name):
        def act(page):
            loc = page.get_by_text(re.compile(r"^\s*" + re.escape(name) + r"\s*(\(\s*\d+\s*\))?\s*$", re.I))
            if not loc.count():
                return None
            loc.first.click(timeout=10000)
            self.sleep(4)
            return self._snap(page)
        return self._run(act)

    def row_texts(self, number):
        return self._run(lambda page: page.evaluate(ROW_JS, [order_re(number).pattern, "", ""])["rows"])

    def click_row_button(self, number, label_re):
        """Clicks the ONE button matching label_re inside this order's row. -> (ok, label or why, snapshot)."""
        mark = secrets.token_hex(6)

        def act(page):
            out = page.evaluate(ROW_JS, [order_re(number).pattern, mark, label_re])
            if not out.get("ok"):
                return False, out.get("why"), self._snap(page)
            page.locator(f'[data-cjpay="{mark}"]').first.click(timeout=10000)
            self.sleep(6)
            return True, out.get("label"), self._snap(self._current(page))
        return self._run(act)

    def _current(self, page):
        """A click that opened a tab: use it (and release it if the debugger left it frozen)."""
        pages = page.context.pages
        if len(pages) > 1 and pages[-1] is not page:
            new = pages[-1]
            try:
                cdp = page.context.new_cdp_session(new)
                cdp.send("Runtime.runIfWaitingForDebugger")
                cdp.detach()
            except Exception:
                pass
            self.sleep(3)
            self.jb._worker._page = new
            return new
        return page

    def choose_method(self, label=WALLET_LABEL):
        def act(page):
            loc = page.get_by_text(re.compile(r"^\s*" + re.escape(label) + r"\s*$", re.I))
            if loc.count() != 1:
                return None
            loc.first.click(timeout=10000)
            self.sleep(3)
            return self._snap(page)
        return self._run(act)

    def tick_terms(self):
        mark = secrets.token_hex(6)

        def act(page):
            out = page.evaluate(TERMS_JS, mark)
            if not out.get("ok"):
                return False, out.get("why"), self._snap(page)
            if not out.get("checked"):
                page.locator(f'[data-cjpay="{mark}"]').first.click(timeout=10000)
                self.sleep(1)
                out = page.evaluate(TERMS_JS, mark)
            return bool(out.get("checked")), ("ticked" if out.get("checked") else "the terms box didn't tick"), self._snap(page)
        return self._run(act)

    def press_pay(self):
        """Presses CJ's PAY button once. -> (pressed, label or why)."""
        mark = secrets.token_hex(6)

        def act(page):
            out = page.evaluate(PAY_BUTTON_JS, [mark, PAY_LABEL])
            if not out.get("ok"):
                return False, out.get("why")
            page.locator(f'[data-cjpay="{mark}"]').first.click(timeout=10000)
            return True, out.get("label")
        return self._run(act)

    def wait_result(self, tabs_before, seconds=40):
        """After PAY: watches the page until it settles on a result (or a bank check), without touching it."""
        deadline = time.time() + seconds
        snap = None
        while time.time() < deadline:
            self.sleep(4)
            snap = self._run(lambda page: self._snap(self._current(page)))
            text = snap.get("text") or ""
            if needs_danny(snap) or DECLINED_RE.search(text) or SUCCESS_RE.search(text) or len(snap.get("tabs") or []) > tabs_before:
                break
        return snap

    def screenshot(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return self._run(lambda page: page.screenshot(path=path, full_page=True, timeout=30000) and path, timeout=60)


def read_shopify(order_id, jb=None):
    """The Shopify admin page for one order, as text (Jarvis's browser is logged in to Shopify)."""
    if jb is None:
        import jarvis_browser as jb
    info = jb.goto(SHOPIFY_ORDER_URL.format(id=int(order_id)), settle=6.0)
    if "accounts.shopify.com" in info.get("url", "") or "/login" in info.get("url", ""):
        raise ValueError("Jarvis's browser is logged out of Shopify")
    return jb.read_text(30000)


# ------------------------------------------------------------------ the work
class Payer:
    def __init__(self, store=None, driver=None, shopify=None, announcer=None, push=None, folder=None,
                 limits_path=None, clock=time.time):
        self.store = store or PayStore()
        self.driver = driver or CJBrowser()
        self.shopify = shopify or read_shopify          # order_id -> page text
        self.announcer = announcer
        self.push = push or (lambda: None)
        self.folder = folder or os.path.join(config.DATA_DIR, "cj_pay")
        self.limits_path = limits_path
        self.clock = clock
        self.busy = threading.Lock()                    # one CJ job at a time in this process
        self.told = set()
        self.after_balance = {}                         # request id -> wallet balance after paying
        self.last_tab_url = CJ_LIST_URL

    # -- small helpers
    def say(self, text, once_key=None):
        if once_key is not None:
            if once_key in self.told:
                return
            self.told.add(once_key)
        if self.announcer:
            try:
                self.announcer.urgent(text)
            except Exception:
                pass

    def _job_folder(self, number):
        path = os.path.join(self.folder, time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"\D", "", number))
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def audit(folder, **entry):
        entry = {k: (redact(v) if isinstance(v, str) else v) for k, v in entry.items()}
        entry["ts"] = time.time()
        with open(os.path.join(folder, "audit.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def _shot(self, folder, name):
        try:
            return self.driver.screenshot(os.path.join(folder, f"{len(os.listdir(folder)):02d}-{name}.png"))
        except Exception as error:
            return f"(screenshot failed: {error})"

    def find_on_cj(self, number):
        """-> (tab, rows) for the first CJ list tab showing this order, else (None, [])."""
        for tab in UNPAID_TABS + PAID_TABS:
            snap = self.driver.click_tab(tab)
            if snap is None:
                continue
            rows = self.driver.row_texts(number)
            if rows:
                self.last_tab_url = snap.get("url") or CJ_LIST_URL
                return tab, rows
        return None, []

    def _open_list(self):
        snap = self.driver.open(CJ_LIST_URL)
        url = (snap or {}).get("url") or ""
        if "login" in url.lower():
            return "Jarvis's browser is logged out of CJdropshipping"
        why = needs_danny(snap)
        if why:
            return why
        return None

    # -- 1. prepare a request (no money moves)
    def wanted(self, order, now=None):
        """Should the watcher look at this Shopify order now?"""
        now = now or self.clock()
        if (order.get("payment") or "").lower() != "paid" or not order.get("id"):
            return False
        if (order.get("fulfilment") or "").lower() not in ("unfulfilled", "in progress", "", "on hold"):
            return False
        number = order["number"]
        reqs = self.store.for_order(number)
        if any(r["status"] in OPEN + FINAL_FOR_ORDER for r in reqs):
            return False
        seen = self.store.seen(number)
        if seen:
            if seen["outcome"] in ("already_paid", "paid"):
                return False
            wait = REOFFER_SECONDS if seen["outcome"] == "offered" else RETRY_PREPARE_SECONDS
            if now - (seen["last_try"] or 0) < wait:
                return False
        return True

    def prepare(self, order, force=False):
        """Gets one Shopify order ready to pay on CJ and offers it to Danny. Never pays.
        order: {number, id, payment, fulfilment} from the Shopify orders list."""
        number = order["number"]
        if not limits(self.limits_path)["enabled"]:
            return {"ok": False, "why": "paying CJ from Jarvis is switched off in the settings file"}
        if not force and not self.wanted(order):
            return {"ok": False, "why": "not needed right now"}
        if any(r["status"] in OPEN + FINAL_FOR_ORDER for r in self.store.for_order(number)):
            return {"ok": False, "why": f"order {number} already has a payment request"}
        if (self.store.seen(number) or {}).get("outcome") in ("already_paid", "paid"):
            return {"ok": False, "why": f"order {number} is already paid on CJ"}
        if not self.busy.acquire(blocking=False):
            return {"ok": False, "why": "Jarvis is already busy with CJ"}
        folder = self._job_folder(number)
        try:
            with self.driver.hold():
                out = self._prepare(order, folder)
        except Exception as error:
            out = {"ok": False, "outcome": "problem", "why": f"something went wrong: {str(error).splitlines()[0][:200]}"}
        finally:
            self.busy.release()
        self.audit(folder, step="prepare_result", **{k: v for k, v in out.items() if k != "request"})
        self.store.mark_seen(number, out.get("outcome") or ("offered" if out.get("ok") else "problem"), out.get("why") or "")
        if out.get("ok"):
            r = out["request"]
            said = r["summary"].replace(" × ", " of the ")
            if r["method"] == "wallet":
                self.say(f"New order {number}, sir. {said} It can come from your CJ wallet: tap Pay in the Jarvis window "
                         f"or on your phone, or ignore it and nothing is paid.")
            else:
                where = "" if "/mine/payment" in (r.get("pay_url") or "") else f", find order {number} there"
                self.say(f"New order {number}, sir. {said} It all checks out. CJ needs your card's security code, so tap "
                         f"Open CJ in the Jarvis window or on your phone{where}, pay it with the card ending "
                         f"{limits(self.limits_path)['card_last4']} and type the code. I'll check it went through.")
            self.push()
        elif out.get("outcome") == "already_paid":
            pass
        else:
            what_to_do = "You can pay it on CJ yourself."
            if "logged out of CJ" in out["why"]:
                what_to_do = ("Please log in to CJ once in Jarvis's browser window (the green Shopify button signs you in), "
                              "and I'll offer it again within half an hour.")
            elif "human check" in out["why"]:
                what_to_do = "Please pass the check yourself in Jarvis's browser window, and I'll try again within half an hour."
            self.say(f"Order {number} came in, sir, but I haven't offered to pay CJ for it: {out['why']}. {what_to_do}",
                     once_key=("prep", number, out.get("why")))
        return out

    def _prepare(self, order, folder):
        number = order["number"]
        self.audit(folder, step="start", order=number, shopify_id=order.get("id"))
        try:
            shop = parse_shopify_order(self.shopify(order["id"]))
        except ValueError as error:
            return {"ok": False, "outcome": "problem", "why": str(error)}
        self.audit(folder, step="shopify", items=shop["items"], postcode=shop["postcode"], paid_gbp=shop["paid_gbp"],
                   cancelled=shop["cancelled"])
        if shop["cancelled"]:
            return {"ok": False, "outcome": "blocked", "why": f"order {number} is cancelled or refunded on Shopify"}

        problem = self._open_list()
        if problem:
            return {"ok": False, "outcome": "problem", "why": problem}
        tab, rows = self.find_on_cj(number)
        self.audit(folder, step="cj_found", tab=tab, rows=len(rows))
        if tab is None:
            return {"ok": False, "outcome": "problem", "why": f"I can't find order {number} on CJ's order list yet"}
        if tab in PAID_TABS:
            return {"ok": False, "outcome": "already_paid", "why": f"CJ already shows order {number} as {tab.lower()}"}
        if len(rows) != 1:
            return {"ok": False, "outcome": "problem", "why": f"order {number} shows up more than once on CJ"}

        moved = True
        if tab == "Imported":
            ok, label, snap = self.driver.click_row_button(number, MOVE_ON_LABEL)
            self.audit(folder, step="add_to_orders_picking", ok=ok, label=label, url=(snap or {}).get("url"))
            if ok:
                tab, rows = self.find_on_cj(number)
                if tab in PAID_TABS:
                    return {"ok": False, "outcome": "already_paid", "why": f"CJ already shows order {number} as {tab.lower()}"}
                if tab is None or len(rows) != 1:
                    return {"ok": False, "outcome": "problem", "why": f"I lost track of order {number} on CJ after moving it on"}
            moved = ok and tab != "Imported"
        list_url = self.last_tab_url

        row = parse_cj_row(rows[0])
        self._shot(folder, "cj-order")
        self.audit(folder, step="cj_row", tab=tab, cj_orders=row["cj_orders"], skus=row["skus"], qty_total=row["qty_total"],
                   total_usd=row["total_usd"], postcode_shown=bool(row["postcode"]))

        # CJ's payment page (needed for a wallet payment; for a hand-off, the list will do if it can't be opened).
        pay, pay_url = {"orders": [], "amount_usd": None}, list_url
        if moved:
            ok, label, snap = self.driver.click_row_button(number, TO_PAYMENT_LABEL)
            self.audit(folder, step="open_payment", ok=ok, label=label, url=(snap or {}).get("url"))
            from .job_browser import page_blocked
            if ok and page_blocked(snap):
                return {"ok": False, "outcome": "problem", "why": "CJ is showing a human check (\"are you a robot\")"}
            if ok and "/mine/payment" in (snap.get("url") or ""):
                # The card tile may already show its CVV box: that's for Danny (hand-off). Everything is checked
                # again, card fields included, before any wallet payment.
                pay = parse_payment_page(snap.get("text"))
                pay_url = snap["url"]
                self._shot(folder, "cj-payment-page")
                self.audit(folder, step="payment_page", url=pay_url, orders=pay["orders"], amount_usd=pay["amount_usd"])
            elif ok:
                self._shot(folder, "unexpected-page")

        lim = limits(self.limits_path)
        problems = check_match(shop, row, pay, number, lim, self.store.spent_today(), need_pay_page=False)
        if problems:
            self.audit(folder, step="checks_failed", problems=problems)
            return {"ok": False, "outcome": "blocked", "why": "; ".join(problems)}

        amount = pay["amount_usd"] if pay["amount_usd"] is not None else row["total_usd"]
        balance = self.wallet_balance()
        on_pay_page = "/mine/payment" in pay_url
        method = "wallet" if on_pay_page and balance is not None and balance >= amount else "handoff"
        self.audit(folder, step="wallet", balance_usd=balance, method=method)
        net = order.get("net_gbp")
        profit = (net if net is not None else shop["paid_gbp"] * 0.97) - amount * lim["usd_to_gbp"]
        summary = summary_text(number, shop, amount, profit)
        check = {"items": shop["items"], "postcode": shop["postcode"], "surname": _surname(shop["name"]),
                 "paid_gbp": shop["paid_gbp"], "skus": row["skus"]}
        rid = self.store.create(shop_order=number, shopify_id=str(order["id"]), cj_order=row["cj_orders"][0],
                                pay_order=(pay["orders"] or [""])[0], pay_url=pay_url, amount_usd=amount, paid_gbp=shop["paid_gbp"],
                                summary=summary, check_json=json.dumps(check), folder=folder, method=method)
        self.audit(folder, step="offered", request=rid, amount_usd=amount, summary=summary, method=method)
        return {"ok": True, "outcome": "offered", "request": self.store.get(rid)}

    # -- 2. Danny's tap
    def approve(self, rid, by, nonce=None, amount_usd=None, background=True):
        r = self.store.approve(rid, by, nonce=nonce, amount_usd=amount_usd)
        self.audit(r["folder"], step="approved", request=rid, by=by, amount_usd=r["amount_usd"])
        if background:
            threading.Thread(target=self.pay, args=(rid,), daemon=True, name=f"jarvis-cjpay-{rid}").start()
        else:
            self.pay(rid)
        return r

    def reject(self, rid, by, nonce=None):
        r = self.store.reject(rid, by, nonce=nonce)
        self.audit(r["folder"], step="rejected", request=rid, by=by)
        self.store.mark_seen(r["shop_order"], "offered", "turned down")
        self.push()
        return r

    # -- 3. paying (only ever reached through approve())
    def pay(self, rid):
        if not self.store.start_paying(rid):
            return self.store.get(rid)                  # a second tap, or already handled: nothing happens
        r = self.store.get(rid)
        folder = r["folder"]
        with self.busy:
            try:
                with self.driver.hold():
                    status, why = self._pay(r, folder)
            except Exception as error:
                st = self.store.get(rid)
                status = "unclear" if st.get("pay_clicked_at") else "blocked"
                why = f"something went wrong: {str(error).splitlines()[0][:200]}"
        r = self.store.finish(rid, status, why)
        self.audit(folder, step="finished", request=rid, status=status, result=why)
        self.store.mark_seen(r["shop_order"], "paid" if status == "paid" else ("problem" if status == "blocked" else status), why)
        number, amount = r["shop_order"], r["amount_usd"]
        if status == "paid":
            left = self.after_balance.pop(rid, None)
            low = limits(self.limits_path)["low_wallet_usd"]
            note = f" Your CJ wallet is down to ${left:.2f}." if left is not None and left < low else ""
            self.say(f"Done, sir: CJ is paid ${amount:.2f} for order {number} from your wallet, and they'll ship it. "
                     f"I'll tell you when it's dispatched.{note}")
        elif status == "blocked":
            self.say(f"I didn't pay CJ for order {number}, sir: {why}. Nothing was charged.")
        elif status == "failed":
            self.say(f"CJ's payment for order {number} didn't go through, sir: {why}. I won't try again; you can pay it on CJ yourself.")
        elif status == "needs_danny":
            self.say(f"I pressed Pay for order {number}, sir, but {why}. Please finish it yourself in Jarvis's browser window "
                     f"(it's on CJ's payment page), or pay it on CJ. I won't press Pay again.")
        else:
            self.say(f"I pressed Pay for order {number}, sir, but I can't tell whether CJ took the money: {why}. "
                     f"Please check the order on CJ yourself. I won't press Pay again.")
        self.push()
        return r

    def _pay(self, r, folder):
        number, amount = r["shop_order"], r["amount_usd"]
        check = json.loads(r["check_json"] or "{}")
        lim = limits(self.limits_path)
        if not lim["enabled"]:
            return "blocked", "paying CJ from Jarvis is switched off in the settings file"
        if amount > lim["max_per_order_usd"]:
            return "blocked", f"${amount:.2f} is over the ${lim['max_per_order_usd']:.0f} limit per order"
        spent = self.store.spent_today(exclude=r["id"])
        if spent + amount > lim["max_per_day_usd"]:
            return "blocked", f"it would go over the ${lim['max_per_day_usd']:.0f} limit for today (${spent:.2f} already)"

        # Shopify again: still paid, same items, same address.
        try:
            shop = parse_shopify_order(self.shopify(r["shopify_id"]))
        except ValueError as error:
            return "blocked", str(error)
        if shop["cancelled"]:
            return "blocked", "the order has been cancelled or refunded on Shopify since"
        if shop["items"] != check.get("items") or shop["postcode"] != check.get("postcode"):
            return "blocked", "the Shopify order has changed since I offered it"
        self.audit(folder, step="recheck_shopify", ok=True)

        # CJ's list again: the order is still unpaid, with the same items and total.
        problem = self._open_list()
        if problem:
            return "blocked", problem
        tab, rows = self.find_on_cj(number)
        self.audit(folder, step="status_before", tab=tab)
        if tab in PAID_TABS:
            return "blocked", f"CJ already shows it as {tab.lower()}, so it looks paid already"
        if tab is None or len(rows) != 1:
            return "blocked", "I can't find the order on CJ's unpaid lists any more"
        row = parse_cj_row(rows[0])
        if row["cj_orders"] != [r["cj_order"]] or row["skus"] != check.get("skus") or row["total_usd"] is None \
                or abs(row["total_usd"] - amount) > 0.005:
            return "blocked", "CJ's order has changed since I offered it"

        # The wallet still covers it (it may have paid something else since).
        if r.get("method") != "wallet":
            return "blocked", "this order is one you pay on CJ yourself"
        balance = self.wallet_balance()
        if balance is None:
            return "blocked", "I couldn't read your CJ wallet balance"
        if balance < amount:
            return "blocked", f"your CJ wallet only has ${balance:.2f} now"
        self.audit(folder, step="wallet_before", balance_usd=balance)

        # The payment page: same order, same amount, nothing asking for card details.
        snap = self.driver.open(r["pay_url"])
        pay = parse_payment_page((snap or {}).get("text"))
        if "/mine/payment" not in (snap or {}).get("url", "") or pay["orders"] != [r["pay_order"]] \
                or pay["amount_usd"] is None or abs(pay["amount_usd"] - amount) > 0.005:
            self._shot(folder, "payment-page-changed")
            return "blocked", "CJ's payment page doesn't show the same order and amount any more"
        snap = self.driver.choose_method(WALLET_LABEL)
        if snap is None:
            return "blocked", "I couldn't find 'Pay with CJ wallet' on CJ's payment page"
        why = needs_danny(snap)
        if why:
            self._shot(folder, "details-asked")
            return "blocked", why + " (Jarvis never types those)"
        if INSUFFICIENT_RE.search(snap.get("text") or ""):
            self._shot(folder, "wallet-short")
            return "blocked", "CJ says your wallet balance isn't enough for this order"
        ok, what, snap = self.driver.tick_terms()
        if not ok:
            return "blocked", f"I couldn't tick CJ's terms box ({what})"
        final = parse_payment_page(snap.get("text"))
        why = needs_danny(snap)
        if why or final["amount_usd"] is None or abs(final["amount_usd"] - amount) > 0.005 or final["orders"] != [r["pay_order"]]:
            return "blocked", why or "the amount on CJ's page changed"
        self._shot(folder, "before-pay")
        self.audit(folder, step="pressing_pay", request=r["id"], amount_usd=amount, cj_order=r["cj_order"], url=snap.get("url"))

        # The one press. From here on nothing is retried.
        self.store.update(r["id"], pay_clicked_at=time.time())
        pressed, label = self.driver.press_pay()
        if not pressed:
            self.store.update(r["id"], pay_clicked_at=None)
            return "blocked", f"I didn't press Pay: {label}"
        self.audit(folder, step="pressed_pay", label=label)
        tabs_before = len(snap.get("tabs") or [])
        after = self.driver.wait_result(tabs_before)
        self._shot(folder, "after-pay")
        text = (after or {}).get("text") or ""
        self.audit(folder, step="after_pay", url=(after or {}).get("url"), tabs=len((after or {}).get("tabs") or []),
                   success_text=bool(SUCCESS_RE.search(text)), declined_text=bool(DECLINED_RE.search(text)))
        why = needs_danny(after)
        if why:
            return "needs_danny", why
        if len((after or {}).get("tabs") or []) > tabs_before:
            return "needs_danny", "CJ opened another window for the payment"
        if DECLINED_RE.search(text) and not SUCCESS_RE.search(text):
            m = DECLINED_RE.search(text)
            return "failed", f"CJ said '{m.group(0)}'"

        # Proof: CJ's order status has to have moved on.
        if self._open_list():
            return "unclear", "I couldn't re-open CJ's order list to check"
        tab, rows = self.find_on_cj(number)
        self._shot(folder, "status-after")
        self.audit(folder, step="status_after", tab=tab)
        if tab in PAID_TABS:
            self.after_balance[r["id"]] = self.wallet_balance()
            return "paid", f"CJ now shows the order as {tab.lower()}"
        return "unclear", (f"CJ still lists it under {tab}" if tab else "I can't find the order on CJ's list now")

    def wallet_balance(self):
        """Danny's CJ wallet balance in dollars (None if it can't be read)."""
        try:
            snap = self.driver.open(WALLET_URL)
        except Exception:
            return None
        if "login" in ((snap or {}).get("url") or "").lower():
            return None
        return parse_wallet((snap or {}).get("text"))

    # -- 3b. hand-off orders: Danny pays on CJ himself; Jarvis watches for it
    def verify_handoffs(self):
        """For each open hand-off: has CJ's list moved it to a paid tab? Then it's paid (once). Never clicks anything."""
        todo = [r for r in self.store.offered() if r.get("method") == "handoff"]
        if not todo or not self.busy.acquire(blocking=False):
            return []
        paid = []
        try:
            with self.driver.hold():
                if self._open_list():
                    return []
                for r in todo:
                    tab, rows = self.find_on_cj(r["shop_order"])
                    self.audit(r["folder"], step="handoff_check", tab=tab)
                    if tab in PAID_TABS and self.store.mark_paid_by_danny(r["id"], f"you paid it on CJ; CJ shows it as {tab.lower()}"):
                        self._shot(r["folder"], "paid-by-danny")
                        self.audit(r["folder"], step="finished", request=r["id"], status="paid", by="danny_on_cj")
                        self.store.mark_seen(r["shop_order"], "paid", "Danny paid it on CJ")
                        self.say(f"CJ shows order {r['shop_order']} as paid, sir. I'll tell you when it's dispatched.")
                        paid.append(r["shop_order"])
        finally:
            self.busy.release()
        if paid:
            self.push()
        return paid

    # -- 4. later: dispatched?
    def check_shipping(self):
        """For paid orders not yet reported: has CJ dispatched it? Says so once, with the tracking number."""
        todo = [r for r in self.store.recent(50) if r["status"] == "paid" and not r["shipped_told"]]
        if not todo or not self.busy.acquire(blocking=False):
            return []
        told = []
        try:
            with self.driver.hold():
                if self._open_list() or self.driver.click_tab("Dispatched") is None:
                    return []
                for r in todo:
                    rows = self.driver.row_texts(r["shop_order"])
                    if len(rows) != 1:
                        continue
                    m = TRACKING_RE.search(rows[0])
                    tracking = m.group(1) if m else ""
                    self.store.update(r["id"], shipped_told=1, tracking=tracking)
                    self.say(f"CJ has dispatched order {r['shop_order']}, sir" + (f": tracking {tracking}." if tracking else "."))
                    told.append(r["shop_order"])
        finally:
            self.busy.release()
        if told:
            self.push()
        return told

    # -- 5. answers from the phone
    def take_phone_answers(self, answers):
        """Answers the phone backend collected. Each must carry the request's one-off code and amount."""
        done = []
        for a in answers or []:
            try:
                rid = int(a.get("id"))
                if a.get("action") == "pay":
                    self.approve(rid, "phone", nonce=str(a.get("nonce") or ""), amount_usd=float(a.get("amount_usd")))
                elif a.get("action") == "reject":
                    self.reject(rid, "phone", nonce=str(a.get("nonce") or ""))
                else:
                    continue
                done.append(rid)
            except (ValueError, PermissionError, TypeError) as error:
                r = self.store.get(a.get("id") or 0) if str(a.get("id") or "").isdigit() else None
                if r and r.get("folder"):
                    self.audit(r["folder"], step="phone_answer_refused", why=str(error))
        return done

    def view(self):
        return {"offers": [public(r) for r in self.store.offered()],
                "recent": [public(r) for r in self.store.recent(10) if r["status"] != "offered"],
                "limits": {k: v for k, v in limits(self.limits_path).items() if k != "usd_to_gbp"},
                "spent_today_usd": self.store.spent_today()}


def describe(view):
    """One spoken-style paragraph for the brain's cj_pay_status tool."""
    parts = []
    for o in view.get("offers") or []:
        if o.get("method") == "handoff":
            parts.append(f"Waiting for Danny to pay it on CJ himself (CJ needs his card's security code): {o['summary']}")
        else:
            parts.append(f"Waiting for Danny's Pay tap (from his CJ wallet): {o['summary']}")
    for r in (view.get("recent") or [])[:5]:
        parts.append(f"Order {r['shop_order']} (${r['amount_usd']:.2f}): {r['status']}" + (f" ({r['result']})" if r.get("result") else "")
                     + (f", tracking {r['tracking']}" if r.get("tracking") else "") + ".")
    lim = view.get("limits") or {}
    parts.append(f"Limits: ${lim.get('max_per_order_usd', 0):.0f} an order, ${lim.get('max_per_day_usd', 0):.0f} a day; "
                 f"${view.get('spent_today_usd', 0):.2f} paid to CJ today.")
    return " ".join(parts) if parts else "No CJ payments yet."
