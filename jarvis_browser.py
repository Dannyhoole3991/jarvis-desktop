"""
Jarvis Browser
==============

Gives Jarvis real browser control the way an agent needs it: he attaches to
a normal Edge window (a dedicated profile Danny logs into once and that then
stays logged in), reads pages as TEXT and as a numbered list of clickable
elements, and acts on elements by number -- instead of taking screenshots
and asking a small vision model what it sees (slow and imprecise).

Why Edge via the debugging port rather than an automation-only browser:
sites like Fiverr watch for automated browsers; this is an ordinary Edge
install that Danny launches/logs into himself, with Playwright merely
attaching to it. A dedicated profile folder is required -- Chromium
browsers ignore the debugging port on the default profile.

Safety rules enforced here (not left to the model):
  - never types into password fields,
  - refuses to click anything that looks irreversible or financial
    (buy / pay / publish / delete / send / submit / withdraw ...) unless the
    caller passes confirmed=True, which only a human-approved path may do,
  - page text is returned as DATA; callers must never treat it as
    instructions.
All Playwright calls run on one dedicated thread, since its sync API is not
thread-safe and Jarvis is heavily multithreaded.
"""

import os
import queue
import re
import socket
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEBUG_PORT = 9333
PROFILE_DIR = os.path.join(HERE, "jarvis_browser_profile")
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

IRREVERSIBLE_RE = re.compile(
    r"\b(buy|pay|purchase|checkout|check out|place order|delete|remove|publish|send|submit|confirm|"
    r"withdraw|transfer|accept|approve|sign out|log out|logout|deactivate|cancel (?:order|subscription))\b",
    re.IGNORECASE,
)

_JS_ELEMENTS = r"""
() => {
  const sel = 'a[href], button, input, textarea, select, [role="button"], [role="link"], [role="tab"], [role="checkbox"], [onclick]';
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || cs.visibility === 'hidden' || cs.display === 'none') continue;
    n += 1;
    el.setAttribute('data-jref', String(n));
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    out.push({ref: n, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || '',
              text: text, placeholder: el.getAttribute('placeholder') || '',
              href: (el.tagName === 'A' ? (el.getAttribute('href') || '').slice(0, 100) : '')});
    if (n >= 120) break;
  }
  return out;
}
"""


def find_edge():
    for path in EDGE_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def _port_open():
    try:
        with socket.create_connection(("127.0.0.1", DEBUG_PORT), timeout=1):
            return True
    except OSError:
        return False


def show_window(title_hint="Fiverr"):
    """Raises the Jarvis browser window (e.g. when Danny has to log in or pass a human check)."""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(New-Object -ComObject WScript.Shell).AppActivate('{title_hint}') | Out-Null"],
            timeout=10, capture_output=True,
        )
    except Exception:
        pass


def launch_browser(start_url="about:blank", visible=False):
    """
    Starts the dedicated Edge window if it isn't already running. Returns (ok, message).
    Minimized unless visible=True, so background checks never throw a window
    at Danny; show_window() raises it only when he has to do something there.
    """
    if _port_open():
        return True, "The Jarvis browser is already open."
    edge = find_edge()
    if not edge:
        return False, "Microsoft Edge isn't installed where I expected."
    os.makedirs(PROFILE_DIR, exist_ok=True)
    args = [
        edge, f"--remote-debugging-port={DEBUG_PORT}", f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run", "--no-default-browser-check",
    ]
    if not visible:
        args.append("--start-minimized")
    args.append(start_url)
    subprocess.Popen(args)
    for _ in range(40):
        if _port_open():
            return True, "Opened the Jarvis browser."
        time.sleep(0.5)
    return False, "The browser didn't start listening in time."


class _Worker:
    def __init__(self):
        self._queue = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._pw = None
        self._page = None

    def _ensure_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True, name="jarvis-browser")
                self._thread.start()

    def _loop(self):
        while True:
            fn, result_q = self._queue.get()
            try:
                result_q.put(("ok", fn()))
            except Exception as error:
                result_q.put(("err", error))

    def _page_obj(self):
        from playwright.sync_api import sync_playwright
        if self._pw is None:
            self._pw = sync_playwright().start()
        try:
            if self._page is not None and not self._page.is_closed():
                return self._page
        except Exception:
            pass
        browser = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        pages = [p for p in context.pages if not p.url.startswith("devtools")]
        self._page = pages[0] if pages else context.new_page()
        return self._page

    def run(self, action, timeout=60):
        if not _port_open():
            ok, message = launch_browser()
            if not ok:
                raise RuntimeError(message)
        self._ensure_thread()
        for attempt in (1, 2):
            result_q = queue.Queue(maxsize=1)
            self._queue.put((lambda: action(self._page_obj()), result_q))
            try:
                status, value = result_q.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError("The browser took too long to respond.")
            if status == "ok":
                return value
            # A crashed/closed tab or stale connection: reconnect once and retry.
            self._page = None
            if attempt == 2:
                raise value


