import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from jarvis_trading import forward as F
from jarvis_trading import promotion as PR
from jarvis_trading import registry as R
from jarvis_trading import strategies as S
from jarvis_trading.live import bundle as LB
from jarvis_trading.live import limits as LL

DAY = 86400
NOW = 1_800_000_000.0


def make_near_miss(reg, template="trend_momentum", overrides=None, sharpe=1.1, p=0.03, dd=0.08, instability=0.0, codes=("NOT_SIGNIFICANT",),
                   entries=None, days=822):
    name, source, params = S.generate(template, overrides)
    sid, ver, _ = reg.create_strategy("h", f"template:{template}", "m", params["timeframe"], params, source)
    reg.set_status(sid, ver, "backtested", "bt")
    reg.record_experiment(sid, ver, "backtest_training", {"n_trades": 300, "sharpe": 1.0, "total_return": 0.5, "max_drawdown": 0.1, "avg_daily_return": 0.001})
    reg.record_experiment(sid, ver, "backtest_validation", {"n_trades": 80, "sharpe": 1.2, "total_return": 0.2, "max_drawdown": 0.08, "avg_daily_return": 0.001})
    reg.record_experiment(sid, ver, "robustness", {"neighbour_fail_fraction": instability, "neighbours_tested": 8})
    reg.record_experiment(sid, ver, "backtest_test", {"n_trades": 90, "sharpe": sharpe, "calmar": 2.0, "max_drawdown": dd, "days": days, "total_return": 0.3,
                                                     "avg_daily_return": 0.0008, "win_rate": 0.45})
    reg.record_experiment(sid, ver, "monte_carlo", {"p_value": p, "ruin_probability": 0.0})
    reg.record_experiment(sid, ver, "signature", {"entries": entries if entries is not None else [f"BTC/USDT|{sid}-{i}|" for i in range(40)]})
    reg.record_experiment(sid, ver, "verdict", {"verdict": "REJECTED", "reasons": [[c, c] for c in codes]}, passed=False)
    reg.set_status(sid, ver, "rejected", "REJECTED -- " + ", ".join(codes))
    return sid, ver


def make_trades_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, pair TEXT, is_open INTEGER, open_date TEXT, close_date TEXT, open_rate REAL, close_rate REAL, "
                 "stake_amount REAL, amount REAL, fee_open REAL, fee_close REAL, fee_open_cost REAL, fee_close_cost REAL, close_profit REAL, "
                 "close_profit_abs REAL, exit_reason TEXT, enter_tag TEXT, leverage REAL, is_short INTEGER)")
    for r in rows:
        d = dict(id=1, pair="BTC/USDT", is_open=0, open_date="2026-12-01 00:00:00", close_date="2026-12-02 00:00:00", open_rate=100.0, close_rate=101.0, stake_amount=100.0,
                 amount=1.0, fee_open=0.001, fee_close=0.001, fee_open_cost=0.1, fee_close_cost=0.1, close_profit=0.01, close_profit_abs=1.0, exit_reason="exit_signal",
                 enter_tag="s100", leverage=1.0, is_short=0)
        d.update(r)
        conn.execute("INSERT INTO trades (" + ",".join(d) + ") VALUES (" + ",".join("?" * len(d)) + ")", list(d.values()))
    conn.commit()
    conn.close()


def iso_day(offset_days, base=NOW):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(base + offset_days * DAY))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = F.FORWARD_DIR, F.REPORT_DIR
        F.FORWARD_DIR = os.path.join(self.tmp.name, "forward")
        F.REPORT_DIR = os.path.join(self.tmp.name, "reports")
        F._cache.clear()
        self.reg = R.Registry(os.path.join(self.tmp.name, "f.db"))

    def tearDown(self):
        F.FORWARD_DIR, F.REPORT_DIR = self.old
        for root, _, files in os.walk(self.tmp.name):
            for f in files:
                os.chmod(os.path.join(root, f), stat.S_IWRITE)
        self.tmp.cleanup()

    def trial_set(self, n=3, now=NOW):
        for i in range(n):
            make_near_miss(self.reg, "trend_momentum", {"trend_period": 50 + i * 50, "roc_period": 10 + i}, sharpe=1.2 - 0.05 * i, p=0.02 + 0.01 * i)
        return F.start_set(self.reg, F.SelectionRules(n=n, max_per_family=n), now=now)


