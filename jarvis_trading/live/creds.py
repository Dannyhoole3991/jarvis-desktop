"""
Exchange API credentials.

Never stored in the repository, never written to a Freqtrade config file, never logged.
Storage is Windows DPAPI (encrypted to the current Windows user, via ctypes; no extra
dependency) in the user's profile, or environment variables as a fallback. The user
enters the key in their own terminal with `python -m jarvis_trading.live.setup_credentials`;
Jarvis and Claude never see it.

The key you create at the exchange must have: spot trading only, WITHDRAWALS DISABLED, and
an IP restriction. The trader refuses to start live if the exchange reports otherwise.
"""

import ctypes
import json
import os
import re
import sys
from ctypes import wintypes

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".jarvis_live", "credentials.bin")
ENV_KEY, ENV_SECRET = "JARVIS_EXCHANGE_API_KEY", "JARVIS_EXCHANGE_API_SECRET"


class SecretsError(Exception):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data, protect):
    if sys.platform != "win32":
        raise SecretsError("Encrypted credential storage needs Windows DPAPI; use the environment variables instead.")
    inb, keep = _blob(data)
    out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise SecretsError("Windows DPAPI could not process the credentials.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _check_path(path):
    full = os.path.abspath(path)
    if os.path.commonpath([full, REPO_ROOT]) == REPO_ROOT:
        raise SecretsError("Credentials must never be stored inside the project folder.")
    return full


def store_credentials(key, secret, exchange="binance", path=None):
    if not key or not secret:
        raise SecretsError("Both an API key and a secret are required.")
    full = _check_path(path or DEFAULT_PATH)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    blob = _dpapi(json.dumps({"exchange": exchange, "key": key, "secret": secret}).encode("utf-8"), protect=True)
    with open(full, "wb") as f:
        f.write(blob)
    if sys.platform == "win32":  # best effort: only this user may read the file
        os.system(f'icacls "{full}" /inheritance:r /grant:r "{os.environ.get("USERNAME", "")}:F" >nul 2>&1')
    return full


def load_credentials(path=None, environ=None):
    """Returns {"exchange", "key", "secret"} or None when no credentials are configured."""
    env = os.environ if environ is None else environ
    if env.get(ENV_KEY) and env.get(ENV_SECRET):
        return {"exchange": env.get("JARVIS_EXCHANGE", "binance"), "key": env[ENV_KEY], "secret": env[ENV_SECRET]}
    full = _check_path(path or DEFAULT_PATH)
    if not os.path.exists(full):
        return None
    with open(full, "rb") as f:
        return json.loads(_dpapi(f.read(), protect=False).decode("utf-8"))


def have_credentials(path=None, environ=None):
    try:
        return load_credentials(path, environ) is not None
    except SecretsError:
        return False


_PATTERNS = [re.compile(r"""(?i)(api[_-]?key|secret|password|token|authorization)([\\"'\s]*[:=][\\"'\s]*)([^\s\\"',}]+)""")]


def redact_obj(obj, extra=()):
    """Recursively redacts every string inside a structure BEFORE it is serialised (so escaping cannot defeat the patterns)."""
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if re.search(r"(?i)secret|password|api[_-]?key|token", str(k)) else redact_obj(v, extra)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, extra) for v in obj]
    if isinstance(obj, str):
        return redact(obj, extra)
    return obj


def redact(text, extra=()):
    """Removes anything that looks like a credential (and any exact secrets passed in) from text destined for logs."""
    out = str(text)
    for pattern in _PATTERNS:
        out = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
    for value in extra:
        if value:
            out = out.replace(value, "[REDACTED]")
    return out