_worker = _Worker()


def goto(url, settle=2.0):
    def action(page):
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(settle)
        return {"url": page.url, "title": page.title()}
    return _worker.run(action, timeout=70)


def read_text(max_chars=6000):
    def action(page):
        text = page.inner_text("body")
        return text[:max_chars]
    return _worker.run(action)


def elements():
    """Numbered list of the visible interactive elements, as compact text lines."""
    def action(page):
        items = page.evaluate(_JS_ELEMENTS)
        lines = []
        for item in items:
            label = item["text"] or item["placeholder"] or item["href"] or "(no label)"
            extra = f" type={item['type']}" if item["type"] else ""
            lines.append(f"[{item['ref']}] {item['tag']}{extra}: {label}")
        return "\n".join(lines)
    return _worker.run(action)


def click(ref, confirmed=False):
    def action(page):
        locator = page.locator(f'[data-jref="{int(ref)}"]').first
        label = (locator.inner_text(timeout=3000) or locator.get_attribute("aria-label") or "").strip()
        if not confirmed and IRREVERSIBLE_RE.search(label):
            return f"BLOCKED: '{label[:60]}' looks like an irreversible or financial action. Ask Danny to confirm first."
        locator.click(timeout=8000)
        time.sleep(1.5)
        return f"Clicked [{ref}] '{label[:60]}'. Page is now: {page.url}"
    return _worker.run(action)


def type_text(ref, text, submit=False):
    def action(page):
        locator = page.locator(f'[data-jref="{int(ref)}"]').first
        if (locator.get_attribute("type") or "").lower() == "password":
            return "BLOCKED: I never type into password fields. Danny has to enter that himself."
        locator.fill(str(text), timeout=8000)
        if submit:
            locator.press("Enter")
            time.sleep(1.5)
        return f"Typed into [{ref}]."
    return _worker.run(action)


def press(key):
    def action(page):
        page.keyboard.press(key)
        time.sleep(1.0)
        return f"Pressed {key}."
    return _worker.run(action)


def current():
    return _worker.run(lambda page: {"url": page.url, "title": page.title()})


# ----------------------------------------------------------------------
# Deterministic skills: no model involved, so they're fast and exact.
# ----------------------------------------------------------------------

_STATUS_RE = re.compile(r"\b(Priority|Active|Incomplete|Late|Delivered|Completed|Cancelled|Starred) \((\d+)\)")
_TIME_RE = re.compile(r"^\d+\s+(?:minute|minutes|hour|hours|day|days|week|weeks|month|months)\b|^(?:just now|yesterday)$", re.I)
_SUSPICIOUS_RE = re.compile(r"https?://|www\.|\.html|\.exe|\.zip|already (?:placed|proceeded|ordered)|order .*placed|review the (?:project|document)|awaiting your approval", re.I)


_BLOCKED_RE = re.compile(r"needs a human touch|are you a robot|are you human|verify you are (?:a )?human|press (?:&|and) hold|captcha|unusual traffic", re.I)


def _looks_blocked(text):
    """True when the site is showing a human-verification page instead of content."""
    return bool(_BLOCKED_RE.search(text[:1200]))


def _looks_logged_out(url, text):
    return "/login" in url or "/join" in url or ("Sign in" in text[:400] and "Join" in text[:400] and "Manage" not in text[:800])