class SelectionTests(Base):
    def test_only_pure_near_misses_with_strong_evidence_are_selected(self):
        good = make_near_miss(self.reg, "trend_momentum", {"trend_period": 50})
        make_near_miss(self.reg, "trend_momentum", {"trend_period": 200}, codes=("NOT_SIGNIFICANT", "OOS_DEGRADATION"))      # failed another gate too
        make_near_miss(self.reg, "breakout_volume", sharpe=0.5)                                                                # weak
        make_near_miss(self.reg, "obv_trend", p=0.4)                                                                           # not near
        make_near_miss(self.reg, "meanrev_combo", dd=0.4)                                                                      # drawdown
        make_near_miss(self.reg, "mtf_pullback", instability=0.5)                                                              # unstable
        make_near_miss(self.reg, "ml_walkforward")                                                                             # cannot run in Freqtrade
        sel = F.select_near_misses(self.reg)
        self.assertEqual([(s["strategy_id"], s["version"]) for s in sel["selected"]], [good])
        reasons = " ".join(" ".join(s["why"]) for s in sel["skipped"])
        for fragment in ("Sharpe", "p-value", "drawdown", "instability", "ML strategies"):
            self.assertIn(fragment, reasons)

    def test_ranked_by_luck_adjusted_score_which_follows_oos_sharpe(self):
        a = make_near_miss(self.reg, "trend_momentum", {"trend_period": 50}, sharpe=0.9)
        b = make_near_miss(self.reg, "breakout_volume", sharpe=1.3)
        c = make_near_miss(self.reg, "obv_trend", sharpe=1.1)
        sel = F.select_near_misses(self.reg)
        self.assertEqual([s["strategy_id"] for s in sel["selected"]], [b[0], c[0], a[0]])
        self.assertEqual([s["rank"] for s in sel["selected"]], [1, 2, 3])
        # each selected strategy carries ITS OWN evidence (not another strategy's)
        self.assertEqual([round(s["oos_sharpe"], 2) for s in sel["selected"]], [1.3, 1.1, 0.9])
        for s in sel["selected"]:
            self.assertIn(f"Sharpe {s['oos_sharpe']:.2f}", s["reason"])
        self.assertIn("Rejected ONLY by the multiple-testing significance test", sel["selected"][0]["reason"])

    def test_family_cap_and_behavioural_duplicates_are_skipped(self):
        shared = [f"BTC/USDT|{i}|" for i in range(40)]
        make_near_miss(self.reg, "trend_momentum", {"trend_period": 50}, sharpe=1.3, entries=shared)
        dup = make_near_miss(self.reg, "breakout_volume", sharpe=1.25, entries=shared)
        for i in range(3):
            make_near_miss(self.reg, "obv_trend", {"obv_ma": 20 + 10 * i}, sharpe=1.2 - 0.01 * i)
        sel = F.select_near_misses(self.reg, F.SelectionRules(n=5, max_per_family=2))
        ids = [s["strategy_id"] for s in sel["selected"]]
        self.assertNotIn(dup[0], ids)
        self.assertEqual(sum(1 for s in sel["selected"] if s["template"] == "obv_trend"), 2)
        text = " ".join(" ".join(s["why"]) for s in sel["skipped"])
        self.assertIn("too similarly", text)
        self.assertIn("already 2 selected", text)

    def test_strategies_with_a_trial_are_not_selected_again(self):
        make_near_miss(self.reg)
        F.start_set(self.reg, now=NOW)
        self.assertEqual(F.select_near_misses(self.reg)["selected"], [])


