import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import criteria as C
from jarvis_trading import promotion as PR
from jarvis_trading import registry as R
from jarvis_trading import strategies as S

GOOD_PAPER = {"days": 90, "n_trades": 120, "max_drawdown": 0.06, "sharpe": 1.4}


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = R.Registry(os.path.join(self.tmp.name, "p.db"))
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def challenger(self, paper=None, verdict="CONTINUE", research_ok=True, template="sma_cross"):
        self.n += 1
        name, source, params = S.generate(template, {"fast": 5 + self.n})
        sid, ver, _ = self.reg.create_strategy("h", f"template:{template}", "m", params["timeframe"], params, source)
        for st in ("backtested", "candidate", "paper"):
            self.reg.set_status(sid, ver, st, "t")
        self.reg.record_experiment(sid, ver, "verdict", {"verdict": "CANDIDATE", "criteria": C.ResearchCriteria().fingerprint() if research_ok else "oldfp"}, passed=research_ok)
        self.reg.record_experiment(sid, ver, "paper_check", {"metrics": paper or GOOD_PAPER, "verdict": verdict}, passed=verdict != "REJECTED")
        return sid, ver

    def test_first_live_strategy_needs_only_the_absolute_requirements(self):
        sid, ver = self.challenger()
        r = PR.review(self.reg, sid, ver)
        self.assertTrue(r["eligible"], [i for i in r["items"] if not i["ok"]])
        self.assertIn("become the first live strategy", self.reg.recent_decisions(1)[0]["detail"])
        self.assertEqual(r["phrase"], f"PROMOTE {sid} v{ver} REVIEW {r['review_id']}")
        self.assertIsNone(self.reg.current_live())  # a flag is not a promotion

    def test_each_absolute_requirement_blocks_independently(self):
        for kw, key in (({"paper": dict(GOOD_PAPER, days=30)}, "paper_duration"),
                        ({"paper": dict(GOOD_PAPER, n_trades=10)}, "paper_trade_count"),
                        ({"paper": dict(GOOD_PAPER, max_drawdown=0.30)}, "paper_drawdown"),
                        ({"verdict": "INCOMPLETE"}, "paper_matches_backtest"),
                        ({"research_ok": False}, "passed_research_gates")):
            sid, ver = self.challenger(**kw)
            r = PR.review(self.reg, sid, ver, record=False)
            self.assertFalse(r["eligible"], key)
            self.assertIn(key, [i["key"] for i in r["items"] if not i["ok"]])

    def test_a_past_paper_rejection_blocks_promotion_forever(self):
        sid, ver = self.challenger()
        self.reg.record_experiment(sid, ver, "paper_check", {"metrics": GOOD_PAPER, "verdict": "REJECTED"}, passed=False)
        self.reg.record_experiment(sid, ver, "paper_check", {"metrics": GOOD_PAPER, "verdict": "CONTINUE"}, passed=True)
        r = PR.review(self.reg, sid, ver, record=False)
        self.assertIn("no_paper_breach", [i["key"] for i in r["items"] if not i["ok"]])

    def test_must_be_in_paper(self):
        sid, ver, _ = self.reg.create_strategy("h", "template:sma_cross", "m", "1h", S.generate("sma_cross")[2], S.generate("sma_cross")[1])
        r = PR.review(self.reg, sid, ver, record=False)
        self.assertIn("status_is_paper", [i["key"] for i in r["items"] if not i["ok"]])

    def test_code_that_does_not_regenerate_is_not_promotable(self):
        sid, ver, _ = self.reg.create_strategy("h", "template:sma_cross", "m", "1h", S.generate("sma_cross")[2], "# hand edited code")
        for st in ("backtested", "candidate", "paper"):
            self.reg.set_status(sid, ver, st, "t")
        r = PR.review(self.reg, sid, ver, record=False)
        self.assertIn("reproducible_code", [i["key"] for i in r["items"] if not i["ok"]])

    def live_strategy(self, sharpe=1.0, dd=0.08, days=90):
        sid, ver = self.challenger(paper=dict(GOOD_PAPER, sharpe=sharpe, max_drawdown=dd))
        rid = PR.review(self.reg, sid, ver)["review_id"]
        self.reg.promote_to_live(sid, ver, rid, "h", "l", "first live")
        # the live strategy's own record (its paper twin / live monitoring) as it stands now
        self.reg.record_experiment(sid, ver, "paper_check", {"metrics": dict(GOOD_PAPER, sharpe=sharpe, max_drawdown=dd, days=days), "verdict": "CONTINUE"}, passed=True)
        return sid, ver

    def test_replacing_live_requires_a_clear_margin_not_a_marginal_win(self):
        self.live_strategy(sharpe=1.0, dd=0.08)
        marginal = self.challenger(paper=dict(GOOD_PAPER, sharpe=1.1, max_drawdown=0.06))
        r = PR.review(self.reg, *marginal, record=False)
        self.assertFalse(r["eligible"])
        self.assertIn("beats_live_sharpe", [i["key"] for i in r["items"] if not i["ok"]])
        clear = self.challenger(paper=dict(GOOD_PAPER, sharpe=1.6, max_drawdown=0.06))
        r = PR.review(self.reg, *clear, record=False)
        self.assertTrue(r["eligible"], [i for i in r["items"] if not i["ok"]])
        self.assertAlmostEqual(r["vs_live"]["sharpe_margin"], 0.6)

    def test_a_higher_sharpe_with_a_much_worse_drawdown_is_not_better(self):
        self.live_strategy(sharpe=1.0, dd=0.04)
        risky = self.challenger(paper=dict(GOOD_PAPER, sharpe=2.0, max_drawdown=0.14))
        r = PR.review(self.reg, *risky, record=False)
        self.assertIn("drawdown_not_worse", [i["key"] for i in r["items"] if not i["ok"]])

    def test_overlap_window_is_required(self):
        self.live_strategy(days=20)
        c = self.challenger(paper=dict(GOOD_PAPER, sharpe=3.0))
        r = PR.review(self.reg, *c, record=False)
        self.assertIn("comparison_window", [i["key"] for i in r["items"] if not i["ok"]])

    def test_review_flag_names_the_strategy_it_would_replace(self):
        live = self.live_strategy()
        c = self.challenger(paper=dict(GOOD_PAPER, sharpe=1.8, max_drawdown=0.05))
        r = PR.review(self.reg, *c)
        self.assertEqual(r["replaces"], f"{live[0]} v{live[1]}")
        self.assertIn("replace", self.reg.recent_decisions(1)[0]["detail"])
        self.assertEqual(self.reg.current_live(), live)  # still the old one

    def test_review_if_changed_does_not_flood_the_log(self):
        sid, ver = self.challenger()
        PR.review_if_changed(self.reg, sid, ver)
        PR.review_if_changed(self.reg, sid, ver)
        PR.review_if_changed(self.reg, sid, ver)
        self.assertEqual(len([e for e in self.reg.promotion_events("review", sid) if e["version"] == ver]), 1)

    def test_criteria_are_frozen_and_fingerprinted(self):
        with self.assertRaises(Exception):
            PR.PromotionCriteria().min_paper_days = 1
        self.assertNotEqual(PR.PromotionCriteria().fingerprint(), PR.PromotionCriteria(min_paper_days=5).fingerprint())


if __name__ == "__main__":
    unittest.main()
