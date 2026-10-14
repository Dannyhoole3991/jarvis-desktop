"""
The brain router: Claude first, always; OpenAI, then the local AI, only while Claude can't answer.

  - Each brain that fails is "blocked" until its retry time (for Claude: the moment its usage
    window renews, from Claude Code's own rate-limit event). Blocks are saved to disk, so a
    restart remembers them.
  - Every message tries the brains in order, skipping blocked ones. So the first message after
    Claude's window renews goes straight back to Claude: no manual step.
  - When Claude comes back it is told, briefly, what was said while it was away.
  - A switch is announced once ("My Claude allowance is used up until 18:00, sir ...").
  - Danny can force a brain ("use your local brain") or return to automatic.
"""

import datetime
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

from . import config
from .brains import BrainUnavailable

ORDER = ("claude", "openai", "local")
LABELS = {"claude": "Claude", "openai": "OpenAI", "local": "my local AI", "quick": "Claude (quick)"}

# Clearly a job or a question about Danny's own things: straight to the full brain, no quick-voice hop.
TASK_RE = re.compile(
    r"(?i)\b(check|post|upload|open|launch|start|run|build|make|create|fix|install|search|look up|find|show|read|send|"
    r"remember|recall|note|order|orders|sales|money|earn|earned|store|shop|shopify|product|products|tiktok|views|"
    r"advert|adverts|ads|trading|trade|fiverr|redbubble|blog|adsense|session|sessions|screen|file|files|folder|"
    r"weather|news|price|email|calendar|spec|project|code)\b")
REASONS = {"usage_limit": "my Claude allowance is used up", "no_credit": "OpenAI has no credit",
           "auth": "it needs logging in again", "offline": "it isn't reachable", "error": "it hit an error"}


@dataclass
class Reply:
    text: str
    brain: str
    announcement: str = ""
    tools: list = field(default_factory=list)

    def spoken(self):
        return (self.announcement + " " + self.text).strip() if self.announcement else self.text


def _hhmm(epoch):
    t = datetime.datetime.fromtimestamp(epoch)
    day = "" if t.date() == datetime.date.today() else t.strftime(" on %A")
    return t.strftime("%H:%M") + day


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_state(path=None):
    return _read_json(path or config.BRAIN_STATE_FILE, {"blocked": {}, "forced": None, "active": None})


def status_text(now=None, state_path=None, usage_path=None):
    """Plain-English brain status (works from any process: it reads the saved state)."""
    now = now or time.time()
    st = load_state(state_path)
    current = st.get("answering") or st.get("active")
    parts = [f"Active brain: {LABELS.get(current, 'none yet')}."]
    if st.get("forced"):
        parts.append(f"Danny has fixed it to {LABELS[st['forced']]} (automatic switching is off).")
    for name, b in (st.get("blocked") or {}).items():
        if b.get("until", 0) > now:
            parts.append(f"{LABELS[name]} is unavailable until {_hhmm(b['until'])} because {REASONS.get(b.get('reason'), b.get('reason'))}.")
    try:
        usage = _read_json(usage_path or config.USAGE_FILE, {})
        for key, w in (usage.get("windows") or {}).items():
            util = w.get("utilization")
            pct = f"{util * 100:.0f}%" if isinstance(util, (int, float)) and util <= 1 else (f"{util:.0f}%" if isinstance(util, (int, float)) else "unknown")
            resets = f", resets {_hhmm(w['resets_at'])}" if isinstance(w.get("resets_at"), (int, float)) else ""
            parts.append(f"Claude {key.replace('_', ' ')} window: {pct} used{resets}.")
    except Exception:
        pass
    return " ".join(parts)


