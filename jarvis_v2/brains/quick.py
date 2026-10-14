"""
The quick voice: Claude Haiku with no tools, for small talk and simple questions.

It runs on the same Claude subscription as the main brain but uses far less of the allowance per
message, and with no tools or Jarvis's long instructions to load it answers just as fast. Anything
that needs tools or live information it hands over by replying "HANDOFF: <short heads-up>".
"""

import datetime
import os
import re

from .. import config, persona
from .claude import ClaudeBrain


class QuickBrain(ClaudeBrain):
    name = "quick"
    label = "Claude (quick)"

    def __init__(self, **kw):
        kw.setdefault("model", config.QUICK_MODEL)
        super().__init__(**kw)
        self.home = os.path.join(config.DATA_DIR, "quick_home")   # empty: none of the main brain's files load

    def command(self):
        return [self.exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--model", self.model, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
                "--system-prompt", persona.QUICK_PERSONA]

    def _start(self):
        os.makedirs(self.home, exist_ok=True)
        super()._start()

    def ask(self, text, history=None, preamble=""):
        now = datetime.datetime.now().strftime("[It is %A %d %B %Y, %H:%M.]")
        answer = super().ask(now + "\n" + text, history, preamble=preamble)
        return re.sub(r"[*`]", "", answer)          # it's spoken: no markdown emphasis