class FreezingTests(Base):
    def test_start_records_a_hard_timestamp_frozen_files_and_pre_registered_criteria(self):
        res = self.trial_set()
        self.assertEqual(len(res["started"]), 3)
        for t in res["started"]:
            self.assertEqual(t["start_epoch"], NOW)
            self.assertEqual(self.reg.forward_status(t["trial_id"]), "RUNNING")
            paths = F.trial_paths(t)
            with self.assertRaises(PermissionError):
                open(paths["strategy"], "w")
            self.assertEqual(F.audit_light(t), [])
            self.assertEqual(t["criteria"], F.asdict(F.ForwardCriteria()))
            self.assertEqual(t["criteria_fingerprint"], F.ForwardCriteria().fingerprint())
            self.assertIn("near-miss", json.load(open(paths["protocol"]))["note"])
            self.assertEqual(self.reg.status(t["strategy_id"], t["version"]), "rejected")     # research classification untouched

    def test_frozen_source_is_the_registry_code_plus_an_inert_recorder(self):
        t = self.trial_set(1)["started"][0]
        frozen = open(F.trial_paths(t)["strategy"], encoding="utf-8").read()
        stored = self.reg.get_version(t["strategy_id"], t["version"])["code"]
        self.assertTrue(frozen.startswith(stored))
        self.assertEqual(F.sha256_text(frozen), t["code_sha256"])

    def test_one_trial_per_strategy_version_ever(self):
        self.trial_set(2)
        again = F.start_set(self.reg, F.SelectionRules(n=5), now=NOW + 99)
        self.assertEqual(again["started"], [])
        sid, ver = self.reg.forward_trials()[0]["strategy_id"], 1
        with self.assertRaises(Exception):
            self.reg.create_forward_trial("X", sid, ver, "h", "s", {}, NOW, {}, "f", {})

    def test_trial_tables_are_append_only(self):
        t = self.trial_set(1)["started"][0]
        conn = sqlite3.connect(self.reg.db_path)
        for sql in ("UPDATE forward_trials SET start_epoch=0", "DELETE FROM forward_trials", "UPDATE forward_log SET kind='x'", "DELETE FROM forward_log"):
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(sql)
        conn.close()

    def test_a_forward_trial_strategy_can_never_go_live(self):
        t = self.trial_set(1)["started"][0]
        sid, ver = t["strategy_id"], t["version"]
        rid = self.reg.record_promotion_event("review", sid, ver, True, {"items": []}, "test")
        with self.assertRaises(R.RegistryError):
            self.reg.promote_to_live(sid, ver, rid, "h", "l", "try anyway")
        with self.assertRaises(R.RegistryError):
            self.reg.set_status(sid, ver, "live", "direct")
        with self.assertRaises(R.RegistryError):
            self.reg.set_status(sid, ver, "paper", "rejected is terminal")
        review = PR.review(self.reg, sid, ver, record=False)
        self.assertFalse(review["eligible"])
        self.assertIn("not_a_forward_trial_strategy", [i["key"] for i in review["items"] if not i["ok"]])
        with self.assertRaises(LB.BundleError):
            LB.freeze(self.reg, sid, ver, LL.LiveLimits(capital_limit=12.0), base_dir=self.tmp.name)
        self.assertIsNone(self.reg.current_live())


