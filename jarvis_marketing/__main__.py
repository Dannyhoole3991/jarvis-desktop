"""
    py -3 -m jarvis_marketing status                 what the department is doing (every business)
    py -3 -m jarvis_marketing cycle [--stages a,b]   run one operating cycle now
    py -3 -m jarvis_marketing mode TEST              change the mode (you, at your own terminal)
    py -3 -m jarvis_marketing stop ["reason"]        EMERGENCY STOP
    py -3 -m jarvis_marketing resume                 lift the emergency stop
    py -3 -m jarvis_marketing set-password [--bind 100.x.y.z] [--port 8790]
                                                     password for the laptop dashboard (typed, never shown)
    py -3 -m jarvis_marketing serve                  the laptop dashboard on its own, without the Jarvis window
    py -3 -m jarvis_marketing demo                   fill the SIMULATED (mock) business with a full example run

Commands typed here count as Danny: only you can run them at your own terminal.
"""

import argparse
import getpass
import sys
import time

from jarvis_marketing import config, controls as C

DANNY = C.HumanPrincipal("Danny", "terminal")


def _dept():
    from jarvis_marketing import api
    return api.Department()


def main(argv=None):
    p = argparse.ArgumentParser(prog="jarvis_marketing", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["status", "cycle", "mode", "stop", "resume", "set-password", "serve", "demo"])
    p.add_argument("arg", nargs="?")
    p.add_argument("--business")
    p.add_argument("--stages")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8790)
    a = p.parse_args(argv)

    if a.command == "set-password":
        from jarvis_marketing import remote
        first = getpass.getpass("New laptop dashboard password (12+ characters): ")
        if first != getpass.getpass("Type it again: "):
            print("They didn't match; nothing changed.")
            return 1
        path = remote.set_password(first, bind=a.bind, port=a.port)
        print(f"Saved (only a salted hash) to {path}. The dashboard will listen on {a.bind}:{a.port} next time Jarvis starts.")
        if a.bind == "127.0.0.1":
            print("Note: 127.0.0.1 is this PC only. For the laptop, run this again with --bind <this PC's Tailscale address>.")
        return 0
    dept = _dept()
    slug = a.business or next(iter(dept.directors))
    d = dept.director(slug)
    if a.command == "status":
        from jarvis_marketing import api
        print(api.describe(dept))
        for b in dept.directors.values():
            v = api.overview(dept, b.business["slug"])
            print(f"\n{b.business['name']} [{b.business['profile']}]: mode {v['controls']['mode']}, "
                  f"stop {'ON' if v['controls']['emergency_stop']['on'] else 'off'}, schedule {v['controls']['schedule']}")
            for t in v["tasks"][:8]:
                print(f"  task {t['id']:>4} {t['kind']:<9} {t['status']:<10} {t['error'] or ''}")
            for n in v["notifications"][:5]:
                print(f"  - {n['text']}")
        return 0
    if a.command == "cycle":
        r = d.run_cycle(a.stages.split(",") if a.stages else None)
        for t in r["tasks"]:
            print(f"{t['stage']:<9} {t['status']:<10} {t['error'] or ''}")
        print("cycle", r["cycle_id"], r["status"])
        return 0
    if a.command == "mode":
        print("Mode is now", d.controls.set_mode(DANNY, d.bid, (a.arg or "").upper()))
        return 0
    if a.command == "stop":
        d.controls.emergency_stop(DANNY, a.arg or "stopped from the terminal", d.bid)
        print("EMERGENCY STOP is on.")
        return 0
    if a.command == "resume":
        d.controls.resume(DANNY, "resumed from the terminal")
        print("Resumed.")
        return 0
    if a.command == "serve":
        from jarvis_marketing import remote
        dept.start(schedule=True)
        server, info = remote.start(dept)
        if server is None:
            print(info)
            return 1
        print(f"Marketing dashboard on {'https' if info.tls else 'http'}://{server.server_address[0]}:{server.server_address[1]}/  (Ctrl+C stops)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            server.shutdown()
        return 0
    if a.command == "demo":
        return demo(slug)
    return 1


def demo(slug=None):
    """A full SIMULATED run in the mock database, so the dashboard has something to show. Never touches live data."""
    from jarvis_marketing.director import Director
    from jarvis_marketing.store import Store
    b = dict(config.business(slug), profile="mock")
    d = Director(b, store=Store(config.db_path("mock")))
    print("Profile: mock (SIMULATED).")
    for mode in (C.REVIEW, C.TEST):
        if C.mode_index(d.controls.mode(d.bid)) < C.mode_index(mode):
            d.controls.set_mode(DANNY, d.bid, mode, "demo")
    r = d.run_cycle(["research", "evaluate", "brief", "produce", "qa", "design"])
    print("cycle", r["status"], [t["status"] for t in r["tasks"]])
    pending = d.approvals.pending(d.bid)
    print(f"{len(pending)} SIMULATED posts are waiting for approval in the dashboard (approve them there to see the rest).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
