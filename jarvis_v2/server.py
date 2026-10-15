"""
The Jarvis v2 server: the brain router plus everything the new desktop window shows.

    http://127.0.0.1:8780        (this PC only)

It serves the window (ui/index.html) and a small JSON API. The voice engine
(Jarvis_FINAL_WORKING.py) also asks the brain through it, so voice, typing and the
dashboard all share ONE router and one conversation.

Security: listens on 127.0.0.1 only; every /api call needs the per-launch token
(X-Jarvis-Token) and a localhost Host header, so a web page in a browser cannot
drive Jarvis (it can neither read the token nor pass the Host check via DNS tricks).
The engine's shared secret never reaches the page: engine calls are proxied here.
"""

import http.server
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import urllib.request

from . import advertising, alerts, posting, builds, config, memory, projects as P, router as R, runs, selfimprove, sessions as SS, tools

# A voice/chat approval only counts if Danny's own latest message actually approves.
APPROVAL_WORDS = re.compile(r"(?i)\b(approve[ds]?|go ahead|start (it|the build)|build it|do it|yes|looks good|ship it)\b")

PORT = int(os.environ.get("JARVIS_V2_PORT", "8780"))
UI_FILE = os.path.join(config.PACKAGE_DIR, "ui", "index.html")
TOKEN_FILE = os.path.join(config.DATA_DIR, "server_token")
ENGINE_FLAG_FILE = os.path.join(config.DATA_DIR, "use_v2_brain")


class State:
    def __init__(self, router, executor):
        self.router = router
        self.executor = executor
        self.token = secrets.token_urlsafe(24)       # page + engine + brain tools (written to TOKEN_FILE)
        self.ui_token = secrets.token_urlsafe(24)    # ONLY in the page: needed to apply/reject/undo Jarvis's code changes
        self.thinking = False
        self.current_user_text = ""                  # Danny's message being answered right now
        self.last_reply = None
        self.panels = {}                 # name -> {"text", "at", "running"}
        self.panel_lock = threading.Lock()
        self.adv_refreshing = False
        self.orders_refreshing = False   # reading Shopify orders in Jarvis's browser
        self.orders = None               # last reading: {ok, orders, payouts, at}
        self.posting_now = None
        self.announcer = None            # alerts.Announcer, set by start()
        self.watcher = None


PANEL_SOURCES = {
    "money": (tools.money_check, 0),              # slow (reads 4 sites); only refreshed on request
    "store": (lambda: tools.store_status() + "\n\n" + tools.store_list_drafts(), 300),
    "trading": (tools.trading_status, 300),
    "business": (tools.business_status, 120),
    "needs_you": (tools.needs_you, 30),
}


