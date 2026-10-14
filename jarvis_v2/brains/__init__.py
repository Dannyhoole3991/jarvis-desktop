"""
The three brains. Each has the same shape:

    brain.ask(text, history) -> str          the reply
    raises BrainUnavailable(reason, retry_at) when it can't answer right now

`retry_at` (epoch seconds) is when the router should try this brain again: for Claude
it is the usage window's reset time, for OpenAI a while after "no credit".
"""


class BrainUnavailable(Exception):
    def __init__(self, reason, retry_at=None, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason          # "usage_limit" | "no_credit" | "auth" | "offline" | "error"
        self.retry_at = retry_at
        self.detail = detail
