"""
TikTok Studio parsing, tested on text captured from the real pages (2026-10-14).

    py -3 -m unittest jarvis_v2.tests.test_advertising -v
"""

import unittest

from jarvis_v2 import advertising as A

OVERVIEW = """Last 7 days
Download data
Overview
Content
Viewers
Followers
Video views
17
-18
(-51.4%)
Profile views
0
-1
(-100.0%)
Likes
3
-2
(-40.0%)
Comments
--
0
(--)
Shares
0
0
(--)
Est. rewards
$0.00
"""

FOLLOWERS = """Last 7 days
Get more insights once you reach 100 followers
Total followers
1
All time
Net followers
0
"""

CONTENT = ("Posts 4\nDrafts 0\nViews\nLikes\nComments\nPrivacy\nContent (Created on)\nPrivacy\nViews\nLikes\nComments\nActions\n"
           "00:20\nLong desk days leave your neck stiff? Hook this roller behind your neck. #neckmassager #collective\n"
           "5 Oct, 11:50 pm\nPublic\n1\n1\n0\n"
           "00:19\nStill chopping garlic by hand? \U0001f9c4 Drop it in. #garlic\n5 Oct, 8:18 pm\nPublic\n13\n1\n0\n"
           "01:05\nA longer one\nwith a caption on two lines\n12 Sept 2026, 9:05 am\nPrivate\n1.2K\n40\n3\n")


class TikTokParsingTests(unittest.TestCase):
    def test_overview(self):
        o = A.parse_overview(OVERVIEW)
        self.assertEqual(o["period"], "Last 7 days")
        self.assertEqual(o["metrics"]["Video views"], {"value": 17, "change": -18, "change_pct": -51.4})
        self.assertEqual(o["metrics"]["Likes"]["value"], 3)
        self.assertIsNone(o["metrics"]["Comments"]["value"], "'--' means no data, not zero")

    def test_followers(self):
        self.assertEqual(A.parse_followers(FOLLOWERS), 1)
        self.assertIsNone(A.parse_followers("nothing here"))

    def test_videos_including_tiktoks_narrow_space_in_times(self):
        vids = A.parse_videos(CONTENT)
        self.assertEqual(len(vids), 3, "older posts show the year, and September is written 'Sept'")
        self.assertEqual((vids[0]["views"], vids[0]["likes"], vids[0]["comments"], vids[0]["privacy"]), (1, 1, 0, "Public"))
        self.assertEqual(vids[1]["views"], 13)
        self.assertEqual(vids[0]["posted"], "5 Oct, 11:50 pm")
        self.assertIn("neck stiff", vids[0]["caption"])
        self.assertEqual((vids[2]["views"], vids[2]["privacy"]), (1200.0, "Private"))
        self.assertEqual(vids[2]["caption"], "A longer one with a caption on two lines")

    def test_numbers(self):
        self.assertEqual(A._num("1.2K"), 1200.0)
        self.assertEqual(A._num("3M"), 3_000_000)
        self.assertIsNone(A._num("--"))
        self.assertEqual(A._num("1,024"), 1024)

    def test_describe_never_invents_numbers(self):
        self.assertIn("haven't checked", A.describe({"tiktok": None}))
        self.assertIn("logged out", A.describe({"tiktok": {"ok": False, "problem": "Jarvis's browser is logged out of TikTok"}}))


if __name__ == "__main__":
    unittest.main()