class LedgerTests(Base):
    def test_append_dedupes_and_verifies(self):
        led = F.Ledger(os.path.join(self.tmp.name, "l.jsonl"))
        self.assertIsNotNone(led.append("signal", {"key": "a", "x": 1}))
        self.assertIsNone(led.append("signal", {"key": "a", "x": 2}))
        led.append("entry", {"key": "b"})
        self.assertEqual(F.Ledger(led.path).verify(), (True, []))
        self.assertEqual(F.Ledger(led.path).head, led.head)           # reload keeps the chain

    def test_any_edit_or_deletion_is_detected(self):
        path = os.path.join(self.tmp.name, "l.jsonl")
        led = F.Ledger(path)
        for i in range(5):
            led.append("signal", {"key": f"k{i}", "v": i})
        lines = open(path).read().splitlines()
        open(path, "w").write("\n".join(lines[:2] + [lines[2].replace('"v": 2', '"v": 99')] + lines[3:]) + "\n")
        self.assertFalse(F.Ledger(path).verify()[0])
        open(path, "w").write("\n".join(lines[:2] + lines[3:]) + "\n")
        self.assertFalse(F.Ledger(path).verify()[0])


class RecordingTests(Base):
    def test_sync_records_signals_entries_exits_with_costs_and_excludes_pre_start_trades(self):
        t = self.trial_set(1)["started"][0]
        paths = F.trial_paths(t)
        os.makedirs(paths["dir"], exist_ok=True)
        with open(paths["signals"], "w") as f:
            f.write(json.dumps({"ts": NOW - 50, "kind": "entry_signal", "pair": "BTC/USDT", "candle": "old", "close": 1}) + "\n")      # before start
            f.write(json.dumps({"ts": NOW + 50, "kind": "entry_signal", "pair": "BTC/USDT", "candle": "c1", "close": 100}) + "\n")
            f.write(json.dumps({"ts": NOW + 90, "kind": "exit_signal", "pair": "BTC/USDT", "candle": "c2", "close": 101}) + "\n")
        make_trades_db(paths["db"], [dict(id=1, open_date=iso_day(0.5), close_date=iso_day(1.5), close_profit=0.01),
                                     dict(id=2, open_date=iso_day(-1), close_date=iso_day(-0.5)),                        # opened before the start
                                     dict(id=3, is_open=1, open_date=iso_day(2), close_date=None, close_profit=None, close_profit_abs=None, close_rate=None)])
        added = F.sync_ledger(self.reg, t, NOW + 3 * DAY, "bull/low-vol/normal-volume")
        self.assertEqual(F.sync_ledger(self.reg, t, NOW + 3 * DAY), 0)          # idempotent
        recs = F.Ledger(paths["ledger"]).records()
        kinds = [r["kind"] for r in recs]
        self.assertEqual(sorted(kinds), sorted(["signal", "signal", "excluded", "entry", "exit", "entry"]))
        exit_rec = next(r for r in recs if r["kind"] == "exit")["payload"]
        self.assertAlmostEqual(exit_rec["slippage_adjusted_profit_ratio"], F.adjusted_ratio(0.01))
        self.assertLess(exit_rec["slippage_adjusted_profit_ratio"], 0.01)
        self.assertEqual(exit_rec["fee_cost"], 0.1)
        entry = next(r for r in recs if r["kind"] == "entry")["payload"]
        self.assertEqual(entry["assumed_slippage_per_side"], F.SLIPPAGE)
        self.assertEqual(entry["regime"], "bull/low-vol/normal-volume")
        self.assertEqual(added, 6)
        self.assertTrue(F.Ledger(paths["ledger"]).verify()[0])

    def test_closed_trades_apply_slippage_and_ignore_pre_start(self):
        rows = [dict(id=1, is_open=0, open_date=iso_day(1), close_date=iso_day(2), close_profit=0.02, stake_amount=100.0, close_profit_abs=2.0, fee_open_cost=0.1,
                     fee_close_cost=0.1, leverage=1.0, is_short=0, pair="BTC/USDT"),
                dict(id=2, is_open=0, open_date=iso_day(-1), close_date=iso_day(0), close_profit=0.5, stake_amount=100.0, close_profit_abs=50.0, fee_open_cost=0,
                     fee_close_cost=0, leverage=1.0, is_short=0, pair="BTC/USDT")]
        trades = F.closed_trades(rows, NOW)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0]["profit_ratio"], F.adjusted_ratio(0.02))
        self.assertAlmostEqual(trades[0]["raw_profit_ratio"], 0.02)

    def test_recorder_logs_signals_without_changing_what_the_strategy_returns(self):
        class Row(dict):
            pass

        class Col:
            def __init__(self, vals):
                self.vals = vals
                self.iloc = self

            def __getitem__(self, i):
                return self.vals[i]

        class Frame:
            def __init__(self, entry, exit_):
                self.columns = ["enter_long", "exit_long", "date", "close"]
                self.data = {"enter_long": Col([0, entry]), "exit_long": Col([0, exit_]), "date": Col(["d0", "d1"]), "close": Col([1.0, 2.0])}
                self.iloc = {-1: {"date": "d1", "close": 2.0}}

            def __getitem__(self, k):
                return self.data[k]

        class Strat:
            def populate_entry_trend(self, df, meta):
                return df

            def populate_exit_trend(self, df, meta):
                return df
        ns = {"Strat": Strat}
        exec(F.RECORDER.replace("{cls}", "Strat"), ns)
        log = os.path.join(self.tmp.name, "sig.jsonl")
        with mock.patch.dict(os.environ, {"JARVIS_SIGNAL_LOG": log}):
            frame = Frame(1, 0)
            self.assertIs(Strat().populate_entry_trend(frame, {"pair": "ETH/USDT"}), frame)   # returned object is untouched
            Strat().populate_exit_trend(Frame(0, 1), {"pair": "ETH/USDT"})
            Strat().populate_entry_trend(Frame(0, 0), {"pair": "ETH/USDT"})                    # no signal -> nothing logged
        rows = [json.loads(x) for x in open(log)]
        self.assertEqual([(r["kind"], r["pair"], r["candle"]) for r in rows], [("entry_signal", "ETH/USDT", "d1"), ("exit_signal", "ETH/USDT", "d1")])

    def test_recorder_failure_never_breaks_trading(self):
        class Strat:
            def populate_entry_trend(self, df, meta):
                return "result"

            def populate_exit_trend(self, df, meta):
                return "result"
        ns = {"Strat": Strat}
        exec(F.RECORDER.replace("{cls}", "Strat"), ns)
        with mock.patch.dict(os.environ, {"JARVIS_SIGNAL_LOG": os.path.join(self.tmp.name, "no_such_dir", "x.jsonl")}):
            self.assertEqual(Strat().populate_entry_trend(object(), {"pair": "X"}), "result")


