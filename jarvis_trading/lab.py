"""
The strategy laboratory: runs one hypothesis through the whole research
pipeline and records every step in the append-only registry.

Pipeline (each step is stored as an experiment; every status change has a reason):
  1. register the strategy (deterministic code from a template)
  2. lookahead / data-leakage analysis          -> biased = REJECTED immediately
  3. backtest on the TRAINING period and the VALIDATION period
     (fees + slippage included)                 -> losing in either = REJECTED early,
                                                   WITHOUT touching the held-out test period
  4. parameter robustness (nearby parameter sets, development period only)
  5. market-regime breakdown (development period only)
  6. the HELD-OUT TEST period, run exactly once per strategy version
  7. Monte Carlo + sign-flip significance on the held-out trades
  8. judgement by the predefined criteria (multiple-testing corrected)
Survivors become CANDIDATE and are eligible for paper trading. Nothing is
"deployed": a candidate has only survived history.

`runner` is injected (the Freqtrade adapter in production, a fake in tests).
"""

import csv
import json
import os
import random

from . import criteria as C
from . import lint as LINT
from . import metrics as M
from . import stats as ST
from . import strategies as S
from . import universe as U

PERIODS = {
    "training": "20190101-20221231",
    "validation": "20230101-20240630",
    "test": "20240701-20260930",
}
LOOKAHEAD_WINDOW = "20230101-20230630"
# Low-frequency strategies may not trade enough in six months for the detector to give a verdict, so it is retried
# on progressively longer windows before an inconclusive result is treated as a failure.
LOOKAHEAD_WINDOWS = (LOOKAHEAD_WINDOW, "20210101-20231231", "20190601-20240630")
STARTING_BALANCE = 1000.0
MAX_NEIGHBOURS = 8       # robustness sweep size cap (a deterministic subset, so it is reproducible)
DEDUP_OVERLAP = 0.80     # Jaccard overlap of (pair, entry day, size tag) at or above which two strategies are "the same"
DEDUP_MIN_TRADES = 20    # below this there is too little behaviour to compare


def _ts(date_str):
    return M.to_epoch(f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}T00:00:00Z")


def _range_epochs(timerange):
    a, b = timerange.split("-")
    return _ts(a), _ts(b) + 86400.0


