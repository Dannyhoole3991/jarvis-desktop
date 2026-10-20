"""
The rules, in code. Agents get a read-only view (AgentControls); only a HumanPrincipal, which the
authenticated server layer mints for Danny, can change the mode, the caps or approvals, or resume
after an emergency stop. Nothing an agent returns is ever turned into a permission.

Milestone 1 (Danny's decision, 2026-10-20):
  * research / review run freely;
  * ORGANIC posts are allowed in TEST mode, each one only with Danny's approval of that exact post;
  * paid ads, spend, purchases and price changes are OFF below, as constants. Raising them is a
    code change Danny reviews, never a setting the department can reach.
"""

import hashlib
import json

RESEARCH, REVIEW, TEST, SCALE = "RESEARCH", "REVIEW", "TEST", "SCALE"
MODES = (RESEARCH, REVIEW, TEST, SCALE)
MAX_MODE = TEST                      # SCALE stays refused until Danny sets limits

PAID_ADS_ENABLED = False
PURCHASES_ENABLED = False
PRICE_CHANGES_ENABLED = False
HARD_SPEND_CEILING = 0.0             # no cap may exceed this while paid ads are disabled

ALL = set(MODES)
PERMISSIONS = {
    "research": ALL, "evaluate": ALL, "report": ALL, "learn": ALL, "read_analytics": ALL, "read_orders": ALL,
    "brief": {REVIEW, TEST, SCALE}, "produce": {REVIEW, TEST, SCALE}, "qa": {REVIEW, TEST, SCALE},
    "design_experiment": {REVIEW, TEST, SCALE}, "request_approval": {REVIEW, TEST, SCALE},
    "publish_organic": {TEST, SCALE},
    # Hard-off in this milestone, whatever the mode:
    "paid_spend": set(), "launch_paid": set(), "scale_budget": set(), "purchase": set(), "price_change": set(),
}
HARD_OFF = {"paid_spend": PAID_ADS_ENABLED, "launch_paid": PAID_ADS_ENABLED, "scale_budget": PAID_ADS_ENABLED,
            "purchase": PURCHASES_ENABLED, "price_change": PRICE_CHANGES_ENABLED}
NEEDS_APPROVAL = {"publish_organic", "launch_paid", "scale_budget", "purchase", "price_change"}
APPROVAL_SECONDS = 24 * 3600


class PermissionDenied(Exception):
    pass


class HumanPrincipal:
    """Danny, as established by the server: the window's UI token or the laptop login. Agents never get one."""

    def __init__(self, name, via):
        if via not in ("window", "laptop", "terminal", "test"):
            raise ValueError("unknown login route")
        self.name, self.via = str(name), via

    def __str__(self):
        return f"{self.name} ({self.via})"


def _require_human(principal):
    if not isinstance(principal, HumanPrincipal):
        raise PermissionDenied("Only Danny can do that, from the dashboard.")


