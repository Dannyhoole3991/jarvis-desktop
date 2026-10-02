"""
Command line for the live subsystem. Run under the Freqtrade venv's Python:

    trading_lab\\ft_venv\\Scripts\\python.exe -m jarvis_trading.live.cli <command>

  status                      show live state (strategy, arming, trader, account)
  rehearse [--minutes N]      run the FULL live pipeline in dry-run (simulated money, no keys) for a wiring check
  start                       start the LIVE trader (refuses unless armed and every precondition holds)
  approve --review N          approve a promotion review (you type the confirmation phrase)
  arm                         arm real-money trading for the live strategy (you type the confirmation phrase)
  disarm                      disarm (always allowed)
  emergency-stop              stop entries, close everything, latch
  clear-emergency | clear-halt   clear a latch (you type the confirmation phrase)
  feasibility                 what the exchange's real minimums mean for a small account

Nothing here enables trading by itself: starting requires an approved promotion AND an explicit, typed arming.
"""

import argparse
import json
import os
import random
import socket
import sys
import tempfile
import time

from .. import registry as R
from . import approval, bundle, config, creds, paths
from . import exchange as X
from . import limits as L
from . import trader as T


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _exchange(public_only=False):
    c = None if public_only else creds.load_credentials()
    settings = config.load_settings()
    return X.CcxtExchange(settings.get("exchange", "binance"), c["key"] if c else None, c["secret"] if c else None)


def cmd_status(args):
    reg = R.Registry()
    from . import status as ST
    print(json.dumps(ST.build(reg), indent=2, default=str))


def cmd_approve(args):
    reg = R.Registry()
    review = next((e for e in reg.promotion_events("review", limit=2000) if e["id"] == args.review), None)
    if not review:
        print("No such review.")
        return 1
    from .. import promotion as PR
    phrase = PR.approval_phrase(args.review, review["strategy_id"], review["version"])
    print(f"Review #{args.review}: {review['strategy_id']} v{review['version']} eligible={bool(review['eligible'])}")
    for item in review["details"]["items"]:
        print(f"  [{'ok' if item['ok'] else 'NO'}] {item['detail']}")
    print(f"To approve, type exactly:\n  {phrase}")
    result = approval.approve_promotion(reg, args.review, input("> "))
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_arm(args):
    reg = R.Registry()
    live = reg.current_live()
    settings = config.load_settings()
    limits = config.build_limits(settings)
    if not live:
        print("No live strategy.")
        return 1
    print(f"Live strategy {live[0]} v{live[1]}, capital {limits.capital_limit:g} USDT, limits {limits.fingerprint()}")
    print(f"To enable REAL-MONEY trading, type exactly:\n  {approval.arming_phrase(live[0], live[1], limits.capital_limit)}")
    print(json.dumps(approval.arm(reg, input("> "), days=args.days), indent=2))
    return 0


def cmd_simple(fn):
    def run(args):
        fn(R.Registry())
        return 0
    return run


