"""
Which messages the voice engine keeps for its own PC handlers, and which go to the v2 brain.
The engine file is one big script with a live main loop, so only the routing pieces are loaded.

    py -3 -m unittest jarvis_v2.tests.test_engine_routing -v
"""

import ast
import os
import re
import unittest

from jarvis_v2 import config

NAMES = {"_QUESTION_RE", "_LOCAL_QUESTION_RE", "_is_plain_question", "_DIRECT_PC_RE", "BRAIN_PC_MARKER",
         "_is_direct_pc_command", "_goes_to_v2_brain"}


def _load():
    path = os.path.join(config.REPO_DIR, "Jarvis_FINAL_WORKING.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in NAMES)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", None) in NAMES for t in n.targets))]
    ns = {"re": re}
    exec(compile(ast.Module(body=keep, type_ignores=[]), path, "exec"), ns)
    return ns


E = _load()


class EngineRoutingTests(unittest.TestCase):
    def test_tonights_pasted_needs_you_text_goes_to_the_brain(self):
        pasted = ('Returns address. Sign up for a UK address that accepts parcels and customer returns. A normal '
                  '"virtual office" usually only takes letters, so make sure it says it accepts parcels. Example: '
                  'Warehouse Address, from about £11.99/month: https://warehouseaddress.com/collections/virtual-address')
        for text in (pasted.lower(), "i copied what you said in the needs you section",
                     "so how would a warehouse work for 11.99", "sign up for a uk address", "how are you"):
            self.assertTrue(E["_goes_to_v2_brain"](text), text)

    def test_short_direct_pc_commands_stay_with_the_engine(self):
        for text in ("open steam", "jarvis open chrome", "volume up", "let's play rust", "i'm ready to play spider-man",
                     "turn the volume down", "take a screenshot", "what time is it", "close spotify", "play spider-man on xbox"):
            self.assertFalse(E["_goes_to_v2_brain"](text), text)

    def test_long_or_multi_sentence_text_is_never_a_pc_command(self):
        self.assertFalse(E["_is_direct_pc_command"]("open the store. then post the video. then tell me the views."))
        self.assertFalse(E["_is_direct_pc_command"]("open https://example.com"))
        self.assertFalse(E["_is_direct_pc_command"]("open " + "very " * 20 + "long thing"))

    def test_the_brains_own_pc_jobs_never_bounce_back_to_it(self):
        self.assertFalse(E["_goes_to_v2_brain"]("sign up for a uk address", from_brain=True))
        self.assertTrue(E["BRAIN_PC_MARKER"].startswith("\x00"))

    def test_pc_command_tool_marks_itself_as_from_the_brain(self):
        src = open(os.path.join(config.REPO_DIR, "jarvis_v2", "tools.py"), encoding="utf-8").read()
        self.assertIn('"from_brain": True', src)


if __name__ == "__main__":
    unittest.main()