def payload_hash(payload):
    """The exact content an approval covers. Any change (caption, video, link) makes a new hash."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def mode_index(mode):
    return MODES.index(mode)


class Controls:
    def __init__(self, store):
        self.store = store

    # ---------------------------------------------------------- reads
    def _get(self, key, default):
        with self.store.conn() as c:
            row = c.execute("SELECT value_json FROM controls WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key, value, by):
        with self.store.lock, self.store.conn() as c:
            c.execute("INSERT INTO controls (key, value_json, updated_at, updated_by) VALUES (?,?,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at, "
                      "updated_by=excluded.updated_by", (key, json.dumps(value), self.store.clock(), str(by)))

    def mode(self, business_id):
        return self._get(f"mode:{business_id}", RESEARCH)

    def stopped(self):
        return self._get("emergency_stop", {"on": False})

    def caps(self, business_id):
        return self._get(f"caps:{business_id}", {"total": 0.0, "per_campaign": 0.0})

    def spent(self, business_id, campaign_id=None):
        return 0.0          # nothing can be spent in this milestone; real spend comes from the ad platform later

    def allowed(self, business_id, action):
        if action not in PERMISSIONS:
            return False
        if action in HARD_OFF and not HARD_OFF[action]:
            return False
        return self.mode(business_id) in PERMISSIONS[action] and not self.stopped()["on"]

    def summary(self, business_id):
        mode = self.mode(business_id)
        return {"mode": mode, "max_mode": MAX_MODE, "emergency_stop": self.stopped(), "caps": self.caps(business_id),
                "spent": self.spent(business_id),
                "hard_off": {"paid_ads": not PAID_ADS_ENABLED, "purchases": not PURCHASES_ENABLED,
                             "price_changes": not PRICE_CHANGES_ENABLED},
                "allowed": sorted(a for a in PERMISSIONS if self.allowed(business_id, a)),
                "modes": [{"mode": m, "allowed": sorted(a for a, ms in PERMISSIONS.items() if m in ms and HARD_OFF.get(a, True)),
                           "available": mode_index(m) <= mode_index(MAX_MODE)} for m in MODES]}

    # ---------------------------------------------------------- Danny only
    def set_mode(self, principal, business_id, mode, reason=""):
        _require_human(principal)
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        current = self.mode(business_id)
        if mode_index(mode) > mode_index(MAX_MODE):
            self.store.audit(principal, "set_mode", "refused", subject=mode, business_id=business_id, reason="above MAX_MODE")
            raise PermissionDenied(f"{mode} is disabled in this milestone (paid spend is off until you set limits).")
        if mode_index(mode) > mode_index(current) + 1:
            raise PermissionDenied(f"Move one step at a time: {current} can go up to {MODES[mode_index(current) + 1]}.")
        self._set(f"mode:{business_id}", mode, principal)
        self.store.audit(principal, "set_mode", "ok", subject=f"{current}->{mode}", business_id=business_id, reason=reason)
        return mode

    def set_caps(self, principal, business_id, total, per_campaign):
        _require_human(principal)
        total, per_campaign = float(total), float(per_campaign)
        if total < 0 or per_campaign < 0 or per_campaign > total:
            raise ValueError("Caps must be zero or more, and the per-campaign cap can't exceed the total.")
        if max(total, per_campaign) > HARD_SPEND_CEILING:
            self.store.audit(principal, "set_caps", "refused", business_id=business_id, total=total, per_campaign=per_campaign)
            raise PermissionDenied("Paid spend is disabled in this milestone, so caps stay at 0.")
        self._set(f"caps:{business_id}", {"total": total, "per_campaign": per_campaign}, principal)
        self.store.audit(principal, "set_caps", "ok", business_id=business_id, total=total, per_campaign=per_campaign)

    def emergency_stop(self, by, reason="", business_id=None):
        """Anyone (Danny, the director's watchdog) may STOP. Only Danny may resume."""
        self._set("emergency_stop", {"on": True, "reason": str(reason)[:300], "at": self.store.clock(), "by": str(by)}, by)
        self.store.audit(by, "emergency_stop", "ok", business_id=business_id, reason=reason)
        self.store.notify(f"EMERGENCY STOP: {reason or 'pressed'}. Nothing external will run until Danny resumes.", "urgent", business_id)

    def resume(self, principal, reason=""):
        _require_human(principal)
        self._set("emergency_stop", {"on": False, "reason": "", "at": self.store.clock(), "by": str(principal)}, principal)
        self.store.audit(principal, "resume", "ok", reason=reason)

    # ---------------------------------------------------------- the gate every action passes
    def require(self, business_id, action, actor, approval_id=None, content=None, amount=0.0, campaign_id=None):
        """Raises PermissionDenied unless `action` may happen now. For approval-gated actions this also
        CONSUMES the approval (single use), so the same approval can never cover a second post."""
        def deny(why):
            self.store.audit(actor, action, "refused", business_id=business_id, reason=why, approval_id=approval_id)
            raise PermissionDenied(why)
        if action not in PERMISSIONS:
            deny(f"Unknown action {action}.")
        if self.stopped()["on"]:
            deny("Emergency stop is on.")
        if action in HARD_OFF and not HARD_OFF[action]:
            deny(f"{action} is disabled in code for this milestone.")
        mode = self.mode(business_id)
        if mode not in PERMISSIONS[action]:
            deny(f"{action} isn't allowed in {mode} mode.")
        if amount:
            caps = self.caps(business_id)
            if amount < 0 or self.spent(business_id) + amount > caps["total"] or \
                    self.spent(business_id, campaign_id) + amount > caps["per_campaign"]:
                deny("That would exceed the spend caps.")
        if action in NEEDS_APPROVAL:
            if approval_id is None or content is None:
                deny(f"{action} needs Danny's approval of this exact content.")
            if not Approvals(self.store).consume(approval_id, action, payload_hash(content)):
                deny("No valid approval for this exact content (missing, rejected, expired, already used or content changed).")
        self.store.audit(actor, action, "allowed", business_id=business_id, approval_id=approval_id)


class AgentControls:
    """What an agent may see: never a setter."""

    def __init__(self, controls, business_id):
        self._c, self._bid = controls, business_id

    def mode(self):
        return self._c.mode(self._bid)

    def allowed(self, action):
        return self._c.allowed(self._bid, action)

    def stopped(self):
        return self._c.stopped()["on"]


class Approvals:
    """Danny's yes/no on one exact piece of content. Single use, expires, bound to the content hash."""

    def __init__(self, store):
        self.store = store

    def request(self, business_id, action, subject_type, subject_id, content, summary, requested_by):
        h = payload_hash(content)
        existing = self.store.one("approvals", business_id=business_id, action=action, payload_hash=h, status="pending")
        if existing:
            return existing["id"]
        aid = self.store.insert("approvals", business_id=business_id, action=action, subject_type=subject_type,
                                subject_id=subject_id, payload_hash=h, summary=str(summary)[:1000], status="pending",
                                requested_by=str(requested_by), requested_at=self.store.clock())
        self.store.audit(requested_by, "request_approval", "ok", subject=f"{subject_type}:{subject_id}", business_id=business_id,
                         approval_id=aid, action_requested=action)
        self.store.notify(f"Waiting for your approval: {summary}"[:300], "approval", business_id)
        return aid

    def decide(self, principal, approval_id, decision, note=""):
        _require_human(principal)
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve or reject")
        a = self.store.get("approvals", approval_id)
        if not a:
            raise ValueError("No such approval.")
        if a["status"] != "pending":
            raise ValueError(f"That was already answered ({a['status']}).")
        now = self.store.clock()
        status = "approved" if decision == "approve" else "rejected"
        if not self.store.update("approvals", approval_id, where={"status": "pending"}, status=status, decided_by=str(principal),
                                 decided_at=now, note=str(note)[:300],
                                 expires_at=now + APPROVAL_SECONDS if status == "approved" else None):
            raise ValueError("That approval changed while you were answering it.")
        self.store.audit(principal, f"approval_{status}", "ok", subject=f"{a['subject_type']}:{a['subject_id']}",
                         business_id=a["business_id"], approval_id=approval_id)
        return self.store.get("approvals", approval_id)

    def consume(self, approval_id, action, content_hash):
        now = self.store.clock()
        with self.store.lock, self.store.conn() as c:
            cur = c.execute("UPDATE approvals SET used_at=? WHERE id=? AND action=? AND status='approved' AND used_at IS NULL "
                            "AND payload_hash=? AND expires_at>?", (now, approval_id, action, content_hash, now))
            return cur.rowcount == 1

    def pending(self, business_id=None):
        rows = self.store.find("approvals", status="pending", **({"business_id": business_id} if business_id else {}))
        return rows

    def expire_old(self, max_age=7 * 24 * 3600):
        now = self.store.clock()
        for a in self.store.find("approvals", status="pending"):
            if now - a["requested_at"] > max_age:
                self.store.update("approvals", a["id"], where={"status": "pending"}, status="expired", decided_at=now)

