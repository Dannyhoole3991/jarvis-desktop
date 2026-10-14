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
import threading
import time
from dataclasses import dataclass, field

from . import config
from .brains import BrainUnavailable

ORDER = ("claude", "openai", "local")
LABELS = {"claude": "Claude", "openai": "OpenAI", "local": "my local AI"}
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
    def __init__(self, brains, clock=time.time, state_path=None):
        self.brains = {b.name: b for b in brains}
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
            notes, previous = [], self.state.get("active")
            for name in self._order():
                until = self.blocked_until(name)
                if until and not self.state.get("forced"):
                    continue
                brain = self.brains[name]
                self.state["answering"] = name   # so brain_status is right DURING the turn, not only after it
                self._save()
                preamble = self._handover() if name == "claude" and self.away else ""
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
        return ("[Context, not a question: while you (Claude) were unavailable, a fallback brain handled these "
                "exchanges with Danny. Carry on from them naturally.]\n" + "\n\n".join(lines))

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
    return Router([ClaudeBrain(), OpenAIBrain(), LocalBrain()])
