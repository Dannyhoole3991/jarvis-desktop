"""
Advertising: how Danny's posts and ads are doing, platform by platform.

TikTok is read from TikTok Studio in Jarvis's own browser window (Danny logs in there once).
READ-ONLY here: it opens TikTok's own stats pages and reads their text. It never clicks, likes,
comments, posts or spends. (Posting comes later, and always needs Danny's click on that video.)

Results are cached in data/advertising.json so the tab is instant; a refresh takes ~20 seconds.
Nothing is ever guessed: a page that can't be read (logged out, human check, layout changed)
is reported as exactly that, and the last good reading is kept with its time.
"""

import json
import os
import re
import time

from . import config

CACHE_FILE = os.path.join(config.DATA_DIR, "advertising.json")
STUDIO = "https://www.tiktok.com/tiktokstudio"
DURATION_RE = re.compile(r"^\d{1,2}:\d{2}$")
DATE_RE = re.compile(r"^\d{1,2} [A-Z][a-z]{2,3}(?: \d{4})?, \d{1,2}:\d{2} [ap]m$")   # "5 Oct", "12 Sept 2026"
PRIVACY = ("Public", "Private", "Friends", "Only me", "Followers")
OVERVIEW_METRICS = ("Video views", "Profile views", "Likes", "Comments", "Shares")


def _num(text):
    """'1.2K' / '3' / '--' -> number or None."""
    t = (text or "").strip().replace(",", "")
    if not t or t in ("--", "-"):
        return None
    mult = 1
    if t[-1:] in "KkMm":
        mult = 1000 if t[-1] in "Kk" else 1_000_000
        t = t[:-1]
    try:
        return float(t) * mult if "." in t else int(t) * mult
    except ValueError:
        return None


def _lines(text):
    # TikTok writes "11:50 pm" (a narrow no-break space): normalise every odd space to a plain one.
    return [re.sub(r"[    ]", " ", l).strip() for l in text.splitlines() if l.strip()]


def parse_overview(text):
    """{metric: {"value", "change", "change_pct"}} from the Studio overview page text."""
    lines = _lines(text)
    out = {}
    for name in OVERVIEW_METRICS:
        if name in lines:
            i = lines.index(name)
            value = _num(lines[i + 1]) if i + 1 < len(lines) else None
            change = _num(lines[i + 2]) if i + 2 < len(lines) else None
            pct = None
            if i + 3 < len(lines):
                m = re.match(r"^\(([-+]?[\d.]+)%\)$", lines[i + 3])
                pct = float(m.group(1)) if m else None
            out[name] = {"value": value, "change": change, "change_pct": pct}
    period = next((l for l in lines[:5] if l.startswith("Last ")), None)
    return {"period": period, "metrics": out}


def parse_followers(text):
    lines = _lines(text)
    if "Total followers" in lines:
        i = lines.index("Total followers")
        return _num(lines[i + 1]) if i + 1 < len(lines) else None
    return None


def parse_videos(text):
    """[{duration, caption, posted, privacy, views, likes, comments}] from the Studio content list."""
    lines = _lines(text)
    videos = []
    i = 0
    while i < len(lines):
        if DURATION_RE.match(lines[i]) and i + 6 < len(lines):
            # duration, caption (may span lines until the date), date, privacy, views, likes, comments
            j = i + 1
            caption = []
            while j < len(lines) and not DATE_RE.match(lines[j]) and j - i < 8:
                caption.append(lines[j])
                j += 1
            if j + 4 < len(lines) and DATE_RE.match(lines[j]) and lines[j + 1] in PRIVACY:
                videos.append({"duration": lines[i], "caption": " ".join(caption), "posted": lines[j], "privacy": lines[j + 1],
                               "views": _num(lines[j + 2]), "likes": _num(lines[j + 3]), "comments": _num(lines[j + 4])})
                i = j + 5
                continue
        i += 1
    return videos


def _looks_logged_out(url, text):
    return "/login" in url or ("Log in" in text[:600] and "TikTok Studio" not in text[:600] and "Overview" not in text[:600])


def read_tiktok():
    """Reads the three Studio pages. Returns {"ok", ...data} or {"ok": False, "problem"}."""
    import jarvis_browser as jb
    try:
        info = jb.goto(STUDIO + "/analytics", settle=6)
        text = jb.read_text(8000)
        if jb._looks_blocked(text):
            return {"ok": False, "problem": "TikTok is showing a human check in Jarvis's window"}
        if _looks_logged_out(info["url"], text):
            return {"ok": False, "problem": "Jarvis's browser is logged out of TikTok", "logged_out": True}
        overview = parse_overview(text)
        if not overview["metrics"]:
            return {"ok": False, "problem": "couldn't read TikTok's stats page (its layout may have changed)"}
        jb.goto(STUDIO + "/analytics/followers", settle=5)
        followers = parse_followers(jb.read_text(4000))
        jb.goto(STUDIO + "/content", settle=6)
        videos = parse_videos(jb.read_text(40000))
    except Exception as error:
        return {"ok": False, "problem": f"Jarvis's browser couldn't reach TikTok ({error})"}
    return {"ok": True, "period": overview["period"], "metrics": overview["metrics"], "followers": followers, "videos": videos}


def load():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"tiktok": None}


def refresh():
    """Reads TikTok now and saves it. A failed read keeps the last good data and records the problem."""
    data = load()
    result = read_tiktok()
    now = time.time()
    if result.get("ok"):
        data["tiktok"] = dict(result, checked_at=now, problem=None)
    else:
        previous = data.get("tiktok") or {}
        data["tiktok"] = dict(previous, ok=bool(previous.get("ok")), problem=result["problem"], problem_at=now,
                              logged_out=result.get("logged_out", False))
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return data


def describe(data=None):
    """Spoken summary for the brain."""
    t = (data or load()).get("tiktok")
    if not t or not t.get("ok"):
        problem = (t or {}).get("problem") or "I haven't checked TikTok yet"
        return f"TikTok: {problem}."
    m = t.get("metrics") or {}
    views = (m.get("Video views") or {}).get("value")
    pct = (m.get("Video views") or {}).get("change_pct")
    likes = (m.get("Likes") or {}).get("value")
    vids = t.get("videos") or []
    best = max(vids, key=lambda v: v.get("views") or 0) if vids else None
    parts = [f"TikTok, {(t.get('period') or 'recently').lower()}: {views if views is not None else 'unknown'} video views"
             + (f" ({pct:+.0f}%)" if pct is not None else "") + f", {likes or 0} likes, {t.get('followers') or 0} followers, {len(vids)} videos posted."]
    if best:
        parts.append(f"Best so far: '{best['caption'][:60]}...' with {best['views']} views.")
    age = int((time.time() - (t.get("checked_at") or time.time())) // 60)
    parts.append(f"(Checked {age} minutes ago.)" if age else "(Checked just now.)")
    if t.get("problem"):
        parts.append(f"Latest check failed: {t['problem']}.")
    return " ".join(parts)
