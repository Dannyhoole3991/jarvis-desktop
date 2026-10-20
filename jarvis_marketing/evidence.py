"""
Evidence with provenance. Facts are kept apart from estimates, hypotheses and predictions.

  fact        something a source said. It must cite a recorded fetch (adapter + URL + snapshot), and its
              quote must appear word for word in that snapshot. Any number in the value must appear in the quote.
  verified    a fact read from a first-party system of record (our own Shopify orders, our own
              platform analytics), the same rules apply.
  estimate    a number we worked out. It must say how (basis), e.g. "USD price x fixed rate 0.75".
  hypothesis  an idea to test (an ad angle, a target customer). Never used as input to scoring.
  prediction  a forecast (QA scores, expected CVR). Never reported as a result.

Anything an agent (or a language model) offers as a fact that fails these checks is refused with
FabricatedEvidence. Simulated data stays simulated: a fact from a mock fetch is always simulated.
"""

import hashlib
import json
import re

FACT, VERIFIED, ESTIMATE, HYPOTHESIS, PREDICTION = "fact", "verified", "estimate", "hypothesis", "prediction"
LABELS = (FACT, VERIFIED, ESTIMATE, HYPOTHESIS, PREDICTION)
SOURCED = (FACT, VERIFIED)
FIRST_PARTY_ADAPTERS = {"shopify_orders_api", "shopify_orders_browser", "tiktok_studio", "mock_orders", "mock_analytics",
                        "shopify_public", "mock_store"}
SNAPSHOT_LIMIT = 400_000


class FabricatedEvidence(ValueError):
    pass


def _norm(text):
    return " ".join(str(text or "").split())


def record_fetch(store, business_id, adapter, url, content, ok=True, simulated=False):
    """Every read from the outside world is kept (trimmed) so later claims can be checked against it."""
    text = content if isinstance(content, str) else json.dumps(content, sort_keys=True, default=str)
    return store.insert("fetches", business_id=business_id, adapter=adapter, url=url, ok=bool(ok), fetched_at=store.clock(),
                        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), snapshot=text[:SNAPSHOT_LIMIT],
                        simulated=bool(simulated))


def json_quote(key, value):
    """The exact fragment json.dumps writes for "key": value inside a snapshot."""
    return f"{json.dumps(key)}: {json.dumps(value, sort_keys=True, default=str)}"


def item_quote(item):
    """The exact JSON one list item has inside a snapshot (keeps a value tied to ITS product, not a neighbour's)."""
    return json.dumps(item, sort_keys=True, default=str)


def _numbers(value):
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, (list, tuple)):
        return [n for v in value for n in _numbers(v)]
    if isinstance(value, dict):
        return [n for v in value.values() for n in _numbers(v)]
    return []


def _number_in(n, text):
    for m in re.finditer(r"-?\d+(?:\.\d+)?", text):
        try:
            if abs(float(m.group(0)) - float(n)) < 1e-9:
                return True
        except ValueError:
            continue
    return False


def add(store, business_id, subject_type, subject_key, label, field, claim, value=None, unit=None, confidence=0.5,
        source_url=None, source_name=None, fetch_id=None, quote=None, basis=None, observed_at=None,
        model_version="jarvis-marketing/1 (rules)", simulated=None):
    if label not in LABELS:
        raise ValueError(f"label must be one of {LABELS}")
    if not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if not str(claim or "").strip():
        raise ValueError("a claim needs words")
    fetch = None
    if label in SOURCED:
        if not fetch_id:
            raise FabricatedEvidence(f"'{claim}': a {label} must cite a recorded source.")
        fetch = store.get("fetches", fetch_id)
        if not fetch or not fetch["ok"]:
            raise FabricatedEvidence(f"'{claim}': the cited source was not read successfully.")
        if source_url and source_url != fetch["url"]:
            raise FabricatedEvidence(f"'{claim}': the URL doesn't match the page that was read.")
        if not quote or _norm(quote) not in _norm(fetch["snapshot"]):
            raise FabricatedEvidence(f"'{claim}': the quoted text isn't in the source.")
        for n in _numbers(value):
            if not _number_in(n, quote):
                raise FabricatedEvidence(f"'{claim}': the value {n} isn't in the quoted source text.")
        if label == VERIFIED and fetch["adapter"] not in FIRST_PARTY_ADAPTERS:
            raise FabricatedEvidence(f"'{claim}': only our own systems of record can verify.")
        source_url = fetch["url"]
        source_name = source_name or fetch["adapter"]
        observed_at = observed_at or fetch["fetched_at"]
        if simulated is False and fetch["simulated"]:
            raise FabricatedEvidence("Simulated data can't be recorded as real.")
        simulated = bool(fetch["simulated"])
    elif label == ESTIMATE and not str(basis or "").strip():
        raise ValueError(f"'{claim}': an estimate must say how it was worked out (basis).")
    if observed_at and observed_at > store.clock() + 60:
        raise FabricatedEvidence(f"'{claim}': observed in the future.")
    return store.insert("evidence", business_id=business_id, subject_type=subject_type, subject_key=str(subject_key),
                        label=label, field=field, claim=str(claim)[:500], value_json=value, unit=unit,
                        source_url=source_url, source_name=source_name, fetch_id=fetch_id, quote=(quote or None),
                        basis=basis, observed_at=observed_at, collected_at=store.clock(), confidence=float(confidence),
                        simulated=bool(simulated), model_version=model_version)


def fact(store, business_id, subject_type, subject_key, field, claim, fetch_id, snapshot_obj_key, value, unit=None,
         confidence=0.9):
    """A fact read from a JSON snapshot: the quote is the exact "key": value fragment."""
    return add(store, business_id, subject_type, subject_key, FACT, field, claim, value=value, unit=unit,
               confidence=confidence, fetch_id=fetch_id, quote=json_quote(snapshot_obj_key, value))


def for_subject(store, subject_type, subject_key, labels=None):
    rows = store.find("evidence", subject_type=subject_type, subject_key=str(subject_key))
    return [r for r in rows if labels is None or r["label"] in labels]


def facts(store, subject_type, subject_key):
    """{field: value} from facts/verified only (the latest per field). Estimates never leak in here."""
    out = {}
    for r in for_subject(store, subject_type, subject_key, SOURCED):
        out[r["field"]] = r["value"]
    return out


def estimates(store, subject_type, subject_key):
    out = {}
    for r in for_subject(store, subject_type, subject_key, (ESTIMATE,)):
        out[r["field"]] = r["value"]
    return out
