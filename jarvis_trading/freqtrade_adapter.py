"""
Adapter between the Jarvis lab and the Freqtrade trading engine.

Freqtrade is GPL-3.0. It lives in its OWN virtualenv (trading_lab/ft_venv)
and is only ever run as a separate process through its command line; Jarvis
never imports it, so no GPL code is linked into Jarvis. Generated strategy
files import freqtrade (they must, to run inside it) and stay inside
trading_lab/ft_user_data.

Safety, enforced before every run by assert_safe_config():
  dry_run is True, no exchange API key or secret is present, spot only,
  no leverage. Nothing in this module can place a real order.

Freqtrade models fees but assumes ZERO slippage, so the lab passes
fee + slippage_assumption as the per-side cost (--fee is applied twice).
"""

import glob
import hashlib
import json
import os
import subprocess
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
LAB_DIR = os.path.join(os.path.dirname(HERE), "trading_lab")
FT_VENV_PY = os.path.join(LAB_DIR, "ft_venv", "Scripts", "python.exe")
FT_EXE = os.path.join(LAB_DIR, "ft_venv", "Scripts", "freqtrade.exe")
USER_DIR = os.path.join(LAB_DIR, "ft_user_data")
STRATEGY_DIR = os.path.join(USER_DIR, "strategies")
DATA_DIR = os.path.join(USER_DIR, "data", "binance")
RESULTS_DIR = os.path.join(USER_DIR, "backtest_results")

DEFAULT_PAIRS = ["BTC/USDT", "ETH/USDT"]
EXCHANGE_FEE = 0.001          # Binance spot taker fee, 0.10% per side
SLIPPAGE_ASSUMPTION = 0.0005  # 0.05% per side, a conservative liquid-pair estimate


class UnsafeConfig(Exception):
    pass


def build_config(pairs=None, timeframe="1h", starting_balance=1000.0, stake_amount=100.0, max_open_trades=2):
    pairs = pairs or DEFAULT_PAIRS
    return {
        "max_open_trades": max_open_trades, "stake_currency": "USDT", "stake_amount": stake_amount,
        "tradable_balance_ratio": 0.99, "dry_run": True, "dry_run_wallet": starting_balance,
        "trading_mode": "spot", "timeframe": timeframe, "cancel_open_orders_on_exit": False,
        "unfilledtimeout": {"entry": 10, "exit": 10, "unit": "minutes"},
        "exchange": {"name": "binance", "key": "", "secret": "", "pair_whitelist": pairs, "pair_blacklist": []},
        "entry_pricing": {"price_side": "same", "use_order_book": False},
        "exit_pricing": {"price_side": "same", "use_order_book": False},
        "pairlists": [{"method": "StaticPairList"}],
    }


def assert_safe_config(cfg):
    problems = []
    if cfg.get("dry_run") is not True:
        problems.append("dry_run must be True")
    ex = cfg.get("exchange", {})
    for key in ("key", "secret", "password", "uid"):
        if ex.get(key):
            problems.append(f"exchange.{key} must be empty - no real credentials are allowed")
    if cfg.get("trading_mode", "spot") != "spot":
        problems.append("only spot trading is allowed")
    if cfg.get("leverage") or cfg.get("margin_mode"):
        problems.append("leverage / margin is not allowed")
    if "api_server" in cfg and cfg["api_server"].get("listen_ip_address") not in (None, "127.0.0.1"):
        problems.append("api_server may only listen on localhost")
    if problems:
        raise UnsafeConfig("; ".join(problems))
    return cfg


def write_config(name, cfg):
    assert_safe_config(cfg)
    os.makedirs(USER_DIR, exist_ok=True)
    path = os.path.join(USER_DIR, f"{name}.json")
    text = json.dumps(cfg, indent=2, sort_keys=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def tool_versions():
    try:
        out = subprocess.run([FT_VENV_PY, "-c", "import freqtrade; print(freqtrade.__version__)"],
                             capture_output=True, text=True, timeout=60)
        return {"freqtrade": out.stdout.strip() or "unknown"}
    except Exception:
        return {"freqtrade": "unknown"}


def data_hash(pairs, timeframe):
    """Hash of the actual candle files an experiment used, so 'same data' is provable."""
    digest = hashlib.sha256()
    for pair in sorted(pairs):
        path = os.path.join(DATA_DIR, f"{pair.replace('/', '_')}-{timeframe}.feather")
        digest.update(path.encode())
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
    return digest.hexdigest()[:16]


def write_strategy(name, source):
    """Writes the strategy file; refuses to overwrite a file whose content differs."""
    os.makedirs(STRATEGY_DIR, exist_ok=True)
    path = os.path.join(STRATEGY_DIR, f"{name}.py")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            if f.read() != source:
                raise RuntimeError(f"Strategy file {name}.py already exists with different content; refusing to overwrite.")
        return path
    with open(path, "w", encoding="utf-8") as f:
        f.write(source)
    return path


def _run(args, timeout=1800):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=LAB_DIR)


