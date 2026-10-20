"""
The Marketing dashboard for Danny's laptop browser: a separate small web server on the PC, serving
ONLY the marketing page and its API. The laptop needs nothing installed and never sees the PC's files.

Security:
  * off until Danny sets a password (py -3 -m jarvis_marketing set-password); only a salted
    PBKDF2-SHA256 hash is stored (data/remote_login.json, git-ignored);
  * login gives an HttpOnly, SameSite=Strict session cookie (12 hours); every change also needs the
    per-session CSRF token, and a browser's Origin must match;
  * 5 wrong passwords from one address lock it out for 15 minutes (and every failure is audited);
  * it binds only to the address Danny chose (e.g. his Tailscale address); HTTPS is used when
    JARVIS_MARKETING_TLS_CERT / _KEY point to a certificate;
  * no route reads arbitrary files: videos come only from the department's own assets folder.
"""

import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import ssl
import threading
import time
import urllib.parse

from jarvis_marketing import api, config, controls as C

SESSION_SECONDS = 12 * 3600
LOCKOUT_FAILURES, LOCKOUT_SECONDS = 5, 15 * 60
PBKDF2_ROUNDS = 310_000
DEFAULT_PORT = 8790


def hash_password(password, salt=None, rounds=PBKDF2_ROUNDS):
    salt = salt or secrets.token_bytes(16)
    return {"salt": salt.hex(), "rounds": rounds,
            "hash": hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds).hex()}


def set_password(password, bind="127.0.0.1", port=DEFAULT_PORT, path=None):
    if len(password) < 12:
        raise ValueError("Use at least 12 characters.")
    path = path or config.PASSWORD_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(hash_password(password), bind=bind, port=int(port)), f)
    return path


