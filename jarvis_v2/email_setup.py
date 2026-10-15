"""
Connects Jarvis to the business inbox (support@collectivefinds.co.uk).

    py -3 -m jarvis_v2.email_setup

Danny types the Gmail address behind support@ and a Gmail APP PASSWORD into his own terminal
(the password is read with getpass: it is never shown, printed or logged). The login is tested
against Gmail (IMAP to read, SMTP to send) before anything is saved, then the password is
encrypted with Windows DPAPI to Danny's Windows user and stored OUTSIDE the project folder,
the same way jarvis_store/creds.py keeps the store's keys. Claude never sees it.
"""

import getpass
import imaplib
import json
import os
import re
import smtplib
import socket
import sys

from jarvis_store.creds import SecretsError, _dpapi
from . import config

DEFAULT_PATH = os.path.join(os.path.expanduser("~"), ".jarvis_v2", "mail_credentials.bin")
FIELDS = ("address", "app_password", "from_address")
DEFAULT_FROM = "support@collectivefinds.co.uk"
IMAP_HOST, IMAP_PORT = "imap.gmail.com", 993
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587

APP_PASSWORD_HELP = ("Gmail refused the login. Gmail needs 2-Step Verification turned on and an APP PASSWORD "
                     "(not your normal Gmail password). Make one at https://myaccount.google.com/apppasswords "
                     "while signed in to the new support Gmail, then run this again.")


def _check_path(path):
    full = os.path.abspath(path)
    try:
        inside = os.path.commonpath([full, config.REPO_DIR]) == config.REPO_DIR
    except ValueError:          # a different drive: not inside
        inside = False
    if inside:
        raise SecretsError("Mail credentials must never be stored inside the project folder.")
    return full


def load(path=None):
    """The stored fields (missing ones are empty strings)."""
    full = _check_path(path or DEFAULT_PATH)
    data = {}
    if os.path.exists(full):
        with open(full, "rb") as f:
            data = json.loads(_dpapi(f.read(), protect=False).decode("utf-8"))
    return {k: (data.get(k) or "").strip() for k in FIELDS}


def save(values, path=None):
    full = _check_path(path or DEFAULT_PATH)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    data = {k: (values.get(k) or "").strip() for k in FIELDS}
    with open(full, "wb") as f:
        f.write(_dpapi(json.dumps(data).encode("utf-8"), protect=True))
    if sys.platform == "win32":  # best effort: only this Windows user may read it
        os.system(f'icacls "{full}" /inheritance:r /grant:r "{os.environ.get("USERNAME", "")}:F" >nul 2>&1')
    return full


def configured(path=None):
    try:
        c = load(path)
    except Exception:
        return False
    return bool(c["address"] and c["app_password"])


def status(path=None):
    """Whether the inbox is connected and which addresses -- never the password."""
    try:
        c = load(path)
    except Exception as error:
        return {"configured": False, "error": str(error)}
    return {"configured": bool(c["address"] and c["app_password"]), "address": c["address"],
            "from_address": c["from_address"] or DEFAULT_FROM}


def explain(error):
    """A login/connection failure in plain English (never echoes the password)."""
    text = str(error)
    if isinstance(error, (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError)) and re.search(
            r"(?i)auth|credential|password|login|username|534|535", text):
        return APP_PASSWORD_HELP
    if isinstance(error, (socket.gaierror, socket.timeout, TimeoutError, ConnectionError, OSError)):
        return "Couldn't reach Gmail. Check the PC is online and try again."
    return f"Gmail said: {text[:200]}"


def test_login(address, app_password, imap=imaplib.IMAP4_SSL, smtp=smtplib.SMTP):
    """(ok, message): logs in to IMAP and SMTP, changes nothing."""
    try:
        box = imap(IMAP_HOST, IMAP_PORT, timeout=30)
        try:
            box.login(address, app_password)
            box.select("INBOX", readonly=True)
        finally:
            try:
                box.logout()
            except Exception:
                pass
    except Exception as error:
        return False, "Reading mail failed. " + explain(error)
    try:
        s = smtp(SMTP_HOST, SMTP_PORT, timeout=30)
        try:
            s.starttls()
            s.login(address, app_password)
        finally:
            try:
                s.quit()
            except Exception:
                pass
    except Exception as error:
        return False, "Sending mail failed. " + explain(error)
    return True, "Gmail accepted the login for reading and sending."


def main(ask=input, secret=getpass.getpass, out=print, tester=test_login, path=None):
    out("Connect Jarvis to the business inbox (support@collectivefinds.co.uk).")
    out("You need: the new support Gmail address, and a Gmail APP PASSWORD for it")
    out("(Google Account > Security > 2-Step Verification > App passwords). The password is not shown as you type.\n")
    address = ask("Support Gmail address: ").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        out("That doesn't look like an email address. Nothing was saved.")
        return 1
    app_password = re.sub(r"\s+", "", secret("Gmail app password (16 letters): "))
    if not app_password:
        out("No password typed. Nothing was saved.")
        return 1
    from_address = ask(f"Reply from which address? [{DEFAULT_FROM}]: ").strip() or DEFAULT_FROM
    out("\nTesting the login with Gmail...")
    ok, message = tester(address, app_password)
    if not ok:
        out(message)
        out("Nothing was saved.")
        return 1
    try:
        where = save({"address": address, "app_password": app_password, "from_address": from_address}, path)
    except SecretsError as error:
        out(f"Couldn't save it: {error}")
        return 1
    out(message)
    out(f"Saved (encrypted to your Windows user) at {where}.")
    out(f"Jarvis will read {address} and reply as {from_address}.")
    out("Restart Jarvis (close the v2 window, run START_JARVIS_V2.bat) and open the Inbox tab.")
    out(f"Important: in Gmail, Settings > Accounts > 'Send mail as' must list {from_address}, or replies show the Gmail address.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