def _latest_result_file():
    candidates = glob.glob(os.path.join(RESULTS_DIR, "backtest-result-*.zip"))
    return max(candidates, key=os.path.getmtime) if candidates else None


def _trades_from_zip(path, strategy_name):
    with zipfile.ZipFile(path) as z:
        json_name = next(n for n in z.namelist() if n.endswith(".json") and "_config" not in n and "meta" not in n)
        data = json.loads(z.read(json_name))
    strat = data["strategy"][strategy_name]
    trades = []
    for t in strat["trades"]:
        stake = float(t["stake_amount"])
        close_amount = float(t.get("close_rate", 0)) * float(t.get("amount", 0))
        fees = stake * float(t.get("fee_open", 0)) + close_amount * float(t.get("fee_close", 0))
        trades.append({
            "pair": t["pair"], "open_ts": t["open_timestamp"] / 1000.0, "close_ts": t["close_timestamp"] / 1000.0,
            "profit_ratio": float(t["profit_ratio"]), "profit_abs": float(t["profit_abs"]),
            "stake": stake, "fee_paid": fees, "exit_reason": t.get("exit_reason"),
        })
    return trades, strat


def run_backtest(strategy_name, timerange, timeframe, pairs=None, fee=EXCHANGE_FEE, slippage=SLIPPAGE_ASSUMPTION,
                 starting_balance=1000.0, stake_amount=100.0, max_open_trades=2):
    """
    Runs one Freqtrade backtest and returns {"trades": [...], "command": str, "per_side_cost": float, ...}.
    Per-side cost = fee + slippage (Freqtrade itself assumes zero slippage).
    """
    pairs = pairs or DEFAULT_PAIRS
    cfg_path = write_config("config_backtest", build_config(pairs, timeframe, starting_balance, stake_amount, max_open_trades))
    per_side = fee + slippage
    before = set(glob.glob(os.path.join(RESULTS_DIR, "backtest-result-*.zip")))
    args = [FT_EXE, "backtesting", "--config", cfg_path, "--userdir", USER_DIR, "--strategy", strategy_name,
            "--strategy-path", STRATEGY_DIR, "--timerange", timerange, "--timeframe", timeframe,
            "--fee", str(per_side), "--export", "trades", "--cache", "none", "--breakdown", "month"]
    result = _run(args)
    after = set(glob.glob(os.path.join(RESULTS_DIR, "backtest-result-*.zip")))
    new = sorted(after - before, key=os.path.getmtime)
    if result.returncode != 0 or not new:
        raise RuntimeError(f"Backtest failed (exit {result.returncode}): {(result.stdout + result.stderr)[-1500:]}")
    trades, strat = _trades_from_zip(new[-1], strategy_name)
    return {"trades": trades, "command": " ".join(str(a) for a in args), "per_side_cost": per_side,
            "result_file": os.path.basename(new[-1]), "starting_balance": starting_balance,
            "freqtrade_summary": {k: strat.get(k) for k in ("total_trades", "profit_total", "max_drawdown_account", "sharpe", "sortino", "calmar", "profit_factor")}}


def run_lookahead_analysis(strategy_name, timerange, timeframe, pairs=None, minimum_trades=10):
    """Returns {"clean": bool|None, "raw": str}. None means the tool could not give a verdict."""
    pairs = pairs or DEFAULT_PAIRS
    cfg_path = write_config("config_backtest", build_config(pairs, timeframe))
    args = [FT_EXE, "lookahead-analysis", "--config", cfg_path, "--userdir", USER_DIR, "--strategy", strategy_name,
            "--strategy-path", STRATEGY_DIR, "--timerange", timerange, "--timeframe", timeframe,
            "--minimum-trade-amount", str(minimum_trades), "--targeted-trade-amount", str(max(minimum_trades, 20))]
    result = _run(args, timeout=3600)
    text = result.stdout + result.stderr
    verdict = None  # None = the tool gave no verdict; callers must treat that as NOT clean
    for line in text.splitlines():
        if strategy_name not in line:
            continue
        lowered = line.lower()
        if "no bias detected" in lowered:
            verdict = True
        elif "bias detected" in lowered:
            verdict = False
            break  # any detection wins
    return {"clean": verdict, "raw": text[-2500:], "exit_code": result.returncode}
