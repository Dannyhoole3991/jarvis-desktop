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

from . import alerts, builds, config, memory, projects as P, router as R, runs, selfimprove, sessions as SS, tools

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


def api_post(state, path, body, from_ui=False):
    ex = state.executor
    parts = path.strip("/").split("/")
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
    should_watch = real if watch is None else watch
    if should_watch:
        state.watcher = alerts.Watcher(announcer, proposals=selfimprove.listing).start()
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
        claude = state.router.brains.get("claude")
        if claude is not None:
            claude.stop()
