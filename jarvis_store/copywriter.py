"""
Honest product copy built only from CJ's own facts.

No model is asked to invent anything: titles, descriptions, video lines and
TikTok captions are assembled from the supplier's data (name, options,
material, size, what's in the box). `check_claims` is the last line of
defence and rejects anything Danny has ruled out: health claims, fake
reviews or social proof, fake discounts/was-prices, and urgency tricks.
"""

import html
import re

from . import config

BANNED = re.compile(
    r"(?i)\b(cure|cures|heal|heals|healing|pain relief|relieves? pain|therapeutic|medical|clinically|doctor|"
    r"detox|weight loss|anti[- ]?aging|best[- ]?sell\w*|#1|number one|top rated|5[- ]?star|reviews?|rated|"
    r"was £|now only|\d+% off|sale|discount|limited time|hurry|only \d+ left|guarantee[ds]?|"
    r"100% (?:safe|natural|effective)|miracle|viral|everyone is)\b")

# Words in CJ titles that are marketing noise, years, or other-language filler.
_NOISE = re.compile(r"(?i)\b(new|hot|sale|selling|best|2019|2020|2021|2022|2023|2024|2025|2026|xmas|navidad|"
                    r"creative|fashion|household|home|for|with|and|the|a|an|of|in|decor|decoration|decorations|"
                    r"ornament|ornaments|gift|gifts|style|cross[- ]border|amazon|tiktok|wholesale|dropshipping)\b")

FACT_KEYS = ("material", "size", "specification", "dimension", "capacity", "power supply", "battery", "power",
             "color", "colour", "weight", "length", "width", "height", "packing list", "package", "quantity")


def check_claims(text):
    """Raises ValueError if `text` contains a claim Danny has ruled out."""
    m = BANNED.search(text or "")
    if m:
        raise ValueError(f"Blocked claim in copy: '{m.group(0)}'")
    return text


def clean_title(cj_name, max_len=60):
    """A short, readable title made only from words already in CJ's name."""
    words = re.sub(r"[^A-Za-z0-9&+\- ]", " ", cj_name).split()
    kept, seen = [], set()
    for w in words:
        lw = w.lower()
        if lw in seen or _NOISE.fullmatch(lw):
            continue
        seen.add(lw)
        kept.append(w if w.isupper() and len(w) <= 4 else w.capitalize())
        if len(" ".join(kept)) >= max_len - 8:
            break
    if not kept:
        kept = [w.capitalize() for w in words[:6]]
    title = " ".join(kept)[:max_len].strip(" -&+")
    return check_claims(title)


def facts_from_description(description_html):
    """Key: value facts from CJ's description (Material, Size, Packing list ...)."""
    text = html.unescape(re.sub(r"<[^>]+>", "\n", description_html or ""))
    facts = {}
    for line in text.splitlines():
        m = re.match(r"\s*\[?([A-Za-z ]{3,25})\]?\s*[:：]\s*(.{1,120})", line)
        if not m:
            continue
        key, value = m.group(1).strip().lower(), m.group(2).strip()
        if any(k == key or key.startswith(k) for k in FACT_KEYS) and value and key not in facts:
            try:
                facts[key] = check_claims(value)
            except ValueError:
                continue
    return facts


def option_summary(options):
    parts = []
    for o in options:
        values = o["values"]
        if len(values) == 1:
            continue
        joined = ", ".join(values[:-1]) + " or " + values[-1] if len(values) > 1 else values[0]
        parts.append(f"{o['name']}: {joined}")
    return parts


def description_html(title, facts, options, delivery_days):
    bullets = []
    for key in ("material", "size", "specification", "dimension", "capacity", "power supply", "battery", "power"):
        if key in facts:
            bullets.append(f"{key.capitalize()}: {facts[key]}")
    bullets += option_summary(options)
    if "packing list" in facts or "package" in facts:
        bullets.append(f"In the box: {facts.get('packing list') or facts.get('package')}")
    lo, hi = delivery_days
    body = [f"<p>{html.escape(title)}.</p>"]
    if bullets:
        body.append("<ul>" + "".join(f"<li>{html.escape(b)}</li>" for b in bullets) + "</ul>")
    body.append(f"<p><strong>Delivery:</strong> free tracked UK delivery, usually {lo}-{hi} working days.</p>")
    return check_claims("".join(body))


def video_lines(title, options):
    """Six short spoken/caption lines for jarvis_video (facts only)."""
    lines = [f"Meet the {title}.", "Here's a closer look."]
    for o in options:
        if len(o["values"]) > 1:
            vals = o["values"][:4]
            lines.append(f"Choose your {o['name'].lower()}: " + ", ".join(vals[:-1]) + " or " + vals[-1] + ".")
            break
    lines.append("Simple, useful, and made for everyday life.")
    lines.append("Free UK delivery. Shop at Collective Finds!")
    for line in lines:
        check_claims(line)
    return lines


def tiktok_caption(title, price_gbp):
    caption = (f"{title} ✨ £{price_gbp:.2f} with free UK delivery 🛒 {config.SHOP_PUBLIC_URL}\n"
               "#tiktokmademebuyit #homefinds #usefulgadgets #ukshop #collective")
    return check_claims(caption)