def load_login(path=None):
    try:
        with open(path or config.PASSWORD_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def check_password(password, login):
    got = hash_password(password, bytes.fromhex(login["salt"]), login["rounds"])["hash"]
    return hmac.compare_digest(got, login["hash"])


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis Marketing</title><style>body{margin:0;background:#050508;color:#fff3e0;font-family:Segoe UI,system-ui,sans-serif;
display:grid;place-items:center;height:100vh}form{border:1px solid rgba(255,179,71,.3);border-radius:14px;padding:28px;width:300px}
h1{font-size:16px;color:#ffb347;margin:0 0 14px}input,button{width:100%;box-sizing:border-box;padding:10px;margin-top:8px;border-radius:8px;
border:1px solid rgba(255,179,71,.3);background:#111;color:#fff3e0;font-size:14px}button{background:rgba(255,179,71,.2);cursor:pointer}
p{color:#ff6a6a;font-size:13px;min-height:1em}</style></head><body><form method="post" action="/login">
<h1>Jarvis &middot; Marketing</h1><input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
<button>Sign in</button><p>__ERROR__</p></form></body></html>"""


class RemoteState:
    def __init__(self, dept, login, clock=time.time):
        self.dept, self.login, self.clock = dept, login, clock
        self.sessions = {}                 # token -> {"csrf", "expires", "ip"}
        self.failures = {}                 # ip -> [failure times]
        self.lock = threading.Lock()
        self.tls = False

    def locked_out(self, ip):
        now = self.clock()
        recent = [t for t in self.failures.get(ip, []) if now - t < LOCKOUT_SECONDS]
        self.failures[ip] = recent
        return len(recent) >= LOCKOUT_FAILURES

    def audit(self, action, outcome, **details):
        d = next(iter(self.dept.directors.values()))
        d.store.audit("laptop", action, outcome, **details)


def make_handler(rs):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "JarvisMarketing"
        sys_version = ""

        def log_message(self, *args):
            pass

        def _ip(self):
            return self.client_address[0]

        def _headers(self, status, ctype, length, extra=()):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                        "style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; media-src 'self'; frame-ancestors 'none'")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()

        def _send(self, status, body, ctype="application/json; charset=utf-8", extra=()):
            data = body if isinstance(body, bytes) else (json.dumps(body, default=str) if not isinstance(body, str) else body).encode("utf-8")
            self._headers(status, ctype, len(data), extra)
            self.wfile.write(data)

        def _session(self):
            cookie = self.headers.get("Cookie") or ""
            m = re.search(r"(?:^|;\s*)jm_session=([A-Za-z0-9_\-]+)", cookie)
            if not m:
                return None, None
            with rs.lock:
                s = rs.sessions.get(m.group(1))
                if not s or s["expires"] < rs.clock():
                    rs.sessions.pop(m.group(1), None)
                    return None, None
                return m.group(1), s

        def _same_origin(self):
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host") or ""
            return urllib.parse.urlparse(origin).netloc == host

        def _cookie(self, value, max_age):
            flags = "HttpOnly; SameSite=Strict; Path=/" + ("; Secure" if rs.tls else "")
            return ("Set-Cookie", f"jm_session={value}; Max-Age={max_age}; {flags}")

        # ------------------------------------------------------------- GET
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            token, session = self._session()
            if url.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if url.path == "/login":
                return self._send(200, LOGIN_PAGE.replace("__ERROR__", ""), "text/html; charset=utf-8")
            if session is None:
                if url.path.startswith("/api/"):
                    return self._send(401, {"error": "Sign in first."})
                return self._send(303, b"", "text/plain", [("Location", "/login")])
            if url.path in ("/", "/marketing"):
                with open(config.UI_FILE, encoding="utf-8") as f:
                    page = f.read().replace("__JM_MODE__", "laptop").replace("__JM_CSRF__", session["csrf"]) \
                        .replace("__JARVIS_TOKEN__", "").replace("__JARVIS_UI_TOKEN__", "")
                return self._send(200, page, "text/html; charset=utf-8")
            m = re.fullmatch(r"/api/marketing/([\w-]+)/creatives/(\d+)/media", url.path)
            if m:
                try:
                    path = api.media_path(rs.dept, m.group(1), int(m.group(2)))
                except ValueError as error:
                    return self._send(404, {"error": str(error)})
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "video/mp4")
            try:
                out = api.handle_get(rs.dept, url.path, urllib.parse.parse_qs(url.query))
            except ValueError as error:
                return self._send(400, {"error": str(error)})
            return self._send(200, out) if out is not None else self._send(404, {"error": "not found"})

        # ------------------------------------------------------------ POST
        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            length = min(int(self.headers.get("Content-Length") or 0), 64_000)
            raw = self.rfile.read(length) if length else b""
            if url.path == "/login":
                ip = self._ip()
                with rs.lock:
                    if rs.locked_out(ip):
                        rs.audit("login", "locked_out", ip=ip)
                        return self._send(429, LOGIN_PAGE.replace("__ERROR__", "Too many attempts. Try again in 15 minutes."),
                                          "text/html; charset=utf-8")
                password = urllib.parse.parse_qs(raw.decode("utf-8", "replace")).get("password", [""])[0]
                if not rs.login or not password or not check_password(password, rs.login):
                    with rs.lock:
                        rs.failures.setdefault(ip, []).append(rs.clock())
                    rs.audit("login", "failed", ip=ip)
                    return self._send(401, LOGIN_PAGE.replace("__ERROR__", "Wrong password."), "text/html; charset=utf-8")
                token = secrets.token_urlsafe(32)
                with rs.lock:
                    rs.failures.pop(ip, None)
                    rs.sessions[token] = {"csrf": secrets.token_urlsafe(24), "expires": rs.clock() + SESSION_SECONDS, "ip": ip}
                rs.audit("login", "ok", ip=ip)
                return self._send(303, b"", "text/plain", [("Location", "/"), self._cookie(token, SESSION_SECONDS)])
            token, session = self._session()
            if session is None:
                return self._send(401, {"error": "Sign in first."})
            if not self._same_origin() or not hmac.compare_digest(self.headers.get("X-CSRF", ""), session["csrf"]):
                rs.audit("csrf", "refused", ip=self._ip(), path=url.path)
                return self._send(403, {"error": "Refused (bad or missing CSRF token)."})
            if url.path == "/logout":
                with rs.lock:
                    rs.sessions.pop(token, None)
                return self._send(200, {"ok": True}, extra=[self._cookie("", 0)])
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                body = {}
            try:
                out = api.handle_post(rs.dept, url.path, body, principal=C.HumanPrincipal("Danny", "laptop"))
            except PermissionError as error:
                return self._send(403, {"error": str(error)})
            except ValueError as error:
                return self._send(400, {"error": str(error)})
            return self._send(200, out) if out is not None else self._send(404, {"error": "not found"})

    return Handler


def start(dept, login=None, bind=None, port=None):
    """Starts the laptop server if a password has been set. Returns (server, state) or (None, reason)."""
    login = login or load_login()
    if not login:
        return None, "No laptop password set yet (py -3 -m jarvis_marketing set-password)."
    bind = bind or os.environ.get("JARVIS_MARKETING_REMOTE_BIND") or login.get("bind") or "127.0.0.1"
    port = int(port if port is not None else os.environ.get("JARVIS_MARKETING_REMOTE_PORT") or login.get("port") or DEFAULT_PORT)
    rs = RemoteState(dept, login)
    server = http.server.ThreadingHTTPServer((bind, port), make_handler(rs))
    cert, key = os.environ.get("JARVIS_MARKETING_TLS_CERT"), os.environ.get("JARVIS_MARKETING_TLS_KEY")
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        rs.tls = True
    threading.Thread(target=server.serve_forever, daemon=True, name="jarvis-marketing-remote").start()
    return server, rs
