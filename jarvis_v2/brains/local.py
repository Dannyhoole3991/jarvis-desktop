"""
Fallback brain 2: the local model through Ollama (free, runs on the PC), with the same tools.
Used when Claude is out of usage AND OpenAI is unavailable or out of credit.
"""

import json
import time
import urllib.request

from .. import config, persona, tools
from . import BrainUnavailable


def _ollama_tools():
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools.specs()]


def _post(url, payload, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class LocalBrain:
    name = "local"
    label = "local AI"

    def __init__(self, post=_post, model=None, clock=time.time, call_tool=None):
        self.post = post
        self.model = model or config.OLLAMA_MODEL
        self.clock = clock
        self.call_tool = call_tool or tools.call
        self.last_tools = []

    def ask(self, text, history=None, preamble=""):
        messages = [{"role": "system", "content": persona.system_prompt(fallback=True)}]
        for h in (history or [])[-config.HANDOVER_TURNS:]:
            messages += [{"role": "user", "content": h["user"]}, {"role": "assistant", "content": h["jarvis"]}]
        messages.append({"role": "user", "content": text})
        self.last_tools = []
        for _ in range(config.MAX_TOOL_ROUNDS):
            try:
                resp = self.post(config.OLLAMA_HOST + "/api/chat",
                                 {"model": self.model, "messages": messages, "tools": _ollama_tools(), "stream": False})
            except Exception as error:
                raise BrainUnavailable("offline", self.clock() + 120, f"the local AI (Ollama) isn't answering ({error})")
            msg = resp.get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                return (msg.get("content") or "").strip()
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                fn = c.get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                self.last_tools.append(fn.get("name"))
                messages.append({"role": "tool", "tool_name": fn.get("name"), "content": self.call_tool(fn.get("name"), args)})
        return "I got lost in my tools on that one, sir. Could you ask it a different way?"
