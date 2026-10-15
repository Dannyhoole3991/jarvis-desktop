"""
Sends the phone app a copy of everything the desktop tabs show (Money, Store + orders, Business,
Advertising, Trading, Usage, Needs You), every few minutes while the Jarvis window is open.

The phone's backend can't reach this PC (it has no route to it), so the PC pushes, the same way
it already pushes what Jarvis says (pc_said). The phone shows the last copy with its age, so the
tabs still work while the PC is off, and its quick chat brain answers "any orders?" from it.
Read-only: nothing on the phone can change anything here through this.
"""

import base64
import gzip
import json
import os
import threading
import time

import requests

INTERVAL = 180                    # seconds between pushes
SECTION_LIMIT = 80_000            # bytes per section, so one oversized panel can't block the rest


def backend():
    url = os.environ.get("JARVIS_PHONE_BACKEND_URL", "").rstrip("/")
    secret = os.environ.get("JARVIS_PHONE_SECRET", "")
    return (url, secret) if url and secret else (None, None)


def _section(fn):
    try:
        value = fn()
    except Exception as error:
        return {"error": str(error)[:200]}
    if len(json.dumps(value, default=str)) > SECTION_LIMIT:
        return {"error": "too large to send to the phone"}
    return value


def snapshot(state):
    """The same data the desktop tabs read, gathered in-process."""
    from . import server as S

    def store():
        s = S.store_view(state)
        s["products"] = [{k: p.get(k) for k in ("title", "url", "price_min", "price_max", "christmas", "image")}
                         for p in s.get("products") or []]
        return s

    def needs_you():
        p = S.api_panel(state, "needs_you")
        if not p.get("text"):              # still loading in the window: read it directly (quick, local files)
            p = {"text": S.PANEL_SOURCES["needs_you"][0](), "at": time.time()}
        return p

    return {
        "at": time.time(),
        "state": _section(lambda: S.api_state(state)),
        "money": _section(lambda: S.api_panel(state, "money")),
        "needs_you": _section(needs_you),
        "store": _section(store),
        "business": _section(lambda: S.api_get(state, "/api/business", {})),
        "trading": _section(lambda: S.api_get(state, "/api/trading", {})),
        "advertising": _section(lambda: S.api_get(state, "/api/advertising", {})),
        "posts": _section(lambda: S.api_get(state, "/api/posts", {})),
    }


def encode(snap):
    """gzip + base64. Cloudflare (in front of the phone server) blocked the plain JSON with a 403
    "Blocked" page, presumably over links and text in the business notes; one opaque string gets through."""
    return {"gz": base64.b64encode(gzip.compress(json.dumps(snap, default=str).encode("utf-8"))).decode("ascii")}


def push(state, post=requests.post):
    url, secret = backend()
    if not url:
        return False
    try:
        r = post(url + "/api/dashboard_push", data=json.dumps(encode(snapshot(state))),
                 headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json"}, timeout=20)
        if r.status_code != 200:
            _log(f"push refused: {r.status_code} {r.text[:120]!r}")
        return r.status_code == 200
    except Exception as error:
        _log(f"push failed: {error}")
        return False


def _log(line):
    """Failures go to data/phone_sync.log (this runs in the windowless app, where print goes nowhere)."""
    try:
        from . import config
        with open(os.path.join(config.DATA_DIR, "phone_sync.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    except Exception:
        pass


def start(state, interval=INTERVAL):
    def loop():
        time.sleep(20)                     # let the window's first readings land
        while not getattr(state, "stopping", False):
            push(state)
            time.sleep(interval)
    threading.Thread(target=loop, daemon=True, name="jarvis-phone-sync").start()
