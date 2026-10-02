import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading.registry import Registry, RegistryError


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "l.db")
        self.reg = Registry(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def paper(self, n):
        sid, ver, _ = self.reg.create_strategy("h", "template:x", "m", "1h", {"n": n}, f"code{n}")
        for st in ("backtested", "candidate", "paper"):
            self.reg.set_status(sid, ver, st, "test")
        return sid, ver

    def review(self, sid, ver, eligible=True):
        return self.reg.record_promotion_event("review", sid, ver, eligible, {"items": []}, "test")

    def test_live_cannot_be_set_directly(self):
        sid, ver = self.paper(1)
        with self.assertRaises(RegistryError):
            self.reg.set_status(sid, ver, "live", "just because")

    def test_promotion_needs_an_eligible_review_for_that_exact_version(self):
        sid, ver = self.paper(1)
        other, over = self.paper(2)
        with self.assertRaises(RegistryError):
            self.reg.promote_to_live(sid, ver, 999, "h", "f", "no review")
        bad = self.review(sid, ver, eligible=False)
        with self.assertRaises(RegistryError):
            self.reg.promote_to_live(sid, ver, bad, "h", "f", "ineligible review")
        good_for_other = self.review(other, over)
        with self.assertRaises(RegistryError):
            self.reg.promote_to_live(sid, ver, good_for_other, "h", "f", "someone else's review")
        self.assertIsNone(self.reg.current_live())

    def test_a_newer_ineligible_review_blocks_an_older_eligible_one(self):
        sid, ver = self.paper(1)
        old = self.review(sid, ver, True)
        self.review(sid, ver, False)
        with self.assertRaises(RegistryError):
            self.reg.promote_to_live(sid, ver, old, "h", "f", "stale approval")

    def test_promotion_replaces_the_previous_live_strategy_and_records_history(self):
        a, av = self.paper(1)
        self.reg.promote_to_live(a, av, self.review(a, av), "hashA", "limits1", "first live strategy")
        self.assertEqual(self.reg.current_live(), (a, av))
        b, bv = self.paper(2)
        previous = self.reg.promote_to_live(b, bv, self.review(b, bv), "hashB", "limits1", "B beats A")
        self.assertEqual(previous, (a, av))
        self.assertEqual(self.reg.current_live(), (b, bv))
        self.assertEqual(self.reg.status(a, av), "retired")
        self.assertEqual(len(self.reg.list_versions(status="live")), 1)
        events = [(h["event"], h["strategy_id"]) for h in reversed(self.reg.live_history())]
        self.assertEqual(events, [("promoted", a), ("retired", a), ("promoted", b)])

    def test_rollback_restores_a_previously_live_version_and_only_that(self):
        a, av = self.paper(1)
        self.reg.promote_to_live(a, av, self.review(a, av), "hashA", "l", "first")
        b, bv = self.paper(2)
        self.reg.promote_to_live(b, bv, self.review(b, bv), "hashB", "l", "second")
        self.reg.rollback_to(a, av, "hashA", "l", "B misbehaved live")
        self.assertEqual(self.reg.current_live(), (a, av))
        self.assertEqual(self.reg.status(b, bv), "retired")
        self.assertEqual(self.reg.live_history()[0]["event"], "rolled_back")
        never, nv = self.paper(3)
        with self.assertRaises(RegistryError):
            self.reg.rollback_to(never, nv, "h", "l", "never was live")

    def test_cannot_promote_something_that_is_not_in_paper(self):
        sid, ver, _ = self.reg.create_strategy("h", "template:x", "m", "1h", {"n": 9}, "code9")
        rid = self.review(sid, ver)
        with self.assertRaises(RegistryError):
            self.reg.promote_to_live(sid, ver, rid, "h", "l", "straight from generated")

    def test_rejected_is_still_terminal_and_cannot_go_live(self):
        sid, ver = self.paper(1)
        self.reg.set_status(sid, ver, "rejected", "paper degraded")
        with self.assertRaises(RegistryError):
            self.reg.promote_to_live(sid, ver, self.review(sid, ver), "h", "l", "try")

    def test_retire_live_needs_a_reason_and_logs(self):
        a, av = self.paper(1)
        self.reg.promote_to_live(a, av, self.review(a, av), "h", "l", "go")
        with self.assertRaises(RegistryError):
            self.reg.retire_live(a, av, "")
        self.reg.retire_live(a, av, "emergency demotion")
        self.assertIsNone(self.reg.current_live())
        self.assertEqual(self.reg.live_history()[0]["event"], "retired")

    def test_promotion_and_live_history_are_append_only(self):
        a, av = self.paper(1)
        self.reg.promote_to_live(a, av, self.review(a, av), "h", "l", "go")
        conn = sqlite3.connect(self.path)
        for sql in ("UPDATE promotion_log SET eligible=0", "DELETE FROM promotion_log", "UPDATE live_history SET reason='x'", "DELETE FROM live_history"):
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(sql)
        conn.close()

    def test_too_old_review_is_refused(self):
        a, av = self.paper(1)
        rid = self.review(a, av)
        with self.assertRaises(RegistryError):
            self.reg.promote_to_live(a, av, rid, "h", "l", "late", max_review_age_hours=-1)


if __name__ == "__main__":
    unittest.main()
