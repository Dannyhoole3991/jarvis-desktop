"""
Machine-learning strategy family: walk-forward gradient boosting.

A "strategy" here is a configuration (features, label horizon, retrain
schedule, probability threshold, stop). The model is trained by `mlrunner`,
never in this module. The source text stored in the registry is a
canonical description of the configuration, which is what gets hashed.

Leakage rules the runner enforces and tests:
  - features at bar t use only bars <= t (checked by truncation: recomputing
    on data cut at t must reproduce every feature at or before t exactly);
  - a training row at bar t is used only once its label (the return over the
    next `horizon` bars) is fully known, i.e. when t + horizon <= retrain time;
  - hyper-parameters are fixed in advance; only the configuration below varies;
  - entries fill at the NEXT bar's open, after fees and slippage.

`ml_leak_control` deliberately adds a feature built from the next bar's close
to prove the causality check catches it. It is never promoted.

Paper trading: this family runs inside the lab's own simulator, not Freqtrade,
so it cannot currently reach paper trading. The registry flags that plainly; a
Freqtrade/FreqAI port would be needed first.
"""

import hashlib
import json
import random

FAMILIES = {
    "ml_walkforward": {
        "hypothesis": "A gradient-boosted model of recent returns, volatility, momentum and volume, retrained walk-forward, can estimate the probability that the next few bars' return beats trading costs; trade only when that probability is high.",
        "defaults": {"timeframe": "4h", "horizon": 12, "threshold": 0.60, "window_days": 730, "features": "basic", "stoploss": 0.08, "leak": False},
        "space": {"timeframe": ["4h", "1d"], "horizon": [3, 6, 12, 24], "threshold": [0.55, 0.60, 0.65], "window_days": [0, 365, 730],
                  "features": ["basic", "extended"], "stoploss": [0.05, 0.08, 0.12]},
    },
    "ml_leak_control": {
        "control": True,
        "hypothesis": "CONTROL ONLY. Adds a feature computed from the NEXT bar's close. Exists to prove the causality check catches leaked features. Never promoted.",
        "defaults": {"timeframe": "4h", "horizon": 12, "threshold": 0.60, "window_days": 730, "features": "basic", "stoploss": 0.08, "leak": True},
        "space": {"timeframe": ["4h"], "horizon": [12], "threshold": [0.60], "window_days": [730], "features": ["basic"], "stoploss": [0.08]},
    },
}

CONTROL_FAMILIES = tuple(k for k, v in FAMILIES.items() if v.get("control"))
RESEARCH_FAMILIES = tuple(k for k, v in FAMILIES.items() if not v.get("control"))

# fixed in advance; never tuned from results
MODEL_PARAMS = {"max_depth": 3, "learning_rate": 0.05, "max_iter": 80, "min_samples_leaf": 100, "l2_regularization": 1.0}
RETRAIN_BARS = {"4h": 180, "1d": 30}       # about monthly
MIN_TRAIN_BARS = {"4h": 2190, "1d": 365}   # about one year before the first model
BAR_SECONDS = {"4h": 4 * 3600, "1d": 86400}


def _check(family):
    if family not in FAMILIES:
        raise ValueError(f"Unknown ML family '{family}'. Known: {sorted(FAMILIES)}")
    return FAMILIES[family]


def canonical_params(family, overrides=None):
    spec = _check(family)
    params = json.loads(json.dumps(spec["defaults"]))
    params.update(overrides or {})
    if params["timeframe"] not in BAR_SECONDS:
        raise ValueError("ML strategies support 4h and 1d only")
    if not 0.5 <= params["threshold"] < 1.0:
        raise ValueError("threshold must be in [0.5, 1)")
    if params["horizon"] < 1 or params["window_days"] < 0:
        raise ValueError("invalid horizon or window")
    if params["features"] not in ("basic", "extended"):
        raise ValueError("features must be 'basic' or 'extended'")
    if bool(params["leak"]) != bool(spec.get("control")):
        raise ValueError("the leak option is reserved for the control family")
    return params


def class_name(family, params):
    return "ML_" + hashlib.sha256(json.dumps({"t": family, "p": params}, sort_keys=True).encode()).hexdigest()[:10]


def generate(family, overrides=None):
    """(name, source_text, params). The text is a canonical description, deliberately free of code."""
    params = canonical_params(family, overrides)
    name = class_name(family, params)
    source = (f"# ML strategy {name}\n# family: {family}\n# engine: jarvis_trading.mlrunner (walk-forward HistGradientBoosting)\n"
              f"config = {json.dumps(params, sort_keys=True)}\nmodel = {json.dumps(MODEL_PARAMS, sort_keys=True)}\n"
              f"retrain_bars = {RETRAIN_BARS[params['timeframe']]}\nmin_train_bars = {MIN_TRAIN_BARS[params['timeframe']]}\n")
    return name, source, params


def hypothesis(family):
    return _check(family)["hypothesis"]


def neighbours(params):
    """Nearby configurations: one setting nudged to an adjacent sensible value (thresholds are not scaled by 25%)."""
    out = []
    for delta in (-0.05, 0.05):
        t = round(params["threshold"] + delta, 3)
        if 0.5 <= t < 1.0:
            out.append(dict(params, threshold=t))
    for factor in (0.5, 2.0):
        h = max(1, int(round(params["horizon"] * factor)))
        if h != params["horizon"]:
            out.append(dict(params, horizon=h))
    for delta in (-0.02, 0.02):
        s = round(params["stoploss"] + delta, 3)
        if s > 0:
            out.append(dict(params, stoploss=s))
    return out


def sample(family, n, seed=1, exclude=()):
    spec = _check(family)
    keys = sorted(spec["space"])
    rng = random.Random(f"{family}:{seed}")
    seen, out, attempts = set(exclude), [], 0
    while len(out) < n and attempts < max(500, n * 50):
        attempts += 1
        try:
            params = canonical_params(family, {k: rng.choice(spec["space"][k]) for k in keys})
        except ValueError:
            continue
        key = json.dumps(params, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(params)
    return out