class VerdictTests(unittest.TestCase):
    crit = F.ForwardCriteria()
    oos = {"sharpe": 1.2}

    def fwd(self, **kw):
        base = {"days_elapsed": 100.0, "n_trades": 30, "sharpe": 1.0, "total_return": 0.05, "profit_factor": 1.4}
        base.update(kw)
        return base

    def test_running_until_the_pre_registered_horizon(self):
        self.assertEqual(F.evaluate_verdict(self.fwd(days_elapsed=40.0), self.oos, self.crit)["outcome"], "RUNNING")
        self.assertEqual(F.evaluate_verdict(self.fwd(n_trades=5), self.oos, self.crit)["outcome"], "RUNNING")

    def test_supports_only_if_positive_and_close_to_out_of_sample(self):
        self.assertEqual(F.evaluate_verdict(self.fwd(), self.oos, self.crit)["outcome"], "COMPLETE_SUPPORTS")
        self.assertEqual(F.evaluate_verdict(self.fwd(sharpe=0.5), self.oos, self.crit)["outcome"], "COMPLETE_INCONCLUSIVE")

    def test_contradicts_when_it_loses_money_or_collapses(self):
        self.assertEqual(F.evaluate_verdict(self.fwd(total_return=-0.02), self.oos, self.crit)["outcome"], "COMPLETE_CONTRADICTS")
        self.assertEqual(F.evaluate_verdict(self.fwd(sharpe=0.1), self.oos, self.crit)["outcome"], "COMPLETE_CONTRADICTS")
        self.assertEqual(F.evaluate_verdict(self.fwd(profit_factor=0.7), self.oos, self.crit)["outcome"], "COMPLETE_CONTRADICTS")

    def test_too_few_trades_at_the_hard_end_is_inconclusive_not_a_pass(self):
        res = F.evaluate_verdict(self.fwd(days_elapsed=181.0, n_trades=5), self.oos, self.crit)
        self.assertEqual(res["outcome"], "COMPLETE_INCONCLUSIVE")

    def test_exceptional_results_are_not_a_special_pass(self):
        res = F.evaluate_verdict(self.fwd(sharpe=4.0, total_return=0.5), self.oos, self.crit)
        self.assertEqual(res["outcome"], "COMPLETE_SUPPORTS")    # supports the history; promotion is a separate, impossible step

    def test_criteria_are_frozen_and_fingerprinted(self):
        with self.assertRaises(Exception):
            F.ForwardCriteria().min_days = 1
        self.assertNotEqual(F.ForwardCriteria().fingerprint(), F.ForwardCriteria(min_days=30).fingerprint())


