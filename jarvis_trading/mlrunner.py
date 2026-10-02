"""
Walk-forward machine-learning backtester for the ML strategy family.

Runs under the Freqtrade venv's Python (needs pandas, numpy, scikit-learn).
See strategies_ml.py for the leakage rules this module enforces. The pure
functions (features, causality check, walk-forward, simulation) take plain
DataFrames so they are fully unit-testable on synthetic data.
"""

import hashlib
import json
import os
import pickle
import re
import threading

os.environ.setdefault("OMP_NUM_THREADS", "2")  # parallel workers must not each grab every core

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from . import strategies_ml as SML  # noqa: E402

COST_PER_SIDE = 0.0015       # 0.10% fee + 0.05% slippage, same as the Freqtrade runs
MAX_OPEN = 5
STAKE = 100.0
LABEL_HURDLE = 2 * COST_PER_SIDE  # a "positive" label must at least beat round-trip costs
_LOCK = threading.Lock()
_MEM_CACHE = {}


# ------------------------------------------------------------------ features
def features(df, kind="basic", leak=False):
    """Causal feature table: row t uses only bars <= t. `leak` adds a deliberately forward-looking feature (control only)."""
    c = df["close"]
    r1 = c.pct_change()
    f = {f"ret_{n}": c.pct_change(n) for n in (1, 3, 6, 12, 24)}
    f["vol_20"] = r1.rolling(20).std()
    delta = c.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    f["rsi_14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    f["dist_sma_50"] = c / c.rolling(50).mean() - 1
    f["dist_sma_200"] = c / c.rolling(200).mean() - 1
    vol = df["volume"]
    f["volz_20"] = (vol - vol.rolling(20).mean()) / vol.rolling(20).std().replace(0, np.nan)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - c.shift()).abs(), (df["low"] - c.shift()).abs()], axis=1).max(axis=1)
    f["atr_pct"] = tr.rolling(14).mean() / c
    if kind == "extended":
        f["ret_72"] = c.pct_change(72)
        f["vol_ratio"] = r1.rolling(20).std() / r1.rolling(100).std().replace(0, np.nan)
        f["range_pct"] = (df["high"] - df["low"]) / c
        f["close_pos"] = (c - df["low"]) / (df["high"] - df["low"]).replace(0, np.nan)
    if leak:
        f["leak_next"] = c.shift(-1) / c - 1
    out = pd.DataFrame(f, index=df.index)
    return out.replace([np.inf, -np.inf], np.nan)


def load_frames(pairs, timeframe, data_dir):
    frames = {}
    for pair in pairs:
        path = os.path.join(data_dir, f"{pair.replace('/', '_')}-{timeframe}.feather")
        df = pd.read_feather(path)
        df["date"] = pd.to_datetime(df["date"], utc=True)
        frames[pair] = df.set_index("date")[["open", "high", "low", "close", "volume"]].astype(float)
    return frames


def check_causality(frames, params, cuts):
    """
    Truncation test. For each cut time, recompute the features from data cut at
    that time and require every feature at or before the cut to equal the value
    computed from the full data. Returns a list of human-readable violations.
    """
    problems = []
    kind, leak = params["features"], bool(params.get("leak"))
    for pair, df in frames.items():
        full = features(df, kind, leak)
        for cut in cuts:
            cut = pd.Timestamp(cut, tz="UTC") if not isinstance(cut, pd.Timestamp) else cut
            part_df = df.loc[df.index <= cut]
            if len(part_df) < 250:
                continue
            part = features(part_df, kind, leak)
            ref = full.loc[part.index]
            both_nan = part.isna() & ref.isna()
            same = (np.isclose(part.to_numpy(dtype=float), ref.to_numpy(dtype=float), rtol=1e-9, atol=1e-12, equal_nan=True)) | both_nan.to_numpy()
            if not same.all():
                bad_cols = [c for c, ok in zip(part.columns, same.all(axis=0)) if not ok]
                problems.append(f"{pair}: features {bad_cols} at or before {cut:%Y-%m-%d} change when future data is removed")
                break
    return problems


# --------------------------------------------------------------- walk-forward
def build_dataset(frames, params):
    parts = []
    h = int(params["horizon"])
    for i, (pair, df) in enumerate(frames.items()):
        x = features(df, params["features"], bool(params.get("leak")))
        x["fwd_ret"] = df["close"].shift(-h) / df["close"] - 1  # label source: ONLY ever used on rows whose label is already known
        x["pair"] = pair
        x["pair_idx"] = i
        parts.append(x)
    data = pd.concat(parts)
    return data.sort_values(["pair_idx"], kind="stable").sort_index(kind="stable")


