"""
Store credentials (Shopify app + CJ API key).

Same approach as jarvis_trading.live.creds: Danny types them into his own
terminal with `py -3 -m jarvis_store.setup_credentials`; they are encrypted
with Windows DPAPI to his Windows user and stored OUTSIDE the project folder.
Jarvis and Claude never see them, and nothing here logs them.

Shopify (since Jan 2026) issues a Client ID + Client secret for apps made in
the Dev Dashboard; shopify.py exchanges them for a ~24h access token using the
client-credentials grant. A legacy static Admin API token (shpat_...) also works.
"""

import ctypes
import json
import os
import sys
from ctypes import wintypes

from . import config

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".jarvis_store", "credentials.bin")
FIELDS = ("shopify_client_id", "shopify_client_secret", "shopify_token", "cj_api_key")


class SecretsError(Exception):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    if sys.platform != "win32":
        raise SecretsError("Encrypted credential storage needs Windows DPAPI.")
    buf = ctypes.create_string_buffer(data, len(data))
    inb = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(out)):
        raise SecretsError("Windows DPAPI could not process the credentials.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _check_path(path):
    full = os.path.abspath(path)
    if os.path.commonpath([full, config.PROJECT_ROOT]) == config.PROJECT_ROOT:
        raise SecretsError("Credentials must never be stored inside the project folder.")
    return full


def load(path=None):
    """Returns a dict of the stored fields (missing ones are empty strings)."""
    full = _check_path(path or DEFAULT_PATH)
    data = {}
    if os.path.exists(full):
        with open(full, "rb") as f:
            data = json.loads(_dpapi(f.read(), protect=False).decode("utf-8"))
    return {k: (data.get(k) or "").strip() for k in FIELDS}


def save(updates, path=None):
    """Merges `updates` (only non-empty values) into the encrypted store."""
    full = _check_path(path or DEFAULT_PATH)
    current = load(full) if os.path.exists(full) else {k: "" for k in FIELDS}
    for key, value in updates.items():
        if key in FIELDS and value:
            current[key] = value.strip()
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(_dpapi(json.dumps(current).encode("utf-8"), protect=True))
    if sys.platform == "win32":  # best effort: only this Windows user may read it
        os.system(f'icacls "{full}" /inheritance:r /grant:r "{os.environ.get("USERNAME", "")}:F" >nul 2>&1')
    return full


def status(path=None):
    """Which credentials are present -- never their values."""
    try:
        c = load(path)
    except SecretsError as error:
        return {"error": str(error)}
    return {
        "shopify": bool(c["shopify_token"] or (c["shopify_client_id"] and c["shopify_client_secret"])),
        "cj": bool(c["cj_api_key"]),
    }