def fiverr_status():
    """
    Real orders, messages and gig stats from Danny's logged-in Fiverr, read
    from the page text. Returns a dict; 'logged_in' False means Danny needs
    to sign in once in the Jarvis browser window.
    """
    result = {"logged_in": True, "blocked": False, "orders": {}, "messages": [], "gigs": [], "error": None}
    try:
        info = goto("https://www.fiverr.com/users/danielhoole/manage_orders", settle=3.0)
        text = read_text(8000)
        if _looks_blocked(text):
            result["blocked"] = True
            return result
        if _looks_logged_out(info["url"], text):
            result["logged_in"] = False
            return result
        result["orders"] = {name: int(n) for name, n in _STATUS_RE.findall(text)}
        if not result["orders"]:
            # Never report "0 orders" for a page we couldn't actually read.
            result["error"] = "I couldn't read the order counts on that page."
            return result

        goto("https://www.fiverr.com/inbox", settle=4.0)
        inbox_raw = read_text(8000)
        if _looks_blocked(inbox_raw):
            result["blocked"] = True
            return result
        inbox = inbox_raw.split("Pick up where you left off")[0]
        lines = [ln.strip() for ln in inbox.splitlines() if ln.strip()]
        start = next((i for i, ln in enumerate(lines) if ln == "All messages"), 0)
        i = start + 1
        while i < len(lines):
            if re.fullmatch(r"[A-Za-z]", lines[i]) and i + 2 < len(lines) and _TIME_RE.match(lines[i + 2]):
                convo = {"sender": lines[i + 1], "preview": "", "when": lines[i + 2], "unread": 0}
                i += 3
                if i < len(lines) and lines[i].isdigit():
                    convo["unread"] = int(lines[i])
                    i += 1
                result["messages"].append(convo)
            else:
                i += 1

        # The list view shows no previews, so open each thread (most recent 8) and read it.
        for convo in result["messages"][:8]:
            try:
                goto(f"https://www.fiverr.com/inbox/{convo['sender']}", settle=3.0)
                thread = read_text(6000)
                if _looks_blocked(thread):
                    result["blocked"] = True
                    return result
                convo["removed"] = "can no longer be contacted" in thread
                if convo["removed"]:
                    convo["preview"] = "(account removed by Fiverr)"
                    convo["suspicious"] = True
                    continue
                marker = thread.find("Learn more")
                body = thread[marker + len("Learn more"):] if marker != -1 else thread
                body = re.split(r"\nCreate an offer|\nAbout\n", body)[0]
                text_lines = [ln.strip() for ln in body.splitlines()
                              if ln.strip() and ln.strip() not in ("Fiverr", "Only visible to you")]
                convo["preview"] = " ".join(text_lines)[:300]
                convo["suspicious"] = bool(_SUSPICIOUS_RE.search(" ".join(text_lines)))
                convo["read"] = bool(text_lines)
            except Exception:
                convo["preview"] = ""
                convo["suspicious"] = False
                convo["read"] = False
        for convo in result["messages"]:
            convo.setdefault("suspicious", False)
            convo.setdefault("removed", False)
            convo.setdefault("read", True)

        goto("https://www.fiverr.com/users/danielhoole/manage_gigs?current_filter=active", settle=3.0)
        gigs_raw = read_text(8000)
        if _looks_blocked(gigs_raw):
            result["blocked"] = True
            return result
        gig_lines = [ln.strip() for ln in gigs_raw.splitlines() if ln.strip()]
        for i in range(1, len(gig_lines)):
            match = re.fullmatch(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*%?", gig_lines[i])
            if match and len(gig_lines[i - 1]) > 15 and gig_lines[i - 1] != "GIG":
                result["gigs"].append({"title": gig_lines[i - 1], "impressions": int(match.group(1)),
                                       "clicks": int(match.group(2)), "orders": int(match.group(3))})
    except Exception as error:
        result["error"] = str(error)
    return result


def describe_fiverr_status(status):
    """Spoken-style summary of fiverr_status()."""
    if status.get("blocked"):
        return ("Fiverr is showing a human-verification page to the Jarvis browser, sir, and I won't try to get "
                "past that. You can complete the check yourself in that window, or I'll read your normal browser instead.")
    if status.get("error"):
        return f"I couldn't read Fiverr, sir: {status['error']}"
    if not status.get("logged_in"):
        return ("Fiverr wants a login in the Jarvis browser window, sir. Sign in there once and I'll stay signed in.")
    orders = status["orders"]
    total_orders = sum(orders.values()) if orders else 0
    parts = [f"Fiverr orders: {total_orders} in total" + (
        " (" + ", ".join(f"{k} {v}" for k, v in orders.items() if v) + ")" if total_orders else ".")]
    if status["gigs"]:
        impressions = sum(g["impressions"] for g in status["gigs"])
        clicks = sum(g["clicks"] for g in status["gigs"])
        parts.append(f"{len(status['gigs'])} gigs live with {impressions} impressions and {clicks} clicks.")
    msgs = status["messages"]
    if msgs:
        removed = [m for m in msgs if m.get("removed")]
        scams = [m for m in msgs if m["suspicious"] and not m.get("removed")]
        unreadable = [m for m in msgs if not m.get("removed") and not m.get("read", True)]
        genuine = [m for m in msgs if not m["suspicious"] and m.get("read", True)]
        parts.append(f"{len(msgs)} message threads.")
        if removed:
            parts.append(f"{len(removed)} are from accounts Fiverr has already removed ({', '.join(m['sender'] for m in removed)}) -- the scam pattern.")
        if scams:
            parts.append(f"{len(scams)} look like scams and should be ignored: {', '.join(m['sender'] for m in scams)}.")
        if unreadable:
            parts.append(f"I couldn't read the text of {len(unreadable)} thread(s) ({', '.join(m['sender'] for m in unreadable)}), "
                         "so I can't vouch for them -- look yourself and treat any 'order placed' plus a link or file as a scam.")
        for m in genuine[:3]:
            parts.append(f"{m['sender']} says: {m['preview'][:140]}")
    else:
        parts.append("No messages.")
    return " ".join(parts)
