"""Store, controls, approvals, evidence, scoring and verdict maths."""

import sqlite3
import unittest

from jarvis_marketing import controls as C, evidence as E, scoring, stats
from jarvis_marketing.tests.helpers import Clock, business, temp_store

DANNY = C.HumanPrincipal("Danny", "test")


class ProfitMathsTests(unittest.TestCase):
    def test_contribution_matches_hand_calculation(self):
        e = scoring.unit_economics(price=19.99, product_cost=4.50, shipping=3.00, payment_fee_pct=0.02,
                                   payment_fee_fixed=0.25, return_rate=0.05, other_variable_cost=0.30)
        # fees 19.99*0.02+0.25 = 0.6498; returns 0.9995; 19.99-4.5-3-0.6498-0.9995-0.3 = 10.5407
        self.assertEqual(e["payment_fees"], 0.65)
        self.assertEqual(e["expected_returns"], 1.0)
        self.assertEqual(e["contribution"], 10.54)
        self.assertEqual(e["break_even_cac"], 10.54)
        self.assertEqual(e["break_even_roas"], round(19.99 / 10.5407, 2))
        self.assertAlmostEqual(e["margin_pct"], round(10.5407 / 19.99, 4))

    def test_loss_making_product_has_no_break_even_roas(self):
        e = scoring.unit_economics(price=5.0, product_cost=4.0, shipping=2.0)
        self.assertLess(e["contribution"], 0)
        self.assertIsNone(e["break_even_roas"])
        self.assertEqual(e["break_even_cac"], 0.0)

    def test_unknown_costs_are_refused_not_assumed(self):
        with self.assertRaises(ValueError):
            scoring.unit_economics(price=10, product_cost=None, shipping=1)

    def test_proposed_price_reaches_the_target(self):
        p = scoring.propose_price(4.5, 3.0, 5.0)
        self.assertTrue(str(p).endswith((".49", ".99")))
        self.assertGreaterEqual(scoring.unit_economics(p, 4.5, 3.0)["contribution"], 5.0)

    def _signals(self, **over):
        s = {"supplier_listed": 2000, "competitor_prices": [17.99, 19.99, 22.0], "delivery_days_max": 9, "images": 6,
             "stock": 100, "fact_count": 6}
        s.update(over)
        return s

    def test_good_product_passes_and_explains_its_score(self):
        b = business()
        econ = scoring.unit_economics(16.99, 3.5, 2.5)
        r = scoring.score(econ, self._signals(), b["thresholds"], b["weights"])
        self.assertFalse(r["rejected"], r["rejections"])
        self.assertEqual(r["confidence"], 1.0)
        self.assertIn("margin", r["explanation"])

    def test_each_threshold_rejects_with_a_reason(self):
        b = business()
        econ = scoring.unit_economics(16.99, 3.5, 2.5)
        cases = {"delivery": self._signals(delivery_days_max=20), "images": self._signals(images=2),
                 "stock": self._signals(stock=0), "facts": self._signals(fact_count=1),
                 "competitor": self._signals(competitor_prices=[9.99, 10.49]), "denied": self._signals(denied="knife")}
        for name, sig in cases.items():
            r = scoring.score(econ, sig, b["thresholds"], b["weights"])
            self.assertTrue(r["rejected"], name)
            self.assertTrue(r["rejections"], name)
        thin = scoring.score(scoring.unit_economics(9.99, 6.0, 2.5), self._signals(), b["thresholds"], b["weights"])
        self.assertTrue(any("Contribution" in x for x in thin["rejections"]))

    def test_missing_data_scores_zero_and_lowers_confidence(self):
        b = business()
        econ = scoring.unit_economics(16.99, 3.5, 2.5)
        r = scoring.score(econ, self._signals(supplier_listed=None, competitor_prices=[]), b["thresholds"], b["weights"])
        self.assertIsNone(r["components"]["demand"])
        self.assertLess(r["confidence"], 1.0)
        self.assertIn("no data for", r["explanation"])

    def test_ranking_is_deterministic(self):
        items = [{"id": 3, "score": 50, "confidence": 1}, {"id": 1, "score": 70, "confidence": 0.5},
                 {"id": 2, "score": 70, "confidence": 0.9}]
        self.assertEqual([i["id"] for i in scoring.rank(items)], [2, 1, 3])


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.store, _ = temp_store()
        self.fid = E.record_fetch(self.store, 1, "cj", "https://cj/p/1", {"listed": 1234, "price": 3.5}, simulated=False)

    def test_sourced_fact_is_kept_with_provenance(self):
        eid = E.fact(self.store, 1, "candidate", "cj:1", "supplier_listed", "Listed by 1234 stores", self.fid, "listed", 1234)
        row = self.store.get("evidence", eid)
        self.assertEqual(row["source_url"], "https://cj/p/1")
        self.assertEqual(row["label"], "fact")
        self.assertFalse(row["simulated"])

    def test_fact_without_a_source_is_refused(self):
        with self.assertRaises(E.FabricatedEvidence):
            E.add(self.store, 1, "candidate", "cj:1", E.FACT, "sales", "Sells 500 a day", value=500)

    def test_fact_with_a_made_up_number_is_refused(self):
        with self.assertRaises(E.FabricatedEvidence):
            E.add(self.store, 1, "candidate", "cj:1", E.FACT, "supplier_listed", "Listed by 9999 stores", value=9999,
                  fetch_id=self.fid, quote='"listed": 1234')

    def test_quote_not_in_source_is_refused(self):
        with self.assertRaises(E.FabricatedEvidence):
            E.add(self.store, 1, "candidate", "cj:1", E.FACT, "rating", "Rated 4.9", value=4.9, fetch_id=self.fid,
                  quote='"rating": 4.9')

    def test_mismatched_url_is_refused(self):
        with self.assertRaises(E.FabricatedEvidence):
            E.add(self.store, 1, "c", "k", E.FACT, "x", "x", value=1234, fetch_id=self.fid, quote='"listed": 1234',
                  source_url="https://elsewhere")

    def test_simulated_fetch_stays_simulated(self):
        fid = E.record_fetch(self.store, 1, "mock_cj", "mock://cj/1", {"listed": 5}, simulated=True)
        eid = E.fact(self.store, 1, "c", "k", "supplier_listed", "5 stores", fid, "listed", 5)
        self.assertTrue(self.store.get("evidence", eid)["simulated"])
        with self.assertRaises(E.FabricatedEvidence):
            E.add(self.store, 1, "c", "k", E.FACT, "x", "x", value=5, fetch_id=fid, quote='"listed": 5', simulated=False)

    def test_estimate_needs_a_basis_and_never_counts_as_fact(self):
        with self.assertRaises(ValueError):
            E.add(self.store, 1, "c", "k", E.ESTIMATE, "cost_gbp", "Cost about 2.63", value=2.63)
        E.add(self.store, 1, "c", "k", E.ESTIMATE, "cost_gbp", "Cost about 2.63", value=2.63, basis="3.5 USD x 0.75")
        self.assertNotIn("cost_gbp", E.facts(self.store, "c", "k"))
        self.assertEqual(E.estimates(self.store, "c", "k")["cost_gbp"], 2.63)

    def test_verified_only_from_our_own_systems(self):
        with self.assertRaises(E.FabricatedEvidence):
            E.add(self.store, 1, "c", "k", E.VERIFIED, "listed", "x", value=1234, fetch_id=self.fid, quote='"listed": 1234')


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store, _ = temp_store(self.clock)
        self.c = C.Controls(self.store)
        self.bid = self.store.business_id(business())

    def test_starts_in_research_with_zero_caps(self):
        s = self.c.summary(self.bid)
        self.assertEqual(s["mode"], C.RESEARCH)
        self.assertEqual(s["caps"], {"total": 0.0, "per_campaign": 0.0})
        self.assertTrue(all(s["hard_off"].values()))

    def test_only_a_human_changes_mode_one_step_at_a_time_never_scale(self):
        with self.assertRaises(C.PermissionDenied):
            self.c.set_mode("director", self.bid, C.REVIEW)
        with self.assertRaises(C.PermissionDenied):
            self.c.set_mode(DANNY, self.bid, C.TEST)          # skipping REVIEW
        self.c.set_mode(DANNY, self.bid, C.REVIEW)
        self.c.set_mode(DANNY, self.bid, C.TEST)
        with self.assertRaises(C.PermissionDenied):
            self.c.set_mode(DANNY, self.bid, C.SCALE)
        self.c.set_mode(DANNY, self.bid, C.RESEARCH)            # stepping down is always fine

    def test_paid_purchase_price_are_refused_in_every_mode(self):
        for mode in (C.RESEARCH, C.REVIEW, C.TEST):
            self.c._set(f"mode:{self.bid}", mode, "test")
            for action in ("paid_spend", "launch_paid", "scale_budget", "purchase", "price_change"):
                with self.assertRaises(C.PermissionDenied, msg=f"{action} in {mode}"):
                    self.c.require(self.bid, action, "agent", approval_id=1, content={"x": 1}, amount=1.0)
                self.assertFalse(self.c.allowed(self.bid, action))

    def test_caps_can_never_be_raised_while_paid_is_off(self):
        with self.assertRaises(C.PermissionDenied):
            self.c.set_caps(DANNY, self.bid, 50, 10)
        with self.assertRaises(C.PermissionDenied):
            self.c.set_caps("agent", self.bid, 0, 0)
        self.c.set_caps(DANNY, self.bid, 0, 0)

    def test_spend_is_refused_against_zero_caps(self):
        self.c._set(f"mode:{self.bid}", C.TEST, "test")
        with self.assertRaises(C.PermissionDenied):
            self.c.require(self.bid, "read_analytics", "agent", amount=0.01)

    def test_organic_post_needs_an_approval_of_that_exact_content_once(self):
        a = C.Approvals(self.store)
        content = {"caption": "hello", "video": "v.mp4"}
        self.c._set(f"mode:{self.bid}", C.TEST, "test")
        with self.assertRaises(C.PermissionDenied):
            self.c.require(self.bid, "publish_organic", "agent")
        aid = a.request(self.bid, "publish_organic", "post", 1, content, "Post 1", "director")
        self.assertEqual(a.request(self.bid, "publish_organic", "post", 1, content, "Post 1", "director"), aid)  # no duplicate request
        with self.assertRaises(C.PermissionDenied):                 # not approved yet
            self.c.require(self.bid, "publish_organic", "agent", approval_id=aid, content=content)
        with self.assertRaises(C.PermissionDenied):
            a.decide("agent", aid, "approve")
        a.decide(DANNY, aid, "approve")
        with self.assertRaises(C.PermissionDenied):                 # content changed after approval
            self.c.require(self.bid, "publish_organic", "agent", approval_id=aid, content=dict(content, caption="HELLO"))
        self.c.require(self.bid, "publish_organic", "agent", approval_id=aid, content=content)
        with self.assertRaises(C.PermissionDenied):                 # single use
            self.c.require(self.bid, "publish_organic", "agent", approval_id=aid, content=content)

    def test_approval_expires(self):
        a = C.Approvals(self.store)
        self.c._set(f"mode:{self.bid}", C.TEST, "test")
        aid = a.request(self.bid, "publish_organic", "post", 1, {"c": 1}, "x", "director")
        a.decide(DANNY, aid, "approve")
        self.clock.advance(C.APPROVAL_SECONDS + 1)
        with self.assertRaises(C.PermissionDenied):
            self.c.require(self.bid, "publish_organic", "agent", approval_id=aid, content={"c": 1})

    def test_organic_post_refused_outside_test_mode(self):
        a = C.Approvals(self.store)
        aid = a.request(self.bid, "publish_organic", "post", 1, {"c": 1}, "x", "director")
        a.decide(DANNY, aid, "approve")
        with self.assertRaises(C.PermissionDenied):
            self.c.require(self.bid, "publish_organic", "agent", approval_id=aid, content={"c": 1})

    def test_emergency_stop_blocks_everything_and_only_danny_resumes(self):
        self.c.emergency_stop("director", "test")
        with self.assertRaises(C.PermissionDenied):
            self.c.require(self.bid, "research", "agent")
        with self.assertRaises(C.PermissionDenied):
            self.c.resume("agent")
        self.c.resume(DANNY)
        self.c.require(self.bid, "research", "agent")

    def test_audit_log_is_append_only_and_redacted(self):
        self.store.audit("x", "y", "ok", token="abc", note="Bearer secretvalue123")
        row = self.store.find("audit")[-1]
        self.assertEqual(row["details"]["token"], "[redacted]")
        self.assertNotIn("secretvalue123", str(row["details"]))
        with self.assertRaises(sqlite3.DatabaseError):
            with self.store.conn() as c:
                c.execute("DELETE FROM audit")
        with self.assertRaises(sqlite3.DatabaseError):
            with self.store.conn() as c:
                c.execute("UPDATE audit SET outcome='ok'")


