"""
Minimal client for Freqtrade's local REST API (stdlib only). Used by the live trader's watchdog to read
state and to execute the emergency stop (stop entries, force-exit everything). The API only ever listens on
127.0.0.1 with per-run random credentials held in memory.
"""

import base64
import json
import urllib.error
import urllib.request


class FtApiError(Exception):
    pass


class FtApi:
    def __init__(self, port, username, password, host="127.0.0.1", timeout=10):
        self.base = f"http://{host}:{port}/api/v1"
        self.username, self.password, self.timeout = username, password, timeout
        self._token = None

    def _request(self, method, path, body=None, auth="bearer", retry=True):
        headers = {"Content-Type": "application/json"}
        if auth == "basic":
            headers["Authorization"] = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        elif auth == "bearer":
            if not self._token:
                self._login()
            headers["Authorization"] = f"Bearer {self._token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            if error.code == 401 and auth == "bearer" and retry:
                self._token = None
                return self._request(method, path, body, auth, retry=False)
            raise FtApiError(f"HTTP {error.code} on {path}") from None
        except Exception as error:
            raise FtApiError(f"{type(error).__name__} on {path}: {str(error)[:120]}") from None

    def _login(self):
        self._token = self._request("POST", "/token/login", auth="basic")["access_token"]

    # reads
    def ping(self):
        return self._request("GET", "/ping", auth=None)

    def status(self):
        return self._request("GET", "/status")          # list of open trades

    def count(self):
        return self._request("GET", "/count")

    def balance(self):
        return self._request("GET", "/balance")

    def profit(self):
        return self._request("GET", "/profit")

    def trades(self, limit=500):
        return self._request("GET", f"/trades?limit={int(limit)}")

    def show_config(self):
        return self._request("GET", "/show_config")

    # actions (only ever reduce risk)
    def stop_entries(self):
        return self._request("POST", "/stopentry")

    def force_exit_all(self):
        return self._request("POST", "/forceexit", {"tradeid": "all", "ordertype": "market"})

    def stop_bot(self):
        return self._request("POST", "/stop")
