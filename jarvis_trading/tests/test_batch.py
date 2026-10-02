import concurrent.futures
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import batch as B
from jarvis_trading import registry as R
from jarvis_trading import strategies as S


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = R.Registry(os.path.join(self.tmp.name, "b.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_is_reproducible_interleaved_and_covers_every_family(self):
        a = B.plan_batch(self.reg, per_family=3, seed=7)
        b = B.plan_batch(self.reg, per_family=3, seed=7)
        self.assertEqual(a, b)
        families = [f for f, _ in a]
        self.assertEqual(set(families), {t for t in S.research_templates() if t not in S.TEMPLATES} )
        self.assertEqual(len(set(families[:len(set(families))])), len(set(families)))  # first round touches every family

    def test_plan_excludes_what_is_already_registered(self):
        first = B.plan_batch(self.reg, families=["trend_momentum"], per_family=5, seed=1)
        from jarvis_trading import universe as U
        for family, params in first[:3]:
            name, source, p = S.generate(family, params)
            self.reg.create_strategy("h", f"template:{family}", "/".join(U.UNIVERSE), p["timeframe"], p, source)
        second = B.plan_batch(self.reg, families=["trend_momentum"], per_family=5, seed=1)
        self.assertTrue(all(params not in [x[1] for x in first[:3]] for _, params in second))

    def test_plan_never_contains_controls(self):
        for family, _ in B.plan_batch(self.reg, per_family=2):
            self.assertNotIn(family, S.CONTROL_TEMPLATES)


class RunBatchTests(unittest.TestCase):
    def setUp(self):
        self.old = (B.STOP_FLAG, B.STATUS_FILE)
        self.tmp = tempfile.TemporaryDirectory()
        B.STOP_FLAG = os.path.join(self.tmp.name, "STOP")
        B.STATUS_FILE = os.path.join(self.tmp.name, "status.json")

    def tearDown(self):
        B.STOP_FLAG, B.STATUS_FILE = self.old
        self.tmp.cleanup()

    def plan(self, n):
        return [("sma_cross", {"i": i}) for i in range(n)]

    def threads(self):
        return lambda: concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def test_counts_results_and_survives_job_errors(self):
        def job(args):
            fam, params, workers = args
            if params["i"] == 1:
                return {"family": fam, "status": "error", "verdict": "boom"}
            return {"family": fam, "strategy_id": f"S-{params['i']}", "status": "candidate" if params["i"] == 0 else "rejected", "verdict": "x", "codes": []}
        state = B.run_batch(self.plan(4), 1, log=lambda m: None, pool_factory=self.threads(), job=job)
        self.assertEqual((state["done"], state["candidates"], state["rejected"], state["errors"]), (4, 1, 2, 1))
        self.assertFalse(state["running"])

    def test_stop_flag_cancels_pending_jobs_without_crashing(self):
        ran = []

        def job(args):
            ran.append(args[1]["i"])
            open(B.STOP_FLAG, "w").close()   # first job asks for a stop
            return {"family": "sma_cross", "strategy_id": "S", "status": "rejected", "verdict": "x", "codes": []}
        state = B.run_batch(self.plan(6), 1, log=lambda m: None, pool_factory=self.threads(), job=job)
        self.assertFalse(state["running"])
        self.assertLess(len(ran), 6)

    def test_empty_plan(self):
        self.assertFalse(B.run_batch([], 1, log=lambda m: None)["running"])


if __name__ == "__main__":
    unittest.main()
