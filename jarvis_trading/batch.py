"""
Batch and parallel research. Plans a reproducible batch of NEW parameter sets
across the strategy families (round-robin, so any partial batch covers every
family), then runs each through the full Lab pipeline in worker processes.

Safe to parallelise because: every Freqtrade run uses its own private
config/results directory; the registry serialises id allocation
(BEGIN IMMEDIATE) and is append-only; the held-out period is still used once
per strategy; and the multiple-testing count assumes every concurrent worker
is mid-trial. Workers run at below-normal priority so the PC stays usable.

Run under the Freqtrade venv's Python:
    trading_lab\ft_venv\Scripts\python.exe -m jarvis_trading.batch --per-family 30 --workers 6
"""

import argparse
import concurrent.futures
import json
import multiprocessing
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_trading import freqtrade_adapter as A  # noqa: E402
from jarvis_trading import lab as L  # noqa: E402
from jarvis_trading import registry as R  # noqa: E402
from jarvis_trading import runners as RUN  # noqa: E402
from jarvis_trading import strategies as S  # noqa: E402
from jarvis_trading import strategies_v2 as V2  # noqa: E402
from jarvis_trading import universe as U  # noqa: E402

STOP_FLAG = os.path.join(A.LAB_DIR, "STOP_RESEARCH")
STATUS_FILE = os.path.join(A.LAB_DIR, "batch_status.json")
DEFAULT_WORKERS = 6


def plan_batch(registry, families=None, per_family=20, seed=1, pairs=None):
    """List of (family, params) never registered before, interleaved across families."""
    pairs = pairs or U.UNIVERSE
    market = "/".join(pairs)
    known = {(t, m, p) for t, m, p in registry.known_parameter_sets()}
    per = {}
    for family in (families or [t for t in S.research_templates() if t in V2.FAMILIES or S.is_ml(t)]):
        exclude = {p for t, m, p in known if t == family and m == market}
        per[family] = S.sample(family, per_family, seed=seed, exclude=exclude)
    plan = []
    for i in range(per_family):
        for family, items in per.items():
            if i < len(items):
                plan.append((family, items[i]))
    return plan


def _daily_closes():
    import pandas as pd
    df = pd.read_feather(os.path.join(A.DATA_DIR, "BTC_USDT-1d.feather"))
    return [(row.date.timestamp(), float(row.close)) for row in df.itertuples()]


_worker_state = {}


def _research_one(job):
    family, params, workers = job
    if os.path.exists(STOP_FLAG):
        return {"family": family, "status": "skipped", "verdict": "stop flag present"}
    if "lab" not in _worker_state:
        _worker_state["lab"] = L.Lab(R.Registry(), RUN.RouterRunner(A), daily_closes=_daily_closes(), concurrency=workers)
    try:
        r = _worker_state["lab"].research(family, params)
        return {"family": family, "strategy_id": r["strategy_id"], "status": r["status"], "verdict": r["verdict"], "codes": r["codes"]}
    except Exception as error:  # a crash is recorded, never silently treated as a pass
        return {"family": family, "status": "error", "verdict": f"{type(error).__name__}: {error}"[:300]}


def _write_status(state):
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATUS_FILE)


def run_batch(plan, workers=DEFAULT_WORKERS, log=print, pool_factory=None, job=None):
    state = {"started": time.time(), "total": len(plan), "done": 0, "candidates": 0, "rejected": 0, "errors": 0, "running": True, "last": ""}
    _write_status(state)
    if not plan:
        state["running"] = False
        _write_status(state)
        return state
    job = job or _research_one
    if pool_factory is None:
        ctx = multiprocessing.get_context("spawn")
        pool_factory = lambda: concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx)
    with pool_factory() as pool:
        futures = [pool.submit(job, (family, params, workers)) for family, params in plan]
        for fut in concurrent.futures.as_completed(futures):
            if fut.cancelled():
                state["skipped"] = state.get("skipped", 0) + 1
                continue  # cancelled after the stop flag appeared; nothing ran
            r = fut.result()
            state["done"] += 1
            state["candidates"] += r["status"] == "candidate"
            state["rejected"] += r["status"] == "rejected"
            state["errors"] += r["status"] == "error"
            state["last"] = f"{r.get('strategy_id', '')} {r['family']} {r['status']}"
            log(f"[{state['done']}/{state['total']}] {r['family']:20s} {r.get('strategy_id', ''):7s} {r['status'].upper():10s} {r['verdict'][:150]}")
            _write_status(state)
            if os.path.exists(STOP_FLAG):
                for f in futures:
                    f.cancel()
    state["running"] = False
    state["finished"] = time.time()
    _write_status(state)
    return state


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--families", nargs="+", default=None)
    ap.add_argument("--per-family", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = ap.parse_args(argv)
    reg = R.Registry()
    plan = plan_batch(reg, args.families, args.per_family, args.seed)
    print(f"Planned {len(plan)} new strategies across {len(set(f for f, _ in plan))} families; {args.workers} workers", flush=True)
    state = run_batch(plan, args.workers)
    print(f"Finished: {state['done']} done, {state['candidates']} candidates, {state['rejected']} rejected, {state['errors']} errors", flush=True)
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
