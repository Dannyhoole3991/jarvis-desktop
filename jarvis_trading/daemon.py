"""
Continuous research + paper-trading daemon (research and paper only).

Loop:
  - plan a batch of NEW strategies across the families (reproducible seeds), run them in parallel;
  - in a background thread, keep every surviving candidate paper trading (supervisor);
  - stop when the trial budget is spent, the space is exhausted, or STOP_RESEARCH appears.

The trial budget exists on purpose: every strategy tried makes the multiple-testing
bar stricter for the next one, so unbounded search destroys its own power. Raise it
deliberately (--max-trials), do not make it infinite.

Run under the Freqtrade venv's Python:
    trading_lab\\ft_venv\\Scripts\\python.exe -m jarvis_trading.daemon
"""

import argparse
import multiprocessing
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis_trading import batch as B  # noqa: E402
from jarvis_trading import freqtrade_adapter as A  # noqa: E402
from jarvis_trading import paper as P  # noqa: E402
from jarvis_trading import registry as R  # noqa: E402
from jarvis_trading import supervisor as SUP  # noqa: E402

PID_FILE = os.path.join(A.LAB_DIR, "daemon.pid")
DEFAULT_MAX_TRIALS = 6000


def _supervise_forever(registry, stop, interval=60, max_paper=3):
    sup = SUP.Supervisor(registry, P.PaperManager(registry), max_concurrent=max_paper)
    while not stop.is_set():
        try:
            sup.tick()
        except Exception as error:
            registry.log_decision("paper_error", f"Supervisor tick failed: {error}")
        stop.wait(interval)


def run(per_family=15, workers=B.DEFAULT_WORKERS, max_trials=DEFAULT_MAX_TRIALS, max_paper=3, families=None, log=print):
    registry = R.Registry()
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    stop = threading.Event()
    threading.Thread(target=_supervise_forever, args=(registry, stop, 60, max_paper), daemon=True).start()
    seed = 1
    try:
        while not os.path.exists(B.STOP_FLAG):
            trials = registry.count_trials()
            if trials >= max_trials:
                log(f"Trial budget reached ({trials} >= {max_trials}); stopping search. Paper trading continues.")
                break
            plan = B.plan_batch(registry, families, per_family, seed)
            if not plan:
                seed += 1
                if seed > 50:
                    log("Search space exhausted for the configured families.")
                    break
                continue
            log(f"Seed {seed}: {len(plan)} new strategies, {trials} trials so far")
            B.run_batch(plan, workers, log=log)
            seed += 1
        # search is finished (or stopped): keep supervising paper trading until told to stop
        while not os.path.exists(B.STOP_FLAG):
            time.sleep(30)
    finally:
        stop.set()
        try:
            os.remove(PID_FILE)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-family", type=int, default=15)
    ap.add_argument("--workers", type=int, default=B.DEFAULT_WORKERS)
    ap.add_argument("--max-trials", type=int, default=DEFAULT_MAX_TRIALS)
    ap.add_argument("--max-paper", type=int, default=3)
    ap.add_argument("--families", nargs="+", default=None)
    args = ap.parse_args(argv)
    run(args.per_family, args.workers, args.max_trials, args.max_paper, args.families, log=lambda m, **k: print(m, flush=True))
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
