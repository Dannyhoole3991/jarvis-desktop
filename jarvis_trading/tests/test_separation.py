"""
Structural guarantees that experimental learning cannot touch the live account.

  1. No research-side module imports (directly or transitively) anything under jarvis_trading.live.
     Only service.py (the Jarvis-facing interface that carries a HUMAN's typed commands) may.
  2. Only live/approval.py may call the registry methods that make a strategy LIVE or retire it.
  3. The research Risk Engine still refuses live mode; only the live engine (constructed inside the live package) accepts it.
  4. Nothing outside the live package writes to the live directory or reads live credentials.
  5. The research side cannot construct LiveLimits or arming files.
"""

import ast
import os
import re
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, ".."))
PKG = "jarvis_trading"
ALLOWED_TO_IMPORT_LIVE = {"service"}


def modules():
    found = {}
    for base, _, files in os.walk(ROOT):
        if "tests" in base.split(os.sep) or "__pycache__" in base:
            continue
        for f in files:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(base, f), ROOT)[:-3].replace(os.sep, ".")
                name = PKG + ("" if rel == "__init__" else "." + rel.replace(".__init__", ""))
                found[name] = os.path.join(base, f)
    return found


def direct_imports(name, path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    package = name if path.endswith("__init__.py") else name.rsplit(".", 1)[0]
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[:len(parts) - node.level + 1])
                target = base + ("." + node.module if node.module else "")
                out.add(target)
                out.update(f"{target}.{a.name}" for a in node.names)
            elif node.module:
                out.add(node.module)
                out.update(f"{node.module}.{a.name}" for a in node.names)
    return {i for i in out if i.startswith(PKG)}


def closure(start, graph):
    seen, stack = set(), [start]
    while stack:
        cur = stack.pop()
        for dep in graph.get(cur, ()):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


class SeparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mods = modules()
        cls.graph = {n: direct_imports(n, p) for n, p in cls.mods.items()}

    def is_live(self, name):
        return name == PKG + ".live" or name.startswith(PKG + ".live.")

    def test_there_are_modules_to_check(self):
        self.assertGreater(len(self.mods), 20)
        self.assertTrue(any(self.is_live(m) for m in self.mods))

    def test_research_side_never_reaches_the_live_package(self):
        for name in self.mods:
            short = name[len(PKG) + 1:]
            if self.is_live(name) or short in ALLOWED_TO_IMPORT_LIVE:
                continue
            reach = closure(name, self.graph)
            leaked = sorted(m for m in reach if self.is_live(m))
            self.assertEqual(leaked, [], f"{name} can reach the live package via {leaked[:3]}")

    def test_nothing_imports_the_service_layer_except_nothing(self):
        for name in self.mods:
            if name == PKG + ".service":
                continue
            self.assertNotIn(PKG + ".service", self.graph[name], name)

    def test_only_the_approval_module_can_make_a_strategy_live(self):
        guarded = re.compile(r"\.(promote_to_live|rollback_to|retire_live|_go_live)\(")
        allowed = {PKG + ".live.approval", PKG + ".registry"}
        for name, path in self.mods.items():
            if name in allowed:
                continue
            source = open(path, encoding="utf-8").read()
            self.assertIsNone(guarded.search(source), f"{name} calls a live-promotion method")

    def test_live_status_is_unreachable_through_set_status(self):
        from jarvis_trading.registry import GUARDED_STATUSES
        self.assertIn("live", GUARDED_STATUSES)

    def test_only_the_live_package_touches_the_live_directory_or_credentials(self):
        pattern = re.compile(r"\b(LIVE_DIR|ARMED_FILE|EMERGENCY_LATCH|HALT_FILE|GATE_FILE|BUNDLE_DIR|load_credentials|store_credentials)\b|JARVIS_EXCHANGE_API")
        for name, path in self.mods.items():
            if self.is_live(name) or name.endswith(".service"):
                continue
            self.assertIsNone(pattern.search(open(path, encoding="utf-8").read()), f"{name} references live paths or credentials")

    def test_research_risk_engine_refuses_live_and_live_engine_refuses_paper(self):
        from jarvis_trading import risk as RK
        from jarvis_trading.live import limits as L
        self.assertEqual(RK.RiskEngine.ALLOWED_MODES, ("backtest", "paper"))
        order = RK.OrderRequest("BTC/USDT", "buy", 6.0, mode="live")
        state = RK.PortfolioState(equity=1000, peak_equity=1000, start_of_day_equity=1000)
        self.assertFalse(RK.RiskEngine().evaluate(order, state).approved)
        live = L.LiveRiskEngine(L.LiveLimits(capital_limit=12.0))
        live.set_context(1, 0, False, 0)
        self.assertFalse(live.evaluate(RK.OrderRequest("BTC/USDT", "buy", 6.0, mode="paper"), RK.PortfolioState(12, 12, 12)).approved)

    def test_the_old_live_venue_placeholder_still_cannot_be_constructed(self):
        from jarvis_trading import execution as E
        with self.assertRaises(E.LiveTradingDisabled):
            E.LiveVenue()

    def test_default_state_places_no_real_orders(self):
        # with no ARMED file the trader's live preconditions fail, whatever else is configured
        import tempfile
        from unittest import mock
        from jarvis_trading import registry as R
        from jarvis_trading.live import approval, paths
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(paths, "ARMED_FILE", os.path.join(tmp, "ARMED.json")):
                reg = R.Registry(os.path.join(tmp, "x.db"))
                self.assertEqual(approval.arming_problems(reg), ["live trading is not armed"])


if __name__ == "__main__":
    unittest.main()