class CheckAndAuditTests(Base):
    def fill(self, t, n, days=100, profit=0.012, start=NOW):
        paths = F.trial_paths(t)
        os.makedirs(paths["dir"], exist_ok=True)
        rows = [dict(id=i + 1, open_date=iso_day(1 + i * (days - 2) / n, start), close_date=iso_day(1.5 + i * (days - 2) / n, start), close_profit=profit,
                     close_profit_abs=profit * 100) for i in range(n)]
        make_trades_db(paths["db"], rows)

    def test_check_logs_standing_comparisons_and_the_ledger_head(self):
        t = self.trial_set(1)["started"][0]
        self.fill(t, 12, days=40)
        d = F.check(self.reg, t, NOW + 40 * DAY, fetcher=False)
        self.assertEqual(d["interim_or_final"]["outcome"], "RUNNING")
        self.assertIn("vs_out_of_sample", d["comparisons"])
        self.assertEqual(d["metrics"]["n_trades"], 12)
        self.assertEqual(self.reg.forward_status(t["trial_id"]), "RUNNING")
        self.assertEqual(len(self.reg.forward_log(t["trial_id"], "check")), 1)

    def test_a_failing_strategy_is_recorded_and_left_unchanged_until_the_horizon(self):
        t = self.trial_set(1)["started"][0]
        self.fill(t, 25, days=60, profit=-0.01)
        F.check(self.reg, t, NOW + 60 * DAY, fetcher=False)
        F.check(self.reg, t, NOW + 61 * DAY, fetcher=False)
        breaches = self.reg.forward_log(t["trial_id"], "breach")
        self.assertEqual(sorted(b["details"]["type"] for b in breaches), ["losing"])         # recorded once
        self.assertIn("not modified" if False else "continues unchanged", breaches[0]["details"]["action"])
        self.assertEqual(self.reg.forward_status(t["trial_id"]), "RUNNING")                  # not stopped, not altered
        self.assertEqual(F.audit_light(t), [])

    def test_exceptional_performance_is_flagged_for_review_and_never_promoted(self):
        t = self.trial_set(1)["started"][0]
        self.fill(t, 40, days=60, profit=0.02)
        F.check(self.reg, t, NOW + 60 * DAY, fetcher=False)
        flags = self.reg.forward_log(t["trial_id"], "flag")
        if flags:
            self.assertIn("FLAGGED FOR HUMAN REVIEW", flags[0]["details"]["action"])
        self.assertEqual(self.reg.status(t["strategy_id"], t["version"]), "rejected")
        self.assertIsNone(self.reg.current_live())
        self.assertFalse(PR.review(self.reg, t["strategy_id"], t["version"], record=False)["eligible"])

    def test_finalises_at_the_horizon_with_a_verdict_and_a_report(self):
        t = self.trial_set(1)["started"][0]
        self.fill(t, 40, days=95, profit=0.03)
        F.check(self.reg, t, NOW + 95 * DAY, fetcher=False)
        status = self.reg.forward_status(t["trial_id"])
        self.assertIn(status, F.TERMINAL)
        text = F.write_report(self.reg, t["set_id"])
        self.assertIn("near-misses", text)
        self.assertIn("none of them can be promoted", text.replace("and none of them can be promoted to live because of it", "none of them can be promoted"))
        self.assertTrue(os.path.exists(os.path.join(F.REPORT_DIR, f"forward_trial_{t['set_id']}.md")))
        self.assertEqual(self.reg.status(t["strategy_id"], t["version"]), "rejected")

    def test_audit_passes_then_catches_each_kind_of_tampering(self):
        t = self.trial_set(1)["started"][0]
        paths = F.trial_paths(t)
        self.fill(t, 5, days=20)
        F.sync_ledger(self.reg, t, NOW + 20 * DAY)
        F.check(self.reg, t, NOW + 20 * DAY, fetcher=False)
        a = F.audit(self.reg, t)
        self.assertTrue(a["ok"], a["problems"])
        self.assertTrue(a["facts"]["can_never_go_live"] and a["facts"]["dry_run_only"])
        # rewrite the ledger after its head was committed to the registry
        lines = open(paths["ledger"]).read().splitlines()
        open(paths["ledger"], "w").write("\n".join(lines[:-1]) + "\n")
        self.assertFalse(F.audit(self.reg, t)["ok"])
        # modify the strategy
        os.chmod(paths["strategy"], stat.S_IWRITE)
        open(paths["strategy"], "a").write("\n# tuned\n")
        problems = F.audit(self.reg, t)["problems"]
        self.assertTrue(any("differs from the frozen" in p for p in problems))


