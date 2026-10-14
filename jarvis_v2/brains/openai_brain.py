"""
Fallback brain 1: OpenAI, with the same tools through function calling.

"No credit" (insufficient_quota) and auth failures become BrainUnavailable so the
router drops to the local brain, and tries OpenAI again later.
"""

import json
import time

from .. import config, persona, tools
from . import BrainUnavailable


def _openai_tools():
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools.specs()]


class OpenAIBrain:
    name = "openai"
    label = "OpenAI"

    def __init__(self, client=None, model=None, clock=time.time, call_tool=None):
        self._client = client
        self.model = model or config.OPENAI_MODEL
        self.clock = clock
        self.call_tool = call_tool or tools.call
        self.last_tools = []

    def client(self):
        if self._client is None:
            try:
                from openai import OpenAI
                self._client = OpenAI(timeout=90)
            except Exception as error:
                raise BrainUnavailable("auth", self.clock() + config.OPENAI_RETRY_SECONDS, f"OpenAI isn't set up ({error})")
        return self._client

    def _classify(self, error):
        text = f"{type(error).__name__} {error}".lower()
        if "insufficient_quota" in text or "exceeded your current quota" in text or "billing" in text:
            return BrainUnavailable("no_credit", self.clock() + config.OPENAI_RETRY_SECONDS, "OpenAI has no credit left")
        if "authentication" in text or "api key" in text or "401" in text:
            return BrainUnavailable("auth", self.clock() + config.OPENAI_RETRY_SECONDS, "OpenAI rejected the key")
        if "connection" in text or "timeout" in text:
            return BrainUnavailable("offline", self.clock() + 120, "couldn't reach OpenAI")
        return BrainUnavailable("error", self.clock() + 120, str(error)[:200])

    def ask(self, text, history=None, preamble=""):
        messages = [{"role": "system", "content": persona.system_prompt(fallback=True)}]
        for h in (history or [])[-config.HANDOVER_TURNS:]:
            messages += [{"role": "user", "content": h["user"]}, {"role": "assistant", "content": h["jarvis"]}]
        messages.append({"role": "user", "content": text})
        self.last_tools = []
        for _ in range(config.MAX_TOOL_ROUNDS):
            try:
                resp = self.client().chat.completions.create(model=self.model, messages=messages, tools=_openai_tools())
            except BrainUnavailable:
                raise
            except Exception as error:
                raise self._classify(error)
            msg = resp.choices[0].message
            calls = getattr(msg, "tool_calls", None) or []
            if not calls:
                return (msg.content or "").strip()
            messages.append({"role": "assistant", "content": msg.content or "",
                             "tool_calls": [{"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}} for c in calls]})
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except ValueError:
                    args = {}
                self.last_tools.append(c.function.name)
                messages.append({"role": "tool", "tool_call_id": c.id, "content": self.call_tool(c.function.name, args)})
        return "I got lost in my tools on that one, sir. Could you ask it a different way?"