def _engine(path, payload=None, timeout=5):
    secret = os.environ.get("JARVIS_PHONE_SECRET", "")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(config.ENGINE_URL + path, data=data, method="POST" if data is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    if secret:
        req.add_header("Authorization", "Bearer " + secret)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def make_handler(state):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        # ---------------------------------------------------------- helpers
        def _json(self, obj, status=200):
            body = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_video(self, item_id):
            item = posting.get(item_id)
            if not item:
                return self._json({"error": "no such video"}, 404)
            try:
                path = posting._safe_video(item["video"])
            except ValueError as error:
                return self._json({"error": str(error)}, 400)
            size = os.path.getsize(path)
            start, end = 0, size - 1
            rng = self.headers.get("Range", "")
            mm = re.match(r"bytes=(\d*)-(\d*)", rng)
            if mm:
                start = int(mm.group(1) or 0)
                end = min(int(mm.group(2)) if mm.group(2) else size - 1, size - 1)
            self.send_response(206 if mm else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if mm:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (ConnectionError, OSError):
                        break
                    remaining -= len(chunk)

        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _authorized(self):
            return self._host_ok() and secrets.compare_digest(self.headers.get("X-Jarvis-Token", ""), state.token)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return {}

        # -------------------------------------------------------------- GET
        def do_GET(self):
            if not self._host_ok():
                return self._json({"error": "forbidden"}, 403)
            url = urllib.parse.urlparse(self.path)
            if url.path in ("/", "/index.html"):
                with open(UI_FILE, encoding="utf-8") as f:
                    page = f.read().replace("__JARVIS_TOKEN__", state.token).replace("__JARVIS_UI_TOKEN__", state.ui_token)
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                return self.wfile.write(body)
            m = re.match(r"^/api/posts/(\d+)/video$", url.path)
            if m and self._host_ok():
                # A <video> tag can't send headers, so this one GET takes the token in the query string.
                t = urllib.parse.parse_qs(url.query).get("t", [""])[0]
                if not secrets.compare_digest(t, state.token):
                    return self._json({"error": "unauthorized"}, 401)
                return self._send_video(int(m.group(1)))
            if not self._authorized():
                return self._json({"error": "unauthorized"}, 401)
            if url.path == "/api/state":
                return self._json(api_state(state))
            if url.path.startswith("/api/panel/"):
                return self._json(api_panel(state, url.path.rsplit("/", 1)[-1]))
            if url.path == "/api/conversation":
                since = urllib.parse.parse_qs(url.query).get("since", ["0"])[0]
                try:
                    return self._json(_engine(f"/conversation?since={int(since)}"))
                except Exception:
                    return self._json({"messages": [], "engine": "offline"})
            try:
                out = api_get(state, url.path, urllib.parse.parse_qs(url.query))
            except ValueError as error:
                return self._json({"error": str(error)}, 400)
            return self._json(out) if out is not None else self._json({"error": "not found"}, 404)

        # ------------------------------------------------------------- POST
        def do_POST(self):
            if not self._authorized():
                return self._json({"error": "unauthorized"}, 401)
            path, body = urllib.parse.urlparse(self.path).path, self._body()
            if path == "/api/ask":
                return self._json(api_ask(state, str(body.get("text", ""))))
            if path == "/api/command":
                return self._json(api_command(state, str(body.get("text", ""))))
            if path == "/api/stop":
                try:
                    _engine("/stop", {})
                except Exception:
                    pass
                return self._json({"ok": True})
            if path == "/api/brain":
                use = body.get("use")
                try:
                    return self._json({"text": state.router.force(None if use in (None, "auto") else use)})
                except ValueError as error:
                    return self._json({"error": str(error)}, 400)
            if path.startswith("/api/panel/") and path.endswith("/refresh"):
                name = path.split("/")[3]
                return self._json(api_panel(state, name, refresh=True))
            ui = secrets.compare_digest(self.headers.get("X-Jarvis-UI", ""), state.ui_token)
            try:
                out = api_post(state, path, body, from_ui=ui)
            except ValueError as error:
                return self._json({"error": str(error)}, 400)
            except PermissionError as error:
                return self._json({"error": str(error)}, 403)
            return self._json(out) if out is not None else self._json({"error": "not found"}, 404)

    return Handler


# ------------------------------------------------------------------ API logic
def api_ask(state, text):
    """The brain. Used by the voice engine and by the window when the engine is offline."""
    text = text.strip()
    if not text:
        return {"error": "empty"}
    state.thinking = True
    state.current_user_text = text
    try:
        reply = state.router.ask(text)
    finally:
        state.thinking = False
    state.last_reply = {"text": reply.text, "brain": reply.brain, "at": time.time()}
    return {"text": reply.text, "spoken": reply.spoken(), "brain": reply.brain, "announcement": reply.announcement, "tools": reply.tools}


def api_command(state, text):
    """Typed in the window: go through the engine (instant PC commands + speech + this brain), else straight to the brain."""
    text = text.strip()
    if not text:
        return {"error": "empty"}
    try:
        reply = _engine("/command", {"command": text}, timeout=200)
        return {"text": reply.get("reply") or reply.get("error") or "", "via": "engine"}
    except Exception:
        out = api_ask(state, text)
        out["via"] = "brain (voice engine offline: reply is text only)"
        out["text"] = out.get("spoken", out.get("text", ""))
        return out


def api_state(state):
    try:
        engine = _engine("/status", timeout=1.5)
        engine["online"] = True
    except Exception:
        engine = {"online": False}
    st = R.load_state()
    usage = R._read_json(config.USAGE_FILE, {})
    now = time.time()
    blocked = {k: v for k, v in (st.get("blocked") or {}).items() if v.get("until", 0) > now}
    return {"active": st.get("answering") or st.get("active"), "forced": st.get("forced"), "blocked": blocked,
            "usage": usage, "status_text": R.status_text(), "thinking": state.thinking, "engine": engine, "now": now}


def api_panel(state, name, refresh=False):
    if name not in PANEL_SOURCES:
        return {"error": "unknown panel"}
    fn, ttl = PANEL_SOURCES[name]
    with state.panel_lock:
        p = state.panels.setdefault(name, {"text": None, "at": None, "running": False})
        never_loaded = p["at"] is None
        expired = bool(ttl) and not never_loaded and time.time() - p["at"] > ttl
        # ttl 0 (money) = only when asked: it is slow and drives a browser.
        start = not p["running"] and (refresh or (bool(ttl) and (never_loaded or expired)))
        if start:
            p["running"] = True

    if start:
        def work():
            try:
                text = fn()
            except Exception as error:
                text = f"Couldn't load this: {error}"
            with state.panel_lock:
                p.update(text=text, at=time.time(), running=False)
        threading.Thread(target=work, daemon=True).start()
    with state.panel_lock:
        return dict(p)


# ------------------------------------------------- runs, projects, builds, changes
def _run_view(run, with_events=False, store=None):
    if not run:
        return None
    view = dict(run)
    view["prompt_short"] = run["prompt"][:200]
    if with_events:
        lines = []
        for e in (store or runs.RunStore()).events(run["id"], limit=2000):
            line = runs.summarise_event(e["event"])
            if line:
                lines.append({"ts": e["ts"], "text": line[:2000]})
        view["transcript"] = lines[-400:]
    return view


def _runs_overview(store):
    active = store.list(limit=20, statuses=list(runs.ACTIVE))
    recent = [r for r in store.list(limit=8) if r["status"] in runs.TERMINAL][:5]
    if not active and not recent:
        return "No runs yet."
    lines = [runs.describe(r) for r in active] or ["Nothing is running."]
    if recent:
        lines.append("Recently finished: " + " ".join(runs.describe(r) for r in recent))
    return " ".join(lines)


def api_get(state, path, query):
    store = state.executor.store
    parts = path.strip("/").split("/")          # ["api", ...]
    if parts[:2] == ["api", "runs"]:
        if len(parts) == 2:
            return {"runs": [_run_view(r) for r in store.list(limit=80)]}
        if parts[2] == "describe":
            return {"text": _runs_overview(store)}
        run_id = int(parts[2])
        run = store.get(run_id)
        if run is None:
            raise ValueError("No such run.")
        if len(parts) == 4 and parts[3] == "describe":
            return {"text": runs.describe(run)}
        return _run_view(run, with_events=True, store=store)
    if parts[:2] == ["api", "projects"]:
        if len(parts) == 2:
            items = P.discover()
            for p in items:
                p["runs"] = len(store.list(limit=500, project=p["name"]))
            return {"projects": items}
        project = P.find(urllib.parse.unquote(parts[2]))
        if not project:
            raise ValueError("No such project.")
        info = P.details(project)
        info["recent_runs"] = [_run_view(r) for r in store.list(limit=15, project=project["name"])]
        info["builds"] = [b for b in builds.builds_list() if os.path.normcase(b["path"]) == os.path.normcase(project["path"])]
        return info
    if path == "/api/docs":
        return {"documents": builds.all_documents(), "builds": builds.builds_list()}
    if path == "/api/doc":
        project = P.find((query.get("project") or [""])[0])
        doc = (query.get("doc") or [""])[0]
        if not project or not doc or ".." in doc:
            raise ValueError("Unknown document.")
        full = os.path.realpath(os.path.join(project["path"], doc))
        if not full.startswith(os.path.realpath(project["path"])) or not os.path.isfile(full):
            raise ValueError("Unknown document.")
        with open(full, encoding="utf-8", errors="replace") as f:
            return {"project": project["name"], "doc": doc, "text": f.read()[:200000]}
    if path == "/api/memory":
        config.ensure_dirs()
        items = []
        for name in sorted(os.listdir(config.MEMORY_DIR)):
            if name.endswith(".md"):
                full = os.path.join(config.MEMORY_DIR, name)
                items.append({"name": name, "modified": os.path.getmtime(full), "size": os.path.getsize(full)})
        return {"files": items, "index": memory.index_text()}
    if parts[:2] == ["api", "memory"] and len(parts) == 3:
        name = os.path.basename(urllib.parse.unquote(parts[2]))
        full = os.path.join(config.MEMORY_DIR, name)
        if not name.endswith(".md") or not os.path.isfile(full):
            raise ValueError("No such memory.")
        with open(full, encoding="utf-8") as f:
            return {"name": name, "text": f.read()}
    if path == "/api/business":
        return business_view()
    if path == "/api/store":
        return store_view(state)
    if path == "/api/posts":
        return {"posts": posting.listing(), "posting_now": state.posting_now}
    if path == "/api/advertising":
        data = advertising.load()
        t = data.get("tiktok") or {}
        stale = time.time() - (t.get("checked_at") or 0) > 3600
        if stale and not t.get("logged_out"):
            _refresh_advertising(state)
        return dict(data, refreshing=state.adv_refreshing)
    if path == "/api/trading":
        return trading_view()
    if path == "/api/sessions":
        snap = state.watcher.last if (state.watcher and state.watcher.last) else SS.snapshot()
        return {"sessions": [{k: v for k, v in s.items() if k != "pipe"} for s in snap], "text": SS.describe(snap)}
    if path == "/api/alerts":
        return {"alerts": list(reversed(state.announcer.history)) if state.announcer else []}
    if parts[:2] == ["api", "proposals"]:
        if len(parts) == 2:
            return {"proposals": selfimprove.listing()}
        if parts[2] == "describe":
            items = selfimprove.listing()[:6]
            return {"text": " ".join(selfimprove.describe(p) for p in items) or "Jarvis hasn't proposed any changes to himself yet."}
        if len(parts) == 4 and parts[3] == "diff":
            return {"diff": selfimprove.diff(int(parts[2]))}
    return None


_URL_RE = re.compile(r"https?://[^\s'\"<>)]+")
_NAME_RE = re.compile(r"^(?:Testing:\s*)?(.*?)(?:\s*\(score\s*([\d.]+)/10\))?$")


def business_view():
    """The business tracker shaped for the Business tab: totals, what's live (with links), activity, lessons."""
    import jarvis_business as jbiz
    s = jbiz.summary()
    links = {}
    for item in list(s.get("recent_lessons") or []) + list(s.get("recent_decisions") or []):
        text = item.get("lesson_text") or item.get("reasoning") or ""
        m = _URL_RE.search(text)
        if m and item.get("experiment_id") and item["experiment_id"] not in links:
            links[item["experiment_id"]] = m.group(0).rstrip(".,")
    experiments = []
    for e in s.get("active_experiments") or []:
        m = _NAME_RE.match((e.get("opportunity_name") or e.get("hypothesis") or "").strip())
        name, score = (m.group(1), m.group(2)) if m else (e.get("hypothesis") or "Experiment", None)
        experiments.append({"id": e["id"], "name": e.get("opportunity_name") or name, "score": float(score) if score else None,
                            "type": (e.get("business_model_type") or "").replace("_", " "), "status": e.get("status"),
                            "started": e.get("started_at"), "cost": e.get("cost_spent") or 0.0, "revenue": e.get("revenue_earned") or 0.0,
                            "traffic": e.get("latest_traffic"), "url": links.get(e["id"])})
    activity = [{"ts": d.get("timestamp"), "type": (d.get("decision_type") or "").replace("_", " "), "text": d.get("reasoning") or "",
                 "url": (_URL_RE.search(d.get("reasoning") or "") or [None])[0] if _URL_RE.search(d.get("reasoning") or "") else None}
                for d in s.get("recent_decisions") or []]
    lessons = [{"ts": l.get("created_at"), "category": l.get("category"), "text": l.get("lesson_text") or ""} for l in s.get("recent_lessons") or []]
    return {"target": s.get("target_daily_revenue"), "budget": s.get("budget"), "revenue": s.get("revenue_total") or 0.0,
            "cost": s.get("cost_total") or 0.0, "profit": s.get("profit_total") or 0.0,
            "opportunities": s.get("opportunity_counts") or {}, "experiment_counts": s.get("experiment_counts") or {},
            "experiments": experiments, "activity": activity, "lessons": lessons,
            "pending_actions": [a.get("description") for a in s.get("pending_human_actions") or []]}


_cache = {}


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    value = fn()
    _cache[key] = (time.time(), value)
    return value


def _public_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/2 (store dashboard)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def store_view(state=None):
    """The live shop (read from its public pages, so it works without Jarvis's store keys) + Jarvis's access."""
    from jarvis_store import config as SC, creds
    base = "https://" + SC.SHOP_PUBLIC_URL

    def load():
        products = _public_json(base + "/products.json?limit=250").get("products", [])
        collections = _public_json(base + "/collections.json").get("collections", [])
        xmas = set()
        for c in collections:
            if "christmas" in (c.get("handle") or ""):
                xmas |= {p["handle"] for p in _public_json(f"{base}/collections/{c['handle']}/products.json?limit=250").get("products", [])}
        items = []
        for p in products:
            prices = sorted(float(v["price"]) for v in p.get("variants") or [] if v.get("price"))
            items.append({"title": p["title"], "url": f"{base}/products/{p['handle']}", "type": p.get("product_type") or "",
                          "image": ((p.get("images") or [{}])[0].get("src") or "").split("?")[0] + "?width=240",
                          "price_min": prices[0] if prices else None, "price_max": prices[-1] if prices else None,
                          "variants": len(p.get("variants") or []), "christmas": p["handle"] in xmas, "published": p.get("published_at")})
        return {"products": items, "collections": [{"title": c["title"], "count": c.get("products_count"), "url": f"{base}/collections/{c['handle']}"}
                                                   for c in collections if c.get("handle") != "frontpage"]}

    try:
        shop = _cached("store", 600, load)
    except Exception as error:
        shop = {"products": [], "collections": [], "error": f"Couldn't read the shop: {error}"}
    access = creds.status()
    money = (state.panels.get("money") if state else None) or {}
    if state is not None:
        orders = state.orders or {}
        if not orders.get("at") or time.time() - orders["at"] > ORDERS_TTL:
            _refresh_orders(state)
    return dict(shop, shop_url=base, access={"shopify": bool(access.get("shopify")), "cj": bool(access.get("cj"))},
                money_text=money.get("text"), money_at=money.get("at"),
                orders=state.orders if state else None, orders_refreshing=bool(state and state.orders_refreshing))


ORDERS_TTL = 600     # re-read Shopify orders at most every 10 minutes (it drives Jarvis's browser)


def _keep_usage_fresh(state, interval=None):
    """Claude's usage limits for the Usage tab, every couple of minutes, at no cost (claude -p /usage)."""
    from . import usage_check
    while not getattr(state, "stopping", False):
        usage_check.refresh()
        time.sleep(interval or usage_check.INTERVAL)


def _refresh_orders(state, force=False):
    """Reads the Shopify orders list + pending payout in the background, one browser job at a time."""
    with state.panel_lock:
        if state.orders_refreshing or state.adv_refreshing or state.posting_now:
            return False
        state.orders_refreshing = True

    def work():
        try:
            import jarvis_money
            reading = jarvis_money.shopify_orders()
        except Exception as error:
            reading = {"ok": False, "problem": f"couldn't read Shopify ({error})"}
        if not reading.get("ok") and state.orders and state.orders.get("ok"):   # keep the last good list, say why it's old
            reading = dict(state.orders, problem=reading.get("problem"), logged_out=reading.get("logged_out"))
        reading["at"] = time.time()
        state.orders = reading
        state.orders_refreshing = False
    threading.Thread(target=work, daemon=True, name="jarvis-orders").start()
    return True


def trading_view():
    """Research lab + forward trial, read-only."""
    from jarvis_trading import service

    def load():
        s = service.summary()
        fv = service.forward_view()
        return {"tested": s.get("n_versions"), "trials": s.get("n_trials"), "counts": s.get("counts"), "funnel": s.get("funnel"),
                "rejections": s.get("rejection_reasons")[:8], "readiness": (s.get("readiness") or {}).get("items", []),
                "mode": s.get("mode"), "daemon": s.get("daemon"), "power": s.get("power"), "benchmark": s.get("benchmark"),
                "near_misses": len(s.get("near_misses") or []),
                "forward": [{k: t.get(k) for k in ("strategy", "template", "start", "days_elapsed", "status", "trades", "pnl_pct",
                                                   "max_drawdown", "win_rate", "open_positions")} for t in fv.get("trials", [])],
                "forward_note": fv.get("note")}
    return _cached("trading", 60, load)


def _refresh_advertising(state):
    """Reads TikTok in the background (one read at a time: it drives Jarvis's browser window)."""
    with state.panel_lock:
        if state.adv_refreshing or state.posting_now or state.orders_refreshing:
            return
        state.adv_refreshing = True

    def work():
        try:
            advertising.refresh()
        finally:
            state.adv_refreshing = False
    threading.Thread(target=work, daemon=True, name="jarvis-advertising").start()


def _start_post(state, item_id):
    """Posts one video in the background (one at a time: it drives Jarvis's browser window)."""
    item = posting.get(item_id)
    if not item:
        raise ValueError("No such video in the list.")
    with state.panel_lock:
        if state.posting_now or state.adv_refreshing or state.orders_refreshing:
            raise ValueError("Jarvis's browser is busy with TikTok right now; try again in a minute.")
        state.posting_now = item_id

    def work():
        try:
            result = posting.post(item_id, progress=state.announcer.urgent if state.announcer else None)
            if state.announcer and result:
                if result["status"] == "posted":
                    state.announcer.urgent(f"Your {result['title'].replace('Christmas: ', '')} video is live on TikTok, sir.")
                else:
                    state.announcer.urgent(f"The {result['title'].replace('Christmas: ', '')} video didn't post: {result.get('error')}")
        finally:
            state.posting_now = None
    threading.Thread(target=work, daemon=True, name="jarvis-posting").start()
    return {"text": f"Posting '{item['title']}' to TikTok now. This takes a minute or two; I'll tell you when it's live."}


def api_post(state, path, body, from_ui=False):
    ex = state.executor
    parts = path.strip("/").split("/")
    if path == "/api/say":
        # The brain's "say it now" (e.g. "On it, sir") before a job that takes a while.
        text = str(body.get("text", "")).strip()[:300]
        if not text:
            raise ValueError("Nothing to say.")
        if state.announcer:
            state.announcer.urgent(text)
        return {"ok": True}
    if path == "/api/runs":
        project = P.find(str(body.get("project", "")))
        if not project:
            raise ValueError(f"I don't know a project called {body.get('project')}.")
        if builds.is_jarvis_repo(project["path"]):
            raise ValueError("Changes to Jarvis himself go through improve_jarvis (a separate copy, tests, and Danny's approval).")
        prompt = str(body.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("The run needs an instruction.")
        run_id = ex.submit(project["name"], project["path"], prompt)
        waiting = "" if runs.claude_available() else " It will start when Claude's allowance renews."
        return {"run_id": run_id, "text": f"Started run {run_id} in {project['name']}.{waiting} You can watch it in the Runs tab."}
    if parts[:2] == ["api", "runs"] and len(parts) == 4:
        run_id = int(parts[2])
        if parts[3] == "cancel":
            return {"text": "Stopped it." if ex.cancel(run_id) else "That run had already finished."}
        if parts[3] == "retry":
            new_id = ex.retry(run_id)
            return {"run_id": new_id, "text": f"Started it again as run {new_id}."}
    m = re.match(r"^/api/posts/(\d+)/(post|remove)$", path)
    if m:
        if not from_ui:
            raise PermissionError("Only Danny can post or remove videos, from the Advertising tab.")
        item_id, action = int(m.group(1)), m.group(2)
        if action == "remove":
            return {"text": posting.remove(item_id)}
        return _start_post(state, item_id)
    if path == "/api/advertising/refresh":
        _refresh_advertising(state)
        return {"text": "Checking TikTok now (about 20 seconds)."}
    if path == "/api/orders/refresh":
        started = _refresh_orders(state)
        return {"text": "Checking Shopify orders now." if started else "Jarvis's browser is busy; try again in a minute."}
    if path == "/api/visibility":
        if state.announcer:
            state.announcer.window_visible = bool(body.get("visible", True))
        return {"ok": True}
    if parts[:2] == ["api", "projects"] and len(parts) == 4 and parts[3] == "open":
        project = P.find(urllib.parse.unquote(parts[2]))
        if not project:
            raise ValueError("No such project.")
        if body.get("what") == "terminal":
            return {"text": P.open_in_terminal(project)}
        return {"text": P.open_in_editor(project, body.get("file") or None)}
    if path == "/api/builds":
        b = builds.start_build(str(body.get("project", "")), str(body.get("title") or "Build"), str(body.get("goal", "")),
                               str(body.get("constraints", "")), str(body.get("out_of_scope", "")))
        return {"build": b, "text": f"I've written the design to {b['spec']} in {b['project']}. Nothing starts until you approve it: "
                                    "read it in the Specs tab, or ask me to read you a section."}
    if path == "/api/doc/approve":
        if body.get("via") == "voice" and not from_ui and not APPROVAL_WORDS.search(state.current_user_text or ""):
            raise PermissionError("I can only record an approval when you've just told me you approve it, sir.")
        return {"text": builds.approve_document(str(body.get("project", "")), str(body.get("doc", "")), ex)}
    if path == "/api/proposals":
        p = selfimprove.propose(str(body.get("goal", "")), ex)
        return {"proposal": p, "text": f"I'm working on that in a separate copy of myself (change {p['id']}). "
                                       "When it's done and its tests pass, it'll be waiting for you in the Changes tab."}
    if parts[:2] == ["api", "proposals"] and len(parts) == 4 and parts[3] in ("approve", "reject", "undo"):
        if not from_ui:
            raise PermissionError("Only Danny can apply, reject or undo Jarvis's changes, from the Changes tab.")
        fn = {"approve": selfimprove.approve, "reject": selfimprove.reject, "undo": selfimprove.undo}[parts[3]]
        return {"text": fn(int(parts[2]))}
    return None


# ---------------------------------------------------------------- lifecycle
def set_engine_brain(enabled):
    """Tells the voice engine to send conversation to this brain (it falls back to its old brain if we're down)."""
    config.ensure_dirs()
    with open(ENGINE_FLAG_FILE, "w", encoding="utf-8") as f:
        f.write("1" if enabled else "0")


def start(router=None, port=PORT, executor=None, announcer=None, watch=None):
    """watch=None: watch sessions/proposals only for the real app (a passed-in test executor means no watcher)."""
    config.ensure_dirs()
    real = executor is None
    announcer = announcer or alerts.Announcer()
    if executor is None:
        executor = runs.Executor()
        executor.listeners += [builds.on_run_change, selfimprove.on_run_change, alerts.on_run_change(announcer)]
        executor.start()
    state = State(router or R.default_router(), executor)
    state.announcer = announcer
    announcer.thinking = lambda: state.thinking
    state.router.on_handoff = announcer.urgent          # the quick voice's "On it, sir" is spoken at once
    should_watch = real if watch is None else watch
    if should_watch:
        state.watcher = alerts.Watcher(announcer, proposals=selfimprove.listing).start()
        threading.Thread(target=_keep_usage_fresh, args=(state,), daemon=True, name="jarvis-usage").start()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(state))
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(state.token)
    threading.Thread(target=server.serve_forever, daemon=True, name="jarvis-v2-server").start()
    return server, state


def stop(server, state):
    set_engine_brain(False)
    try:
        if state.watcher:
            state.watcher.stop()
        state.executor.stop()
        server.shutdown()
    finally:
        for brain in (state.router.brains.get("claude"), getattr(state.router, "quick", None)):
            if brain is not None:
                brain.stop()