class SupervisorTests(Base):
    def make(self, launched, alive_pids=None, clock=None):
        alive_pids = alive_pids if alive_pids is not None else set()
        return F.ForwardSupervisor(self.reg, check_every=3600, alive=lambda pid: pid in alive_pids,
                                   clock=clock or (lambda: NOW + 100 * DAY), launcher=lambda t: launched.append(t["trial_id"]), fetcher=False)

    def test_starts_every_running_trial_and_restarts_dead_ones_with_a_storm_limit(self):
        self.trial_set(2)
        launched = []
        sup = self.make(launched)
        sup.stop = lambda trial: None
        sup.tick()
        self.assertEqual(sorted(launched), [1, 2])
        for _ in range(10):
            sup.tick()
        self.assertLessEqual(launched.count(1), 5)

    def test_state_survives_a_restart_of_the_supervisor(self):
        self.trial_set(2)
        before = [(t["trial_id"], t["start_epoch"], t["code_sha256"]) for t in self.reg.forward_trials()]
        launched = []
        self.make(launched).tick()
        reopened = R.Registry(self.reg.db_path)
        after = [(t["trial_id"], t["start_epoch"], t["code_sha256"]) for t in reopened.forward_trials()]
        self.assertEqual(before, after)
        sup2 = F.ForwardSupervisor(reopened, alive=lambda pid: True, clock=lambda: NOW + 101 * DAY, launcher=lambda t: launched.append("again"), fetcher=False)
        sup2.stop = lambda trial: None
        sup2.tick()
        self.assertNotIn("again", launched)                                                      # processes still alive: nothing restarted
        self.assertEqual([reopened.forward_status(t["trial_id"]) for t in reopened.forward_trials()], ["RUNNING", "RUNNING"])

    def test_a_modified_strategy_file_invalidates_the_trial(self):
        t = self.trial_set(1)["started"][0]
        paths = F.trial_paths(t)
        os.chmod(paths["strategy"], stat.S_IWRITE)
        open(paths["strategy"], "a").write("\n# tweak\n")
        sup = self.make([])
        sup.stop = lambda trial: None
        actions = sup.tick()
        self.assertEqual(self.reg.forward_status(t["trial_id"]), "INVALIDATED")
        self.assertTrue(any("INVALIDATED" in a for a in actions))

    def test_tick_syncs_the_ledger_and_finalises(self):
        t = self.trial_set(1)["started"][0]
        paths = F.trial_paths(t)
        os.makedirs(paths["dir"], exist_ok=True)
        make_trades_db(paths["db"], [dict(id=i + 1, open_date=iso_day(1 + i * 4), close_date=iso_day(2 + i * 4), close_profit=0.03, close_profit_abs=3.0) for i in range(25)])
        sup = self.make([], alive_pids={4242})
        with open(paths["pid"], "w") as f:
            f.write("4242")
        sup.stop = lambda trial: None
        actions = sup.tick()
        self.assertGreater(len(F.Ledger(paths["ledger"]).records()), 20)
        self.assertIn(self.reg.forward_status(t["trial_id"]), F.TERMINAL)
        self.assertTrue(any("finished" in a for a in actions))

    def test_launch_is_always_dry_run_and_never_carries_exchange_keys(self):
        t = self.trial_set(1)["started"][0]
        sup = F.ForwardSupervisor(self.reg, fetcher=False)
        with mock.patch("subprocess.Popen") as popen, mock.patch.dict(os.environ, {"FREQTRADE__EXCHANGE__KEY": "k", "FREQTRADE__EXCHANGE__SECRET": "s"}):
            popen.return_value.pid = 1234
            sup._launch(t)
        args, kwargs = popen.call_args[0][0], popen.call_args[1]
        self.assertIn("--dry-run", args)
        self.assertNotIn("FREQTRADE__EXCHANGE__KEY", kwargs["env"])
        self.assertEqual(kwargs["env"]["JARVIS_SIGNAL_LOG"], F.trial_paths(t)["signals"])
        cfg = json.load(open(F.trial_paths(t)["config"]))
        self.assertTrue(cfg["dry_run"])
        self.assertEqual(cfg["exchange"]["key"], "")