class Router:
    def __init__(self, brains, clock=time.time, state_path=None, quick=None):
        self.brains = {b.name: b for b in brains}
        self.quick = quick         # Haiku: small talk first, hands real jobs to the brains above
        self.on_handoff = None     # called with the quick voice's heads-up ("On it, sir.") so it's spoken at once
        self.quick_missed = []     # exchanges the quick voice hasn't seen (answered by the full brain)
        self.clock = clock
        self.state_path = state_path or config.BRAIN_STATE_FILE
        self.state = load_state(self.state_path)
        self.state.setdefault("blocked", {})
        self.history = []          # [{"user", "jarvis", "brain"}]
        self.away = []             # exchanges answered by a fallback brain since Claude last answered
        self.lock = threading.Lock()

    # ------------------------------------------------------------- state
    def _save(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1)

    def blocked_until(self, name):
        b = self.state["blocked"].get(name)
        return b["until"] if b and b.get("until", 0) > self.clock() else None

    def force(self, name):
        """name in ORDER, or None for automatic."""
        if name is not None and name not in self.brains:
            raise ValueError(f"unknown brain {name}")
        self.state["forced"] = name
        if name:
            self.state["blocked"].pop(name, None)   # an explicit choice gets a fresh try
        self._save()
        return f"Using {LABELS[name]} only, sir." if name else "Back to automatic, sir: Claude first."

    # -------------------------------------------------------------- turns
    def _order(self):
        forced = self.state.get("forced")
        return [forced] if forced else [n for n in ORDER if n in self.brains]

    def ask(self, text):
        with self.lock:
            heads_up = ""
            if self._quick_allowed(text):
                reply, heads_up = self._ask_quick(text)
                if reply:
                    return reply
            return self._ask_full(text, heads_up)

    def _quick_allowed(self, text):
        return (self.quick is not None and not self.state.get("forced") and not self.blocked_until("claude")
                and not TASK_RE.search(text))

    def _ask_quick(self, text):
        """(Reply, "") when the quick voice answered; (None, heads_up) when it handed over or couldn't answer."""
        lines = [f"Danny: {e['user']}\nJarvis: {e['jarvis']}" for e in self.quick_missed[-config.HANDOVER_TURNS:]]
        preamble = ("[Context, not a question: your full self handled these exchanges with Danny since you last "
                    "spoke. Carry on from them naturally.]\n" + "\n\n".join(lines)) if lines else ""
        try:
            answer = self.quick.ask(text, self.history, preamble=preamble)
        except BrainUnavailable as e:
            if e.reason == "usage_limit":          # same Claude allowance as the full brain
                self.state["blocked"]["claude"] = {"until": e.retry_at or (self.clock() + config.CLAUDE_DEFAULT_BLOCK_SECONDS),
                                                   "reason": e.reason, "detail": e.detail}
                self._save()
            return None, ""
        self.quick_missed = []
        answer = (answer or "").strip()
        if not answer or answer.upper().startswith("HANDOFF"):
            heads_up = answer.split(":", 1)[1].strip().strip('"') if ":" in answer else ""
            if heads_up and self.on_handoff:
                try:
                    self.on_handoff(heads_up)
                except Exception:
                    pass
            return None, heads_up
        previous = self.state.get("active")
        self.state["active"] = "claude"
        self._save()
        entry = {"user": text, "jarvis": answer, "brain": "quick"}
        self.history = (self.history + [entry])[-50:]
        self.away.append(entry)                    # so the full brain knows what was said
        return Reply(answer, "quick", self._announce(previous, "claude", [])), ""

    def _ask_full(self, text, heads_up=""):
        notes, previous = [], self.state.get("active")
        for name in self._order():
            until = self.blocked_until(name)
            if until and not self.state.get("forced"):
                continue
            brain = self.brains[name]
            self.state["answering"] = name   # so brain_status is right DURING the turn, not only after it
            self._save()
            preamble = self._handover() if name == "claude" and self.away else ""
            if name == "claude" and heads_up:
                preamble = (preamble + "\n\n" if preamble else "") + \
                    f"[Danny has already been told \"{heads_up}\", so don't give another heads-up; just do it.]"
            try:
                answer = brain.ask(text, self.history, preamble=preamble) if name == "claude" else brain.ask(text, self.history)
            except BrainUnavailable as e:
                retry = e.retry_at or (self.clock() + config.CLAUDE_DEFAULT_BLOCK_SECONDS)
                self.state["blocked"][name] = {"until": retry, "reason": e.reason, "detail": e.detail}
                self._save()
                notes.append((name, e.reason, retry))
                continue
            self.state["blocked"].pop(name, None)
            self.state["active"] = name
            self.state.pop("answering", None)
            self._save()
            announcement = self._announce(previous, name, notes)
            entry = {"user": text, "jarvis": answer, "brain": name}
            self.history = (self.history + [entry])[-50:]
            self.quick_missed = (self.quick_missed + [entry])[-config.HANDOVER_TURNS:]
            if name == "claude":
                self.away = []
            else:
                self.away.append(entry)
            return Reply(answer, name, announcement, list(getattr(brain, "last_tools", []) or []))
        self.state.pop("answering", None)
        self._save()
        return Reply("None of my brains can answer right now, sir. " + (status_text(self.clock(), self.state_path)), "none")

    def _handover(self):
        lines = [f"Danny: {e['user']}\nJarvis ({LABELS[e['brain']]}): {e['jarvis']}" for e in self.away[-config.HANDOVER_TURNS:]]
        return ("[Context, not a question: since your last turn, Jarvis's other voices (a fallback brain while you were "
                "unavailable, or his quick voice for small talk) handled these exchanges with Danny. Carry on from them "
                "naturally.]\n" + "\n\n".join(lines))

    def _announce(self, previous, now_on, notes):
        if previous == now_on and not notes:
            return ""
        if now_on == "claude" and previous and previous != "claude":
            return "Claude is back, sir."
        if now_on != "claude":
            claude_note = next((n for n in notes if n[0] == "claude"), None)
            if claude_note and claude_note[1] == "usage_limit":
                return f"My Claude allowance is used up until {_hhmm(claude_note[2])}, sir, so I'm using {LABELS[now_on]} until then."
            if claude_note:
                why = {"auth": "Claude needs logging in again", "offline": "Claude isn't reachable",
                       "error": "Claude hit an error"}.get(claude_note[1], "Claude isn't available")
                return f"{why}, sir, so I'm using {LABELS[now_on]} for now."
            if previous != now_on:
                return f"I'm using {LABELS[now_on]} for now, sir."
        return ""


def default_router():
    from .brains.claude import ClaudeBrain
    from .brains.local import LocalBrain
    from .brains.openai_brain import OpenAIBrain
    config.ensure_dirs()
    from .brains.quick import QuickBrain
    return Router([ClaudeBrain(), OpenAIBrain(), LocalBrain()], quick=QuickBrain())
