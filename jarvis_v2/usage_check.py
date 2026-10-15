"""
Keeps the Usage tab fresh without spending any allowance.

`claude -p /usage` is answered by Claude Code itself (no model call, 0 tokens) and reports the
same limits as the Claude app, including usage from Danny's other Claude Code sessions:

    Current session: 6% used · resets Oct 15, 12pm (Europe/London)
    Current week (all models): 24% used · resets Oct 17, 7pm (Europe/London)

The server runs it every couple of minutes and saves it in data/usage.json, the file the
Usage tab (and the brain router's status) already read.
"""

import datetime
import json
import os
import re
import subprocess
import time

from . import config
from .claude_env import child_env

LINE_RE = re.compile(r"^Current (session|week(?: \(([^)]+)\))?):\s*(\d+(?:\.\d+)?)% used(?:\s*·\s*resets\s+(.+?))?\s*$", re.M)
RESET_RE = re.compile(r"(?:([A-Z][a-z]{2}) (\d{1,2}),\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
INTERVAL = 120


def _key(label, scope):
    if label == "session":
        return "five_hour"
    scope = (scope or "all models").lower()
    return "seven_day" if scope == "all models" else "seven_day_" + re.sub(r"[^a-z]+", "_", scope.replace(" only", "")).strip("_")


def parse_reset(text, now=None):
    """'Oct 15, 12pm (Europe/London)' -> epoch seconds (local time), or None."""
    now = now or datetime.datetime.now()
    m = RESET_RE.search(text or "")
    if not m:
        return None
    mon, day, hour, minute, ampm = m.groups()
    hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    when = now.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
    if mon:
        when = when.replace(month=MONTHS[mon.lower()], day=int(day))
        if when < now - datetime.timedelta(days=1):          # e.g. "Jan 2" seen in December
            when = when.replace(year=when.year + 1)
    elif when < now:
        when += datetime.timedelta(days=1)
    return when.timestamp()


def parse(text, now=None):
    windows = {}
    for label, scope, pct, reset in LINE_RE.findall(text or ""):
        util = float(pct) / 100
        windows[_key(label, scope)] = {"utilization": util, "resets_at": parse_reset(reset, now),
                                       "status": "rejected" if util >= 1 else "allowed"}
    return windows


def read(run=subprocess.run):
    out = run([config.find_claude_exe(), "-p", "/usage", "--output-format", "json"], capture_output=True, text=True,
              encoding="utf-8", errors="replace", env=child_env(), timeout=60, cwd=config.DATA_DIR,
              creationflags=0x08000000 if os.name == "nt" else 0)
    return parse(json.loads(out.stdout or "{}").get("result") or "")


def save(windows, path=None):
    """Replaces the windows /usage reported (keeps any others), stamped with the time."""
    path = path or config.USAGE_FILE
    if not windows:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            stored = json.load(f)
    except Exception:
        stored = {}
    now = time.time()
    stored.setdefault("windows", {})
    for key, w in windows.items():
        stored["windows"][key] = dict(w, observed_at=now)
    worst = max(windows.values(), key=lambda w: w["utilization"])
    stored.update(observed_at=now, status=worst["status"], source="claude /usage")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(stored, f, indent=1)
    return stored


def refresh():
    try:
        return save(read())
    except Exception:
        return None