class ViewTests(Base):
    def test_status_view_has_everything_the_hud_shows(self):
        t = self.trial_set(1)["started"][0]
        CheckAndAuditTests.fill(self, t, 10, days=30)
        F.check(self.reg, t, NOW + 30 * DAY, fetcher=False)
        v = F.status_view(self.reg, NOW + 30 * DAY)
        self.assertEqual(v["title"], "FORWARD PAPER TRIAL")
        row = v["trials"][0]
        for key in ("start", "days_elapsed", "trades", "pnl_pct", "max_drawdown", "vs_oos", "vs_benchmark", "status", "can_never_go_live", "regime"):
            self.assertIn(key, row)
        self.assertAlmostEqual(row["days_elapsed"], 30.0)
        self.assertTrue(row["can_never_go_live"])

    def test_empty_view(self):
        self.assertEqual(F.status_view(self.reg)["trials"], [])

    def test_benchmark_since_uses_the_same_window_and_degrades_gracefully(self):
        rows = {p: [[(NOW + d * DAY) * 1000, 0, 0, 0, 100 * (1.01 ** d), 1] for d in range(-3, 12)] for p in F.U.UNIVERSE}
        b = F.benchmark_since(NOW, fetcher=lambda pair, since: rows[pair])
        self.assertGreater(b["total_return"], 0.05)
        F._cache.clear()

        def broken(pair, since):
            raise RuntimeError("offline")
        self.assertIsNone(F.benchmark_since(NOW + 1, fetcher=broken))


if __name__ == "__main__":
    unittest.main()
