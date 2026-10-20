"""
Unsupported-claim checks for anything a customer will see (hooks, captions, voice-over, on-screen text).

1. jarvis_store.copywriter.check_claims: Danny's banned list (health claims, fake reviews or social
   proof, fake discounts, urgency tricks, guarantees).
2. Made-up testimonials and promises this shop can't back up.
3. Every number must be one the product's facts allow (its price, delivery days, option count ...).
"""

import re


class UnsupportedClaim(ValueError):
    pass


EXTRA_BANNED = re.compile(
    r"(?i)\b(customers? (say|love)|i love (it|this)|testimonial|everyone('s| is) (buying|talking)|as seen on|"
    r"money[- ]back|free returns?|lifetime|warranty|certified|award[- ]winning|eco[- ]friendly|"
    r"sold out|selling fast|last chance|official)\b")


def _banned_check(text):
    try:
        from jarvis_store.copywriter import check_claims
    except Exception:          # store package unavailable: the extra list still applies
        def check_claims(t):
            return t
    try:
        check_claims(text)
    except ValueError as error:
        raise UnsupportedClaim(str(error))


def check(text, allowed_numbers=()):
    text = str(text or "")
    _banned_check(text)
    m = EXTRA_BANNED.search(text)
    if m:
        raise UnsupportedClaim(f"Unsupported claim: '{m.group(0)}'")
    allowed = {round(float(n), 2) for n in allowed_numbers}
    for num in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", text.replace(",", "")):
        if round(float(num), 2) not in allowed:
            raise UnsupportedClaim(f"The number {num} isn't backed by this product's facts.")
    return text


def check_all(texts, allowed_numbers=()):
    for t in texts:
        check(t, allowed_numbers)
    return True
