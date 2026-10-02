"""
Human-only control actions for the live subsystem: approve a promotion, arm live trading, roll back, retire,
disarm, clear an emergency stop or a risk halt.

Every action that INCREASES what the system may do requires a typed confirmation phrase that names exactly what
is being authorised, and is logged in the append-only promotion log. Actions that REDUCE risk (disarm, emergency
stop) never need one. The research side never imports this module (tests/test_separation.py): nothing that
generates or tests strategies can approve, arm or promote anything.

Two separate steps, on purpose:
  1. approve_promotion -> the strategy becomes the LIVE (approved) version, frozen into a read-only bundle.
     This does NOT enable trading.
  2. arm -> explicitly enables real-money trading for that version with a stated capital. Time-limited.
"""

import calendar
import hashlib
import json
import os
import time

from .. import promotion as PR
from . import bundle, config, creds, paths
from . import limits as L


class ApprovalError(Exception):
    pass


def _norm(text):
    return " ".join(str(text).strip().upper().split())


def _utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _require(typed, expected):
    if _norm(typed) != _norm(expected):
        raise ApprovalError(f"Confirmation did not match. To proceed, type exactly: {expected}")


# ------------------------------------------------------------------ promotion
def approve_promotion(registry, review_id, typed, settings=None, base_dir=None):
    review = next((e for e in registry.promotion_events("review", limit=2000) if e["id"] == review_id), None)
    if review is None:
        raise ApprovalError(f"No promotion review #{review_id} exists.")
    if not review["eligible"]:
        raise ApprovalError("That review found the strategy NOT eligible; it cannot be approved.")
    sid, ver = review["strategy_id"], review["version"]
    _require(typed, PR.approval_phrase(review_id, sid, ver))
    limits = config.build_limits(settings or config.load_settings())
    paths.ensure_dirs()
    manifest = bundle.freeze(registry, sid, ver, limits, review_id, base_dir)
    previous = registry.promote_to_live(sid, ver, review_id, manifest["bundle_hash"], "set at arming",
                                        f"Human approved promotion review #{review_id}")
    disarm(registry, f"live strategy changed to {sid} v{ver}; live trading must be armed again")
    registry.record_promotion_event("approval", sid, ver, True, {"review_id": review_id, "replaced": previous,
                                                                  "bundle_hash": manifest["bundle_hash"]}, "human")
    return {"strategy_id": sid, "version": ver, "replaced": previous, "bundle_hash": manifest["bundle_hash"],
            "note": "Approved as the LIVE strategy. Live trading is still DISABLED until you arm it."}


def rollback(registry, strategy_id, version, typed, base_dir=None):
    _require(typed, f"ROLLBACK TO {strategy_id} v{version}")
    manifest, folder = bundle.load_manifest(strategy_id, version, base_dir)
    ok, problems = bundle.verify(folder)
    if not ok:
        raise ApprovalError("The old bundle failed verification: " + "; ".join(problems))
    previous = registry.rollback_to(strategy_id, version, manifest["bundle_hash"], "set at arming", "Human-requested rollback")
    disarm(registry, f"rolled back to {strategy_id} v{version}; live trading must be armed again")
    registry.record_promotion_event("rollback", strategy_id, version, True, {"replaced": previous}, "human")
    return {"strategy_id": strategy_id, "version": version, "replaced": previous}


def retire_live_strategy(registry, typed):
    live = registry.current_live()
    if not live:
        raise ApprovalError("No strategy is live.")
    _require(typed, "RETIRE LIVE STRATEGY")
    registry.retire_live(live[0], live[1], "Retired by human")
    disarm(registry, "live strategy retired")
    return {"retired": live}


# ----------------------------------------------------------------------- arming
def arming_phrase(strategy_id, version, capital):
    return f"ENABLE LIVE TRADING {strategy_id} v{version} WITH {capital:g} USDT"