def walk_forward_predictions(frames, params, seed=1):
    """Returns DataFrame [ts, pair, prob]. Models are trained only on rows whose labels were already known."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    tf = params["timeframe"]
    bar = SML.BAR_SECONDS[tf]
    h = int(params["horizon"])
    data = build_dataset(frames, params)
    feat_cols = [c for c in data.columns if c not in ("fwd_ret", "pair", "pair_idx")]
    times = data.index.unique().sort_values()
    if len(times) <= SML.MIN_TRAIN_BARS[tf]:
        return pd.DataFrame(columns=["ts", "pair", "prob"])
    step = SML.RETRAIN_BARS[tf]
    out = []
    k = 0
    while True:
        i0 = SML.MIN_TRAIN_BARS[tf] + k * step
        if i0 >= len(times):
            break
        t_k = times[i0]
        t_next = times[min(i0 + step, len(times) - 1)] if i0 + step < len(times) else None
        cutoff = t_k - pd.Timedelta(seconds=h * bar)            # label of a row at t is known once t + h bars <= t_k
        train = data.loc[data.index <= cutoff]
        if params["window_days"] > 0:
            train = train.loc[train.index > t_k - pd.Timedelta(days=int(params["window_days"]))]
        train = train.dropna(subset=feat_cols + ["fwd_ret"])
        k += 1
        if len(train) < 500:
            continue
        y = (train["fwd_ret"] > LABEL_HURDLE).astype(int).to_numpy()
        if y.min() == y.max():
            continue
        clf = HistGradientBoostingClassifier(random_state=seed, early_stopping=False, **SML.MODEL_PARAMS)
        clf.fit(train[feat_cols].to_numpy(), y)
        window = data.loc[(data.index >= t_k) & ((data.index < t_next) if t_next is not None else True)].dropna(subset=feat_cols)
        if window.empty:
            continue
        proba = clf.predict_proba(window[feat_cols].to_numpy())[:, 1]
        out.append(pd.DataFrame({"ts": window.index, "pair": window["pair"].to_numpy(), "prob": proba}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["ts", "pair", "prob"])


# ----------------------------------------------------------------- simulation
def simulate(frames, preds, params, start_epoch=None, end_epoch=None, starting_balance=1000.0):
    """Long-only trades from predictions: enter at the NEXT bar's open, hold `horizon` bars or until the stop, costs on both sides."""
    h, thr, sl = int(params["horizon"]), float(params["threshold"]), float(params["stoploss"])
    arr = {p: {"idx": df.index, "o": df["open"].to_numpy(), "h": df["high"].to_numpy(), "l": df["low"].to_numpy()} for p, df in frames.items()}
    sig = preds[preds["prob"] >= thr].sort_values(["ts", "prob"], ascending=[True, False], kind="stable")
    open_positions = {}   # pair -> exit epoch
    trades = []
    pending = []          # (close_ts, profit_abs) of trades not yet realised at the time being considered
    for ts, pair, prob in zip(sig["ts"], sig["pair"], sig["prob"]):
        a = arr[pair]
        pos = a["idx"].get_loc(ts)
        j0 = pos + 1
        if j0 + 1 >= len(a["idx"]):
            continue
        entry_ts = a["idx"][j0].timestamp()
        if (start_epoch is not None and entry_ts < start_epoch) or (end_epoch is not None and entry_ts >= end_epoch):
            continue
        for p in [p for p, t in open_positions.items() if t <= entry_ts]:
            del open_positions[p]
        if pair in open_positions or len(open_positions) >= MAX_OPEN:
            continue
        # Like Freqtrade's dry-run wallet: no new trade unless the account actually has the stake free
        realised = sum(t["profit_abs"] for t in trades if t["close_ts"] <= entry_ts)
        if starting_balance + realised - STAKE * len(open_positions) < STAKE:
            continue
        entry = a["o"][j0]
        stop = entry * (1 - sl)
        exit_j, exit_price = min(j0 + h, len(a["idx"]) - 1), None
        for j in range(j0, min(j0 + h, len(a["idx"]) - 1)):
            if a["l"][j] <= stop:
                exit_j, exit_price = j, stop
                break
        if exit_price is None:
            exit_price = a["o"][exit_j]
        exit_ts = a["idx"][exit_j].timestamp()
        ratio = (exit_price / entry) * (1 - COST_PER_SIDE) / (1 + COST_PER_SIDE) - 1
        open_positions[pair] = exit_ts
        trades.append({"pair": pair, "open_ts": entry_ts, "close_ts": exit_ts, "profit_ratio": float(ratio), "profit_abs": float(ratio * STAKE),
                       "stake": STAKE, "fee_paid": STAKE * COST_PER_SIDE * 2, "exit_reason": "stoploss" if exit_price == stop else "horizon", "tag": None})
    trades.sort(key=lambda t: t["close_ts"])
    return trades


