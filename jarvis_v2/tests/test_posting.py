"""
TikTok posting: the queue, the safety checks, and the post flow against a fake browser.
Nothing here touches TikTok.

    py -3 -m unittest jarvis_v2.tests.test_posting -v
"""

import os
import tempfile
import unittest
from unittest import mock

from jarvis_v2 import posting as PO


class FakeButton:
    def __init__(self, page, name):
        self.page, self.name = page, name

    def count(self):
        return 1 if self.name in self.page.buttons else 0

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def is_visible(self):
        return True

    def click(self):
        self.page.clicked.append(self.name)


class FakeEditor:
    def __init__(self, page):
        self.page = page
        self.first = self

    def click(self):
        self.page.clicked.append("editor")

    def inner_text(self):
        return self.page.editor_text

    def count(self):
        return 1


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def type(self, text, delay=0):
        self.page.editor_text += text

    def press(self, key):
        if key == "Backspace":
            self.page.editor_text = self.page.editor_text[:-1]


class FakeSuggestion:
    """One entry in TikTok's hashtag dropdown."""
    def __init__(self, page, tag):
        self.page, self.tag = page, tag

    def locator(self, sel):
        return self

    def inner_text(self):
        return self.tag

    def click(self):
        self.page.mentions.append(self.tag)
        self.page.editor_text += " "


class FakeSuggestions:
    def __init__(self, page):
        word = page.editor_text.split()[-1] if page.editor_text.split() else ""
        self.items = [FakeSuggestion(page, word)] if word.startswith("#") and word in page.known_tags else []

    def count(self):
        return len(self.items)

    def nth(self, i):
        return self.items[i]


class FakePage:
    def __init__(self, mangle=False, known_tags=("#christmasdecor", "#collective")):
        self.buttons = {"Got it", "Cancel", "DISCARD", "Discard"}
        self.clicked, self.files, self.mentions = [], [], []
        self.known_tags = set(known_tags)
        self.editor_text = "1_velvet_bow"
        self.mangle = mangle
        self.url = "https://www.tiktok.com/tiktokstudio/upload"
        self.keyboard = FakeKeyboard(self)

    def set_input_files(self, sel, path):
        self.files.append(path)

    def get_by_role(self, role, name, exact=True):
        return FakeButton(self, name)

    def locator(self, sel):
        if "post_video_button" in sel:
            return FakeButton(self, "POST")
        if "discard_post_button" in sel:
            return FakeButton(self, "DISCARD")
        if "hashtag-suggestion-item" in sel:
            return FakeSuggestions(self)
        if "mentionText" in sel:
            return mock.Mock(all=lambda: [mock.Mock(inner_text=lambda t=t: t) for t in self.mentions])
        return FakeEditor(self)

    def evaluate(self, script, text=None):
        self.editor_text = (text[:-10] if self.mangle else text)


class FakeBrowser:
    def __init__(self, page, blocked=False, appears=True):
        self.page, self.blocked, self.appears = page, blocked, appears
        self.at = ""
        self._worker = mock.Mock(run=lambda fn, timeout=None: fn(self.page))

    def goto(self, url, settle=0):
        self.at = url
        return {"url": url, "title": "TikTok Studio"}

    def current(self):
        return {"url": self.at}

    def read_text(self, n=0):
        if self.blocked:
            return "Verify you are human"
        if self.at.endswith("/content") and self.appears and "POST" in self.page.clicked:
            return "Posts 5\n00:15\n" + self.page.editor_text + "\n14 Oct, 9:00 pm\nPublic\n0\n0\n0"
        return "Uploaded（4.8MB）\nDescription\n"

    @staticmethod
    def _looks_blocked(text):
        return "human" in text


class PostingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "generated_videos")
        os.makedirs(os.path.join(self.root, "christmas", "TIKTOK"))
        self.video = os.path.join(self.root, "christmas", "TIKTOK", "1_velvet_bow.mp4")
        with open(self.video, "wb") as f:
            f.write(b"\x00")
        with open(os.path.join(self.root, "christmas", "CHRISTMAS_TIKTOK_CAPTIONS.md"), "w", encoding="utf-8") as f:
            f.write("# t\n\n## 1. Velvet Christmas Tree Bow\n```\nThe finishing touch 🎀 From £18.99\n#christmasdecor #collective\n```\n")
        self.patches = [mock.patch.object(PO, "QUEUE_FILE", os.path.join(self.tmp.name, "q.json")),
                        mock.patch.object(PO, "VIDEO_ROOT", self.root)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_seed_and_safety_checks(self):
        added = PO.seed_christmas()
        self.assertEqual([a["title"] for a in added], ["Christmas: Velvet Christmas Tree Bow"])
        self.assertEqual(added[0]["caption"], "The finishing touch 🎀 From £18.99\n#christmasdecor #collective")
        self.assertEqual(PO.seed_christmas(), [], "already listed")
        outside = os.path.join(self.tmp.name, "evil.mp4")
        open(outside, "wb").close()
        with self.assertRaises(ValueError):
            PO.add(outside, "hi", "x")
        with self.assertRaises(ValueError):
            PO.add(self.video, "Hurry, 50% off today!", "x")

    def test_successful_post_is_verified_on_the_account(self):
        item = PO.seed_christmas()[0]
        page = FakePage()
        result = PO.post(item["id"], browser=FakeBrowser(page), sleep=lambda s: None)
        self.assertEqual(result["status"], "posted")
        self.assertEqual(page.files, [os.path.realpath(self.video)])
        self.assertIn("Cancel", page.clicked, "the optional auto-checks pop-up is declined")
        self.assertEqual(page.clicked.count("POST"), 1)
        self.assertEqual(page.mentions, ["#christmasdecor", "#collective"], "each tag picked from the dropdown")
        self.assertEqual(page.editor_text, "The finishing touch 🎀 From £18.99\n#christmasdecor #collective ")

    def test_a_tag_tiktok_does_not_offer_is_left_out_and_noted(self):
        item = PO.seed_christmas()[0]
        page = FakePage(known_tags=("#collective",))
        result = PO.post(item["id"], browser=FakeBrowser(page), sleep=lambda s: None)
        self.assertEqual(result["status"], "posted")
        self.assertEqual(page.mentions, ["#collective"])
        self.assertNotIn("#christmasdecor", page.editor_text)
        self.assertIn("#christmasdecor", result["note"])

    def test_split_caption(self):
        self.assertEqual(PO.split_caption("Cosy 🎄 From £5\n\n#a #b\n#B #c"), ("Cosy 🎄 From £5", ["#a", "#b", "#c"]))
        self.assertEqual(PO.split_caption("No tags here"), ("No tags here", []))

    def test_a_mangled_caption_is_never_posted(self):
        item = PO.seed_christmas()[0]
        page = FakePage(mangle=True)
        result = PO.post(item["id"], browser=FakeBrowser(page), sleep=lambda s: None)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("POST", page.clicked)
        self.assertIn("DISCARD", page.clicked)
        self.assertIn("caption", result["error"])

    def test_a_human_check_stops_it(self):
        item = PO.seed_christmas()[0]
        page = FakePage()
        result = PO.post(item["id"], browser=FakeBrowser(page, blocked=True), sleep=lambda s: None)
        self.assertEqual(result["status"], "failed")
        self.assertIn("human check", result["error"])
        self.assertNotIn("POST", page.clicked)

    def test_cannot_post_twice_and_remove_keeps_the_file(self):
        item = PO.seed_christmas()[0]
        PO.post(item["id"], browser=FakeBrowser(FakePage()), sleep=lambda s: None)
        with self.assertRaises(ValueError):
            PO.post(item["id"], browser=FakeBrowser(FakePage()), sleep=lambda s: None)
        with self.assertRaises(ValueError):
            PO.remove(item["id"])
        self.assertTrue(os.path.exists(self.video))


class PostingServerTests(unittest.TestCase):
    def test_only_the_window_can_post(self):
        from jarvis_v2 import server
        state = mock.Mock(posting_now=None, adv_refreshing=False)
        with self.assertRaises(PermissionError):
            server.api_post(state, "/api/posts/1/post", {}, from_ui=False)
        with self.assertRaises(PermissionError):
            server.api_post(state, "/api/posts/1/remove", {}, from_ui=False)


if __name__ == "__main__":
    unittest.main()


class SpeakingTests(unittest.TestCase):
    """Jarvis says what he's doing, and restarts a voice engine that's running old code."""

    def test_posting_reports_each_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "generated_videos")
            os.makedirs(os.path.join(root, "christmas", "TIKTOK"))
            open(os.path.join(root, "christmas", "TIKTOK", "1_bow.mp4"), "wb").close()
            with open(os.path.join(root, "christmas", "CHRISTMAS_TIKTOK_CAPTIONS.md"), "w", encoding="utf-8") as f:
                f.write("## 1. Velvet Bow\n```\nThe finishing touch\n#christmastree #collective\n```\n")
            with mock.patch.object(PO, "QUEUE_FILE", os.path.join(tmp, "q.json")), mock.patch.object(PO, "VIDEO_ROOT", root):
                item = PO.seed_christmas()[0]
                said = []
                result = PO.post(item["id"], browser=FakeBrowser(FakePage(known_tags=("#collective",))),
                                 sleep=lambda s: None, progress=said.append)
        self.assertEqual(result["status"], "posted")
        self.assertEqual(said[0], "Uploading the Velvet Bow video to TikTok now, sir.")
        self.assertTrue(any("christmastree" in s and "left it out" in s for s in said))
        self.assertEqual(said[-1], "Caption checked word for word. Posting it now.")

    def test_say_now_speaks_at_once(self):
        from jarvis_v2 import server, tools
        state = mock.Mock()
        self.assertEqual(server.api_post(state, "/api/say", {"text": "On it, sir."}), {"ok": True})
        state.announcer.urgent.assert_called_once_with("On it, sir.")
        with self.assertRaises(ValueError):
            server.api_post(state, "/api/say", {"text": "  "})
        self.assertIn("say_now", tools.TOOLS)

    def test_stale_engine_is_shut_down_for_a_restart(self):
        from jarvis_v2 import desktop
        sent, running = [], [True]

        def send(text):
            sent.append(text)
            running[0] = False
        busy = iter([True, True, False])
        out = desktop.ensure_fresh_engine(started_at=lambda: 100, changed_at=lambda: 200, alive=lambda: running[0],
                                          busy=lambda: next(busy), send=send, sleep=lambda s: None)
        self.assertEqual((out, sent), ("restarted", ["shut down jarvis"]))

    def test_up_to_date_or_absent_engine_is_left_alone(self):
        from jarvis_v2 import desktop
        send = mock.Mock()
        self.assertEqual(desktop.ensure_fresh_engine(started_at=lambda: 300, changed_at=lambda: 200, alive=lambda: True,
                                                     busy=lambda: False, send=send, sleep=lambda s: None), "up to date")
        self.assertEqual(desktop.ensure_fresh_engine(started_at=lambda: None, changed_at=lambda: 200, alive=lambda: False,
                                                     busy=lambda: False, send=send, sleep=lambda s: None), "not running")
        send.assert_not_called()
