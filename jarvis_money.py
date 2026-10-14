"""
Jarvis Money Check
==================

"check money" -- one honest answer to "have we made anything anywhere?".

Jarvis reads each business in his own Edge window (jarvis_browser), which
Danny logs into once and which then stays logged in. Every number reported
here was read off the real page; when a page can't be read (logged out,
human check, layout changed) Jarvis says exactly that instead of guessing.
"""

import re

import jarvis_browser as jb

SHOPIFY_ORDERS = "https://admin.shopify.com/store/mwrfta-mh/orders"
ADSENSE_HOME = "https://adsense.google.com/adsense/"
REDBUBBLE_SALES = "https://www.redbubble.com/studio/dashboard"

_MONEY_RE = re.compile(r"[£$€]\s?\d[\d,]*(?:\.\d{2})?")


def _read(url, settle=4.0):
    info = jb.goto(url, settle=settle)
    return info["url"], jb.read_text(12000)


def shopify_check():
    try:
        url, text = _read(SHOPIFY_ORDERS)
    except Exception as error:
        return {"name": "Shopify store", "ok": False, "problem": f"the page didn't load ({error})"}
    if jb._looks_blocked(text):
        return {"name": "Shopify store", "ok": False, "problem": "it's showing a human check"}
    if "accounts.shopify.com" in url or "/login" in url:
        return {"name": "Shopify store", "ok": False, "logged_out": True, "problem": "I'm logged out"}
    if "Your orders will show here" in text:
        return {"name": "Shopify store", "ok": True, "orders": 0, "summary": "no orders yet"}
    numbers = sorted(set(re.findall(r"#(\d{4,})\b", text)))
    if not numbers:
        return {"name": "Shopify store", "ok": False, "problem": "I couldn't read the orders list"}
    unfulfilled = len(re.findall(r"\bUnfulfilled\b", text))
    summary = f"{len(numbers)} order(s) on the orders page (latest #{numbers[-1]})"
    if unfulfilled:
        summary += f", {unfulfilled} still to fulfil"
    return {"name": "Shopify store", "ok": True, "orders": len(numbers), "summary": summary}


def fiverr_check():
    status = jb.fiverr_status()
    if status.get("blocked"):
        return {"name": "Fiverr", "ok": False, "problem": "it's showing a human check"}
    if not status.get("logged_in"):
        return {"name": "Fiverr", "ok": False, "logged_out": True, "problem": "I'm logged out"}
    if status.get("error"):
        return {"name": "Fiverr", "ok": False, "problem": status["error"]}
    orders = status["orders"]
    total = sum(orders.values())
    done = orders.get("Completed", 0)
    active = orders.get("Active", 0) + orders.get("Priority", 0)
    if total == 0:
        summary = "no orders yet"
    else:
        summary = f"{total} order(s): {active} active, {done} completed"
    return {"name": "Fiverr", "ok": True, "orders": total, "summary": summary}


def adsense_check():
    try:
        url, text = _read(ADSENSE_HOME, settle=6.0)
    except Exception as error:
        return {"name": "Blog ads (AdSense)", "ok": False, "problem": f"the page didn't load ({error})"}
    if "accounts.google.com" in url:
        return {"name": "Blog ads (AdSense)", "ok": False, "logged_out": True, "problem": "I'm logged out"}
    if "get you started" in text.lower():
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        required = [lines[i - 1] for i, ln in enumerate(lines) if ln == "Required" and i > 0]
        todo = f" -- still needs: {', '.join(required)}" if required else ""
        return {"name": "Blog ads (AdSense)", "ok": True, "earning": False,
                "summary": f"not earning yet, the account isn't fully set up{todo}"}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    found = []
    for i, ln in enumerate(lines):
        if re.search(r"(?i)^(today so far|yesterday|last 7 days|this month|balance)", ln):
            for nxt in lines[i + 1:i + 4]:
                money = _MONEY_RE.search(nxt)
                if money:
                    found.append(f"{ln.lower()} {money.group(0)}")
                    break
    if not found:
        return {"name": "Blog ads (AdSense)", "ok": False, "problem": "I couldn't read the earnings figures"}
    return {"name": "Blog ads (AdSense)", "ok": True, "summary": ", ".join(found)}


def redbubble_check():
    try:
        info = jb.goto(REDBUBBLE_SALES, settle=4.0)
        url, text = info["url"], jb.read_text(12000)
    except Exception as error:
        return {"name": "Redbubble", "ok": False, "problem": f"the page didn't load ({error})"}
    if jb._looks_blocked(text):
        return {"name": "Redbubble", "ok": False, "problem": "it's showing a human check"}
    # Logged-out visitors get a 404 or the login page (behind a cookie banner),
    # and public pages carry promo prices -- so only trust figures on a page
    # that is clearly Danny's own logged-in dashboard.
    if "/auth/login" in url or "404" in info.get("title", "") or not re.search(r"(?i)\b(log ?out|sign ?out)\b", text):
        return {"name": "Redbubble", "ok": False, "logged_out": True, "problem": "I'm logged out"}
    if "open your shop and start selling" in text:
        designs = re.search(r"Add designs\s*\n\s*(\d+)\s*/\s*(\d+)", text)
        todo = []
        if designs and int(designs.group(1)) < int(designs.group(2)):
            todo.append(f"{designs.group(1)} of the {designs.group(2)} designs needed")
        for step in ("Add your payment details", "Add your name & address", "Confirm your mobile phone number"):
            if step in text:
                todo.append(step.lower().replace("add your ", "your ").replace("confirm your ", "your "))
        extra = f" -- it has {todo[0]}" + (f", and still needs {', '.join(todo[1:])}" if len(todo) > 1 else "") if todo else ""
        return {"name": "Redbubble", "ok": True, "summary": f"the shop isn't open yet, so it can't sell anything{extra}"}
    if re.search(r"(?i)no sales|haven't made any sales|you have no sales", text):
        return {"name": "Redbubble", "ok": True, "summary": "no sales yet"}
    money = _MONEY_RE.findall(text)
    sales = re.search(r"(?i)(\d+)\s+sales?\b", text)
    if not money and not sales:
        return {"name": "Redbubble", "ok": False, "problem": "I couldn't read the sales page"}
    bits = []
    if sales:
        bits.append(f"{sales.group(1)} sale(s)")
    if money:
        bits.append(f"earnings shown: {money[0]}")
    return {"name": "Redbubble", "ok": True, "summary": ", ".join(bits)}


CHECKS = (shopify_check, fiverr_check, redbubble_check, adsense_check)


def money_status():
    results = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as error:
            results.append({"name": check.__name__.replace("_check", "").title(), "ok": False,
                            "problem": f"the check crashed ({error})"})
    return results


def describe(results):
    parts = []
    for r in results:
        if r["ok"]:
            parts.append(f"{r['name']}: {r['summary']}.")
        else:
            parts.append(f"{r['name']}: I couldn't check it -- {r['problem']}.")
    logged_out = [r["name"] for r in results if r.get("logged_out")]
    if logged_out:
        parts.append(f"Please log in once in my browser window for {', '.join(logged_out)}, sir, and I'll stay logged in.")
    return " ".join(parts)


if __name__ == "__main__":
    print(describe(money_status()))