class VerdictTests(unittest.TestCase):
    CRIT = {"min_sessions_per_variant": 150, "stop_sessions_without_order": 400, "significance": 0.05,
            "data_delay_hours": 48, "attribution_window_days": 7}
    NOW = 1_800_000_000.0
    ENDED = NOW - 10 * 86400

    def v(self, i, sessions, orders, contribution=None, **kw):
        return dict(id=i, sessions=sessions, orders=orders, contribution=contribution if contribution is not None else (orders or 0) * 8.0, **kw)

    def test_clear_profitable_difference_is_a_winner(self):
        r = stats.evaluate([self.v(1, 1000, 40), self.v(2, 1000, 10)], self.CRIT, self.NOW, self.ENDED)
        self.assertEqual(r["verdict"], "winner")
        self.assertEqual(r["winner"], 1)

    def test_small_difference_is_inconclusive_never_a_winner(self):
        r = stats.evaluate([self.v(1, 300, 6), self.v(2, 300, 4)], self.CRIT, self.NOW, self.ENDED)
        self.assertEqual(r["verdict"], "inconclusive")
        self.assertIsNone(r["winner"])

    def test_thin_data_after_the_test_is_inconclusive_and_open_window_is_insufficient(self):
        r = stats.evaluate([self.v(1, 100, 5), self.v(2, 1000, 1)], self.CRIT, self.NOW, self.ENDED)
        self.assertEqual(r["verdict"], "inconclusive")
        self.assertIsNone(r["winner"])
        self.assertEqual(stats.evaluate([self.v(1, 1000, 40), self.v(2, 1000, 10)], self.CRIT, self.NOW, self.NOW - 3600)["verdict"],
                         "insufficient_data")

    def test_missing_metrics_are_never_zero(self):
        r = stats.evaluate([self.v(1, None, None), self.v(2, 1000, 10)], self.CRIT, self.NOW, self.ENDED)
        self.assertEqual(r["verdict"], "insufficient_data")

    def test_stale_data_gives_no_verdict(self):
        r = stats.evaluate([self.v(1, 1000, 40, stale=True), self.v(2, 1000, 10)], self.CRIT, self.NOW, self.ENDED)
        self.assertEqual(r["verdict"], "insufficient_data")

    def test_views_and_likes_never_pick_a_winner(self):
        r = stats.evaluate([self.v(1, 1000, 10, views=900000, likes=50000), self.v(2, 1000, 11, views=10, likes=0)],
                           self.CRIT, self.NOW, self.ENDED)
        self.assertNotEqual(r["winner"], 1)

    def test_no_orders_recommends_stopping(self):
        r = stats.evaluate([self.v(1, 500, 0), self.v(2, 500, 0)], self.CRIT, self.NOW, self.ENDED)
        self.assertEqual(r["verdict"], "no_winner")
        self.assertTrue(all(p["stop"] for p in r["variants"]))

    def test_wilson_bounds(self):
        lo, hi = stats.wilson(10, 100)
        self.assertLess(lo, 0.1)
        self.assertGreater(hi, 0.1)
        self.assertEqual(stats.wilson(0, 0), (0.0, 1.0))


if __name__ == "__main__":
    unittest.main()