def load_daily_closes(csv_path):
    rows = []
    with open(csv_path, "r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append((M.to_epoch(r["date"]), float(r["close"])))
    return rows


def neighbour_params(params, factor=0.25):
    """Nearby parameter sets: each numeric parameter scaled down and up by `factor`, one at a time."""
    out = []
    for key, value in params.items():
        if key in ("timeframe",) or not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        for scale in (1 - factor, 1 + factor):
            new = value * scale
            new = max(2, round(new)) if isinstance(value, int) and key not in ("std", "threshold") else round(new, 4)
            if new == value:
                continue
            candidate = dict(params)
            candidate[key] = new
            if "fast" in candidate and "slow" in candidate and candidate["fast"] >= candidate["slow"]:
                continue
            if candidate not in out:
                out.append(candidate)
    return out


class Lab:
    def __init__(self, registry, runner, daily_closes=None, criteria=None, pairs=None, seed=1, mc_sims=3000, concurrency=1):
        self.registry = registry
        self.runner = runner
        self.daily_closes = daily_closes
        self.criteria = criteria or C.ResearchCriteria()
        self.pairs = pairs or list(U.UNIVERSE)
        self.seed = seed
        self.mc_sims = mc_sims
        # Other workers may be mid-run and not yet counted in the trial total; assume each adds a trial,
        # which can only make the significance bar stricter.
        self.concurrency = max(1, int(concurrency))

    # ------------------------------------------------------------------ helpers
    def _backtest(self, name, params, timerange, kind, sid, version, base_params=None):
        res = self.runner.run_backtest(name, timerange, params["timeframe"], pairs=self.pairs)
        a, b = _range_epochs(timerange)
        metrics = M.compute_metrics(res["trades"], STARTING_BALANCE, a, b)
        self.registry.record_experiment(
            sid, version, kind, metrics, notes=f"period {timerange}",
            data_hash=self._data_hash(params["timeframe"]), command=res.get("command"),
            params=base_params or params, seed=self.seed, tool_versions=self.runner.tool_versions())
        return res["trades"], metrics

    def trials_so_far(self):
        """
        Every parameter set the lab has ever backtested, including the neighbours tried for robustness.
        This is the multiple-testing count: the more things tried, the stricter the significance bar.
        """
        return max(1, self.registry.count_trials() + self.concurrency - 1)

    def _signature(self, trades):
        return sorted({f"{t['pair']}|{int(M.to_epoch(t.get('open_ts') or t['close_ts']) // 86400)}|{t.get('tag') or ''}" for t in trades})

    def _near_duplicate_of(self, signature, sid, version):
        """Returns (other_strategy_id, overlap) if this strategy enters (nearly) the same trades as an earlier one."""
        if len(signature) < DEDUP_MIN_TRADES:
            return None
        mine = set(signature)
        for row in self.registry.all_experiments("signature"):
            if (row["strategy_id"], row["version"]) == (sid, version):
                continue
            other = set(row["metrics"].get("entries", []))
            if not other:
                continue
            overlap = len(mine & other) / len(mine | other)
            if overlap >= DEDUP_OVERLAP:
                return row["strategy_id"], overlap
        return None

    def _data_hash(self, timeframe):
        try:
            return self.runner.data_hash(self.pairs, timeframe)
        except Exception:
            return None

    def _reject(self, sid, version, verdict, evidence, extra_note=""):
        self.registry.record_experiment(sid, version, "verdict", {"verdict": verdict.status, "reasons": verdict.reasons,
                                        "criteria": verdict.criteria_fingerprint}, passed=False, notes=extra_note)
        current = self.registry.status(sid, version)
        if current in ("generated",):
            self.registry.set_status(sid, version, "rejected", verdict.summary())
        elif current == "backtested":
            self.registry.set_status(sid, version, "rejected", verdict.summary())
        self.registry.log_decision("verdict", verdict.summary(), sid, version)
        return {"strategy_id": sid, "version": version, "status": "rejected", "verdict": verdict.summary(),
                "codes": verdict.codes(), "evidence": evidence}

    # --------------------------------------------------------------- main entry
    def research(self, template, overrides=None, hypothesis=None):
        name, source, params = S.generate(template, overrides)
        self.runner.write_strategy(name, source)
        is_control = template in S.CONTROL_TEMPLATES
        sid, version, created = self.registry.create_strategy(
            hypothesis or S.hypothesis(template), f"template:{template}", "/".join(self.pairs), params["timeframe"],
            params, source, PERIODS["training"], PERIODS["validation"], PERIODS["test"])
        self.registry.log_decision("start", f"Researching {template} {params} ({'new' if created else 'already registered'})", sid, version)

        status = self.registry.status(sid, version)
        finished = bool(self.registry.experiments(sid, version, "verdict"))
        if status not in ("generated", "backtested") or finished:
            return {"strategy_id": sid, "version": version, "status": status, "verdict": "already researched; not re-run",
                    "codes": [], "evidence": {}}
        if self.registry.experiments(sid, version, "backtest_test"):
            # A previous run was interrupted AFTER consuming the held-out test period without reaching a verdict.
            # Re-running would reuse that data, so the version is retired instead.
            verdict = C.Verdict("REJECTED", [("INTERRUPTED_AFTER_TEST", "a previous run was interrupted after the held-out period was used; "
                                              "the version is retired rather than re-tested on the same data")], self.criteria.fingerprint())
            if status == "generated":
                self.registry.set_status(sid, version, "backtested", "Resumed after interruption.")
            return self._reject(sid, version, verdict, {})
        if status == "backtested":
            self.registry.log_decision("resume", "Resuming an interrupted research run from the start of the pipeline.", sid, version)
        evidence = {"leverage": 1.0}

        # 2a. static leakage lint (catches what the dynamic detector cannot see, e.g. informative-timeframe leaks)
        findings = LINT.future_reference_findings(source)
        self.registry.record_experiment(sid, version, "static_lint", {"findings": findings}, passed=not findings)
        if findings:
            verdict = C.Verdict("REJECTED", [("LOOKAHEAD_BIAS", "static check found forward-looking code: " + "; ".join(findings[:3]))],
                                self.criteria.fingerprint())
            return self._reject(sid, version, verdict, evidence)

        # 2b. dynamic leakage detector
        look = None
        for window in LOOKAHEAD_WINDOWS:
            look = self.runner.run_lookahead_analysis(name, window, params["timeframe"], pairs=self.pairs)
            if look["clean"] is not None:
                break
        evidence["lookahead_clean"] = look["clean"] is True
        self.registry.record_experiment(sid, version, "lookahead", {"clean": look["clean"], "tail": look["raw"][-400:]},
                                        passed=look["clean"] is True, command="freqtrade lookahead-analysis",
                                        tool_versions=self.runner.tool_versions())
        if look["clean"] is not True:
            verdict = C.Verdict("REJECTED", [("LOOKAHEAD_BIAS" if look["clean"] is False else "LEAKAGE_CHECK_INCONCLUSIVE",
                                              "lookahead/data leakage detected" if look["clean"] is False else
                                              "the leakage check gave no verdict, so the strategy cannot be trusted")],
                                self.criteria.fingerprint())
            return self._reject(sid, version, verdict, evidence)

        # 3. training + validation backtests
        train_trades, train_m = self._backtest(name, params, PERIODS["training"], "backtest_training", sid, version)
        val_trades, val_m = self._backtest(name, params, PERIODS["validation"], "backtest_validation", sid, version)
        in_sample_trades = train_trades + val_trades
        evidence["in_sample"] = M.compute_metrics(in_sample_trades, STARTING_BALANCE,
                                                  _range_epochs(PERIODS["training"])[0], _range_epochs(PERIODS["validation"])[1])
        if self.registry.status(sid, version) == "generated":
            self.registry.set_status(sid, version, "backtested", "Training and validation backtests complete.")

        # 3b. behavioural de-duplication: skip strategies that trade (almost) exactly like one already tested
        signature = self._signature(train_trades)
        self.registry.record_experiment(sid, version, "signature", {"entries": signature})
        duplicate = self._near_duplicate_of(signature, sid, version)
        if duplicate:
            other, overlap = duplicate
            verdict = C.Verdict("REJECTED", [("NEAR_DUPLICATE", f"enters {overlap:.0%} of the same trades as {other}; not a distinct strategy")],
                                self.criteria.fingerprint())
            return self._reject(sid, version, verdict, evidence, "Near-duplicate of an earlier strategy; not tested further.")

        early = []
        if train_m["total_return"] <= 0:
            early.append(("LOSING_IN_TRAINING", f"lost money in the training period ({train_m['total_return']:.1%})"))
        if val_m["total_return"] <= 0:
            early.append(("LOSING_IN_VALIDATION", f"lost money in the validation period ({val_m['total_return']:.1%})"))
        if early:
            verdict = C.Verdict("REJECTED", early, self.criteria.fingerprint())
            return self._reject(sid, version, verdict, evidence, "Rejected before the held-out test period was used.")

        # 3c. cheap in-sample gates before the expensive robustness sweep (trade count, drawdown, costs, implausible results)
        pre = C.evaluate_backtest({"in_sample": evidence["in_sample"], "lookahead_clean": True, "leverage": 1.0}, self.criteria)
        if pre.status == "REJECTED":
            return self._reject(sid, version, pre, evidence, "Rejected on in-sample evidence before the robustness sweep.")

        # 4. robustness (development period only): a deterministic subset of nearby parameter sets,
        #    stopping as soon as the failure limit is certain to be exceeded (every neighbour tried still counts as a trial)
        dev_range = "20190101-20240630"
        fails, total = 0, 0
        neighbours = []
        for candidate in (S.neighbours(template, params) or neighbour_params(params)):
            try:
                neighbours.append((candidate,) + S.generate(template, candidate)[:2])
            except ValueError:
                continue  # not a valid parameter combination for this family
        picker = random.Random(json.dumps(params, sort_keys=True))
        if len(neighbours) > MAX_NEIGHBOURS:
            neighbours = picker.sample(neighbours, MAX_NEIGHBOURS)
        fail_limit = int(self.criteria.max_neighbour_fail_fraction * len(neighbours))
        for neighbour, n_name, n_source in neighbours:
            self.runner.write_strategy(n_name, n_source)
            try:
                res = self.runner.run_backtest(n_name, dev_range, neighbour["timeframe"], pairs=self.pairs)
            except Exception:
                continue
            a, b = _range_epochs(dev_range)
            nm = M.compute_metrics(res["trades"], STARTING_BALANCE, a, b)
            total += 1
            fails += nm["total_return"] <= 0
            if fails > fail_limit:
                break  # already beyond the allowed fraction of the full sweep; no need to run the rest
        evidence["robustness"] = {"neighbour_fail_fraction": (fails / total) if total else 1.0, "neighbours_tested": total}
        self.registry.record_experiment(sid, version, "robustness", evidence["robustness"], params=params, seed=self.seed)

        # 5. regimes
        if self.daily_closes:
            labels = ST.classify_regimes(self.daily_closes)
            evidence["regimes"] = ST.metrics_by_regime(in_sample_trades, labels, STARTING_BALANCE, min_trades=8) or None
        else:
            evidence["regimes"] = None
        if evidence["regimes"] is not None:
            self.registry.record_experiment(sid, version, "regimes", {k: {"total_return": v["total_return"], "n_trades": v["n_trades"]} for k, v in evidence["regimes"].items()})

        # 5b. development-stage verdict: a strategy that already fails here never touches the held-out test period
        dev = C.evaluate_backtest({"in_sample": evidence["in_sample"], "lookahead_clean": True, "leverage": 1.0,
                                   "robustness": evidence["robustness"], "regimes": evidence["regimes"]}, self.criteria)
        if dev.status == "REJECTED":
            return self._reject(sid, version, dev, evidence, "Rejected on development-period evidence; held-out test period not used.")

        # 6. held-out test: ONCE per version
        if self.registry.experiments(sid, version, "backtest_test"):
            raise RuntimeError("The held-out test period was already used for this strategy version.")
        test_trades, test_m = self._backtest(name, params, PERIODS["test"], "backtest_test", sid, version)
        evidence["out_of_sample"] = test_m

        # 7. statistics on the held-out trades
        ratios = [t["profit_ratio"] for t in test_trades]
        p_trade = ST.sign_flip_p_value(ratios, n_perm=4000, seed=self.seed)
        # Trades in different pairs overlap in time and are correlated, so a per-trade test overstates the effective
        # sample. Also test per-DAY aggregated profit and keep the more conservative (larger) p-value.
        by_day = {}
        for t in test_trades:
            day = int(M.to_epoch(t["close_ts"]) // 86400)
            by_day[day] = by_day.get(day, 0.0) + t["profit_abs"]
        p_day = ST.sign_flip_p_value([v / STARTING_BALANCE for v in by_day.values()], n_perm=4000, seed=self.seed)
        p_value = None if p_trade is None or p_day is None else max(p_trade, p_day)
        mc = ST.monte_carlo([t["profit_ratio"] for t in in_sample_trades + test_trades], stake_fraction=0.10,
                            n_sims=self.mc_sims, seed=self.seed)
        evidence["monte_carlo"] = {"p_value": p_value, "p_value_per_trade": p_trade, "p_value_per_day": p_day, "ruin_probability": (mc or {}).get("ruin_probability", 1.0), "mc": mc}
        self.registry.record_experiment(sid, version, "monte_carlo", evidence["monte_carlo"], seed=self.seed)

        # 8. judgement
        tested = self.trials_so_far()
        evidence["n_strategies_tested"] = tested
        verdict = C.evaluate_backtest(evidence, self.criteria)
        if verdict.status == "INCOMPLETE":
            # Missing evidence is never a pass, but it is not a rejection either: the version stays 'backtested'.
            self.registry.record_experiment(sid, version, "verdict", {"verdict": "INCOMPLETE", "reasons": verdict.reasons}, passed=False)
            self.registry.log_decision("verdict", verdict.summary(), sid, version)
            return {"strategy_id": sid, "version": version, "status": "backtested", "verdict": verdict.summary(),
                    "codes": verdict.codes(), "evidence": evidence}
        if is_control and verdict.status == "CANDIDATE":
            verdict = C.Verdict("REJECTED", [("CONTROL_STRATEGY", "control strategies are never promoted")], self.criteria.fingerprint())

        if verdict.status == "CANDIDATE":
            self.registry.record_experiment(sid, version, "verdict", {"verdict": "CANDIDATE", "criteria": verdict.criteria_fingerprint}, passed=True)
            self.registry.set_status(sid, version, "candidate", verdict.summary())
            self.registry.log_decision("verdict", f"CANDIDATE -- survived all research gates ({tested} strategies tested)", sid, version)
            return {"strategy_id": sid, "version": version, "status": "candidate", "verdict": "CANDIDATE",
                    "codes": verdict.codes(), "evidence": evidence}
        return self._reject(sid, version, verdict, evidence)