# --------------------------------------------------------------------- runner
def _parse_range(timerange):
    a, b = timerange.split("-")
    ts = lambda s: pd.Timestamp(f"{s[:4]}-{s[4:6]}-{s[6:8]}", tz="UTC").timestamp()
    return ts(a), ts(b) + 86400.0


class MLRunner:
    """Same interface as the Freqtrade adapter, for ML strategies."""

    def __init__(self, data_dir=None, cache_dir=None, seed=1):
        from . import freqtrade_adapter as A
        self.data_dir = data_dir or A.DATA_DIR
        self.cache_dir = cache_dir or os.path.join(A.LAB_DIR, "ml_cache")
        self.seed = seed
        self._configs = {}
        self._frames = {}

    def tool_versions(self):
        import sklearn
        return {"engine": "jarvis_trading.mlrunner", "scikit-learn": sklearn.__version__, "pandas": pd.__version__}

    def data_hash(self, pairs, timeframe):
        from . import freqtrade_adapter as A
        return A.data_hash(pairs, timeframe)

    def write_strategy(self, name, source):
        match = re.search(r"^config = (\{.*\})$", source, re.M)
        if not match:
            raise ValueError("not an ML strategy description")
        self._configs[name] = json.loads(match.group(1))

    def _frames_for(self, pairs, timeframe):
        key = (tuple(pairs), timeframe)
        if key not in self._frames:
            self._frames[key] = load_frames(pairs, timeframe, self.data_dir)
        return self._frames[key]

    def _predictions(self, params, pairs):
        model_key = {k: params[k] for k in ("timeframe", "horizon", "window_days", "features", "leak")}
        digest = hashlib.sha256(json.dumps({"m": model_key, "pairs": list(pairs), "seed": self.seed, "model": SML.MODEL_PARAMS,
                                            "data": self._safe_hash(pairs, params["timeframe"])}, sort_keys=True).encode()).hexdigest()[:20]
        with _LOCK:
            if digest in _MEM_CACHE:
                return _MEM_CACHE[digest]
        path = os.path.join(self.cache_dir, digest + ".pkl")
        if os.path.exists(path):
            with open(path, "rb") as f:
                preds = pickle.load(f)
        else:
            preds = walk_forward_predictions(self._frames_for(pairs, params["timeframe"]), params, self.seed)
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = path + f".{os.getpid()}.tmp"
            with open(tmp, "wb") as f:
                pickle.dump(preds, f)
            os.replace(tmp, path)
        with _LOCK:
            _MEM_CACHE[digest] = preds
        return preds

    def _safe_hash(self, pairs, timeframe):
        try:
            return self.data_hash(pairs, timeframe)
        except Exception:
            return None

    def run_backtest(self, name, timerange, timeframe, pairs=None, **kwargs):
        from . import universe as U
        pairs = pairs or U.UNIVERSE
        params = self._configs[name]
        a, b = _parse_range(timerange)
        preds = self._predictions(params, pairs)
        trades = simulate(self._frames_for(pairs, timeframe), preds, params, a, b)
        return {"trades": trades, "command": f"mlrunner walk-forward {json.dumps(params, sort_keys=True)} {timerange}",
                "per_side_cost": COST_PER_SIDE, "starting_balance": 1000.0, "result_file": None}

    def run_lookahead_analysis(self, name, timerange, timeframe, pairs=None, minimum_trades=10):
        from . import universe as U
        pairs = pairs or U.UNIVERSE
        params = self._configs[name]
        frames = self._frames_for(pairs, timeframe)
        a, b = _parse_range(timerange)
        cuts = [pd.Timestamp(a + (b - a) * q, unit="s", tz="UTC") for q in (0.25, 0.5, 0.75)]
        problems = check_causality(frames, params, cuts)
        return {"clean": not problems, "raw": "feature causality truncation test: " + ("; ".join(problems) if problems else "no violations"),
                "exit_code": 0}