def cmd_emergency(args):
    paths.ensure_dirs()
    with open(paths.EMERGENCY_FILE, "w", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    print("Emergency stop requested; a running trader will close everything within seconds.")
    return 0


def cmd_clear(kind):
    def run(args):
        reg = R.Registry()
        phrase = "CLEAR EMERGENCY STOP" if kind == "emergency" else "CLEAR RISK HALT"
        print(f"Type exactly: {phrase}")
        (approval.clear_emergency if kind == "emergency" else approval.clear_halt)(reg, input("> "))
        print("Cleared.")
        return 0
    return run


def cmd_feasibility(args):
    from . import feasibility
    print(json.dumps(feasibility.report(float(args.capital), args.currency), indent=2, default=str))
    return 0


def _run(trader, interval):
    port, user, pw = _free_port(), "jv" + "%06x" % random.randrange(1 << 24), "%032x" % random.getrandbits(128)
    trader.build_engine(port, user, pw)
    problems = trader.preflight()
    if problems:
        print("Cannot start:")
        for p in problems:
            print("  -", p)
        return 1
    print("Starting engine, mode:", trader.mode)
    trader.engine_proc.start()
    time.sleep(20)
    try:
        trader.run_forever(interval)
    except KeyboardInterrupt:
        print("Interrupted; the trader is stopping. Open positions stay protected by exchange-side stops.")
    finally:
        trader.engine_proc.terminate()
    return 0


def cmd_start(args):
    reg = R.Registry()
    live = reg.current_live()
    if not live:
        print("No live strategy.")
        return 1
    settings = config.load_settings()
    limits = config.build_limits(settings)
    manifest, folder = bundle.load_manifest(*live)
    trader = T.LiveTrader(reg, limits, "live", _exchange(), folder, manifest, settings=settings)
    return _run(trader, args.interval)


def cmd_rehearse(args):
    """Full pipeline in dry-run. Uses the live strategy if there is one, else a throwaway scratch strategy (clearly a wiring test)."""
    from .. import strategies as S
    paths.ensure_dirs()
    reg = R.Registry()
    live = reg.current_live()
    settings = {"exchange": "binance", "capital_limit": args.capital, "limits": {}}
    limits = config.build_limits(settings)
    if live:
        manifest, folder = bundle.load_manifest(*live)
    else:
        scratch = R.Registry(os.path.join(tempfile.mkdtemp(prefix="rehearsal_"), "scratch.db"))
        name, source, params = S.generate("sma_cross", {"timeframe": "1h", "fast": 5, "slow": 20})
        sid, ver, _ = scratch.create_strategy("REHEARSAL WIRING TEST ONLY", "template:sma_cross", "universe", "1h", params, source)
        base = os.path.join(paths.LIVE_DIR, "rehearsal_bundles")
        manifest = bundle.freeze(scratch, sid, ver, limits, base_dir=base)
        folder = os.path.join(base, f"{sid}_v{ver}")
    trader = T.LiveTrader(reg, limits, "rehearsal", _exchange(public_only=True), folder, manifest, settings=settings)
    deadline = time.time() + args.minutes * 60
    port, user, pw = _free_port(), "jv" + "%06x" % random.randrange(1 << 24), "%032x" % random.getrandbits(128)
    trader.build_engine(port, user, pw)
    problems = trader.preflight()
    if problems:
        print("Rehearsal preflight problems:", problems)
        return 1
    trader.engine_proc.start()
    print("rehearsal engine started; waiting for it to come up")
    healthy = 0
    emergency_ok = False
    try:
        time.sleep(25)
        while time.time() < deadline and trader.state not in ("STOPPED", "EMERGENCY"):
            snap = trader.step()
            if snap["state"] == "RUNNING" and not snap["problems"] and (snap.get("gate") or {}).get("allow_entries"):
                healthy += 1
            print(f"{snap['ts']} state={snap['state']} data_age={snap.get('data_age_seconds')} gate_ok={(snap.get('gate') or {}).get('allow_entries')} "
                  f"equity={snap.get('equity')} open={len(snap.get('open_positions', []))} failures={snap['api_failures']}", flush=True)
            time.sleep(10)
        if args.test_emergency:
            print("testing emergency stop path against the real Freqtrade API")
            report = trader.emergency_stop("rehearsal: emergency-stop test", wait_seconds=30)
            print(json.dumps(report, default=str))
            emergency_ok = all(ok for _, ok in report["steps"]) and os.path.exists(paths.EMERGENCY_LATCH)
        if healthy >= 10 and emergency_ok:
            from . import readiness
            with open(readiness.rehearsal_ok_path(), "w", encoding="utf-8") as f:
                json.dump({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "healthy_loops": healthy, "emergency_stop_tested": True,
                           "strategy": trader.manifest["strategy_id"]}, f)
            print(f"REHEARSAL PASSED: {healthy} healthy watchdog loops against the real Freqtrade API and the emergency stop worked.")
        else:
            print(f"Rehearsal did not qualify as a pass (healthy loops {healthy}, emergency tested {emergency_ok}).")
    finally:
        trader.engine_proc.terminate()
        for p in (paths.EMERGENCY_LATCH,):
            if args.test_emergency and os.path.exists(p):
                os.remove(p)  # a rehearsal latch must not block a later real start
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jarvis_trading.live.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=lambda a: cmd_status(a) or 0)
    r = sub.add_parser("rehearse")
    r.add_argument("--minutes", type=float, default=3)
    r.add_argument("--capital", type=float, default=12.0)
    r.add_argument("--test-emergency", action="store_true")
    r.set_defaults(fn=cmd_rehearse)
    s = sub.add_parser("start")
    s.add_argument("--interval", type=int, default=10)
    s.set_defaults(fn=cmd_start)
    a = sub.add_parser("approve")
    a.add_argument("--review", type=int, required=True)
    a.set_defaults(fn=cmd_approve)
    ar = sub.add_parser("arm")
    ar.add_argument("--days", type=int, default=30)
    ar.set_defaults(fn=cmd_arm)
    sub.add_parser("disarm").set_defaults(fn=cmd_simple(lambda reg: print("was armed:", approval.disarm(reg, "disarmed from CLI"))))
    sub.add_parser("emergency-stop").set_defaults(fn=cmd_emergency)
    sub.add_parser("clear-emergency").set_defaults(fn=cmd_clear("emergency"))
    sub.add_parser("clear-halt").set_defaults(fn=cmd_clear("halt"))
    f = sub.add_parser("feasibility")
    f.add_argument("--capital", default="10")
    f.add_argument("--currency", default="GBP")
    f.set_defaults(fn=cmd_feasibility)
    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
