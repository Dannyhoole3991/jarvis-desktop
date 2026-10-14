"""
The environment every Claude Code process Jarvis starts is given.

One rule, and it is a billing rule: Jarvis's Claude runs on Danny's SUBSCRIPTION,
never on an API key. The CLI silently prefers an inherited ANTHROPIC_API_KEY over
the logged-in account, so every ANTHROPIC_* variable is removed rather than trusted.
CLAUDE_CODE_* / CLAUDECODE are removed too, so a Jarvis started from inside another
Claude Code session does not inherit that session's identity or settings.
"""

import os

SCRUBBED_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_")
SCRUBBED_KEYS = {"CLAUDECODE"}


def child_env(base=None):
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key in SCRUBBED_KEYS or key.upper().startswith(SCRUBBED_PREFIXES):
            del env[key]
    env["PYTHONIOENCODING"] = "utf-8"
    return env