def arm(registry, typed, days=30, settings=None, creds_path=None):
    live = registry.current_live()
    if not live:
        raise ApprovalError("No strategy has been approved as LIVE yet.")
    settings = settings or config.load_settings()
    limits = config.build_limits(settings)
    if limits.capital_limit <= 0:
        raise ApprovalError("Set a capital limit first (live_config.json); with none, nothing could trade.")
    if os.path.exists(paths.EMERGENCY_LATCH):
        raise ApprovalError("An emergency stop is latched. Clear it first.")
    manifest, folder = bundle.load_manifest(*live)
    ok, problems = bundle.verify(folder)
    if not ok:
        raise ApprovalError("The live bundle failed verification: " + "; ".join(problems))
    if not creds.have_credentials(creds_path):
        raise ApprovalError("No exchange credentials are stored. Run: python -m jarvis_trading.live.setup_credentials")
    _require(typed, arming_phrase(live[0], live[1], limits.capital_limit))
    armed = {"armed_at": _utc(), "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + days * 86400)),
             "strategy_id": live[0], "version": live[1], "bundle_hash": manifest["bundle_hash"],
             "limits_fingerprint": limits.fingerprint(), "capital_limit": limits.capital_limit,
             "exchange": settings.get("exchange", "binance"),
             "confirmation_sha256": hashlib.sha256(_norm(typed).encode()).hexdigest()}
    _atomic_write(paths.ARMED_FILE, armed)
    registry.record_promotion_event("arm", live[0], live[1], True, armed, "human")
    return armed


def disarm(registry, reason="disarmed by human"):
    """Always allowed, no confirmation: reducing what the system may do is never gated."""
    was_armed = os.path.exists(paths.ARMED_FILE)
    if was_armed:
        os.remove(paths.ARMED_FILE)
    live = registry.current_live() or (None, None)
    registry.record_promotion_event("disarm", live[0], live[1], None, {"reason": reason, "was_armed": was_armed}, "system")
    return was_armed


def read_arming():
    if not os.path.exists(paths.ARMED_FILE):
        return None
    with open(paths.ARMED_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def arming_problems(registry, settings=None, now=None):
    """Everything that must be true for the trader to be allowed to place REAL orders. Empty list = may trade."""
    armed = read_arming()
    if armed is None:
        return ["live trading is not armed"]
    problems = []
    live = registry.current_live()
    if live != (armed["strategy_id"], armed["version"]):
        problems.append("the armed strategy is no longer the live strategy")
        return problems
    if os.path.exists(paths.EMERGENCY_LATCH):
        problems.append("an emergency stop is latched")
    limits = config.build_limits(settings or config.load_settings())
    if limits.fingerprint() != armed["limits_fingerprint"]:
        problems.append("the live limits/capital changed since arming; disarm and re-arm to accept them")
    manifest, folder = bundle.load_manifest(*live)
    ok, bad = bundle.verify(folder)
    if not ok:
        problems.append("the live bundle was modified: " + "; ".join(bad))
    if manifest["bundle_hash"] != armed["bundle_hash"]:
        problems.append("the live bundle is not the one that was armed")
    expiry = calendar.timegm(time.strptime(armed["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    if (now or time.time()) > expiry:
        problems.append("the arming has expired; re-arm to continue")
    return problems


# --------------------------------------------------------------- clearing latches
def clear_emergency(registry, typed):
    _require(typed, "CLEAR EMERGENCY STOP")
    for p in (paths.EMERGENCY_LATCH, paths.EMERGENCY_FILE):
        if os.path.exists(p):
            os.remove(p)
    disarm(registry, "emergency cleared; live trading must be armed again")
    registry.record_promotion_event("emergency", None, None, None, {"action": "cleared"}, "human")


def clear_halt(registry, typed):
    _require(typed, "CLEAR RISK HALT")
    if os.path.exists(paths.HALT_FILE):
        os.remove(paths.HALT_FILE)
    registry.record_promotion_event("emergency", None, None, None, {"action": "risk halt cleared"}, "human")
