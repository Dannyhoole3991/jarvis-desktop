"""Redbubble prep: designs get a clean transparent background and a listing, and are counted as waiting."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import jarvis_redbubble_prep as rb  # noqa: E402


class PrepTest(unittest.TestCase):
    def test_black_background_removed_but_inner_black_kept(self):
        from PIL import Image, ImageDraw
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "d.png")
        im = Image.new("RGB", (400, 400), (0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse((100, 100, 300, 300), fill=(220, 120, 40))
        d.rectangle((190, 190, 210, 210), fill=(0, 0, 0))          # black detail inside the art
        im.save(src)
        out = rb.cut_out(src, os.path.join(tmp, "out.png"))
        o = Image.open(out)
        self.assertEqual(o.mode, "RGBA")
        self.assertEqual(o.getpixel((0, 0))[3], 0)                  # corner is transparent
        cx, cy = o.width // 2, o.height // 2
        self.assertEqual(o.getpixel((cx, cy))[3], 255)               # inner black detail stays solid

    def test_prepare_lists_once_and_counts_waiting(self):
        from PIL import Image
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "x.png")
        Image.new("RGB", (64, 64), (200, 50, 50)).save(src)
        with mock.patch.object(rb, "READY_DIR", tmp), mock.patch.object(rb, "UPLOAD_DIR", os.path.join(tmp, "up")), \
             mock.patch.object(rb, "LISTINGS", os.path.join(tmp, "listings.json")):
            a = rb.prepare(src, "Retro Frog Tee Design (POD)", "a cute retro frog")
            rb.prepare(src, "Retro Frog Tee Design (POD)")
            self.assertEqual(len(rb.waiting()), 1)
            self.assertEqual(a["title"], "Retro Frog")
            self.assertTrue(os.path.exists(os.path.join(tmp, a["file"])))
            rb.mark_uploaded(src, "https://www.redbubble.com/shop/ap/1")
            self.assertEqual(rb.waiting(), [])


if __name__ == "__main__":
    unittest.main()
