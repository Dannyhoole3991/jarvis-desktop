"""
Live configuration: the human-edited settings file and the Freqtrade config built from it.

The Freqtrade config written to disk NEVER contains the exchange key or secret; they are passed to the child
process through environment variables only. `assert_safe_live_config` is the live counterpart of the research
`assert_safe_config`: spot only, no leverage, no shorting, API on localhost, no embedded credentials, dry-run
exactly matching the mode (rehearsal = dry-run, live = real), and capital/stake inside the limits.
"""

import dataclasses
import json
import os
import secrets as _stdlib_secrets

from .. import freqtrade_adapter as A
from . import limits as L
from . import paths

DEFAULT_SETTINGS = {
    "exchange": "binance",
    "capital_limit": 0.0,                 # USDT the strategy may use; 0 means live trading cannot place anything
    "limits": {},                         # overrides of LiveLimits fields (subject to hard ceilings)
}


class LiveConfigError(Exception):
    pass


def load_settings(path=None):
    path = path or paths.CONFIG_FILE
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        unknown = set(user) - set(DEFAULT_SETTINGS)
        if unknown:
            raise LiveConfigError(f"Unknown settings: {sorted(unknown)}")
        settings.update(user)
    return settings


def save_settings(settings, path=None):
    paths.ensure_dirs()
    build_limits(settings)  # validates (hard ceilings) before anything is written
    with open(path or paths.CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, sort_keys=True)


def build_limits(settings):
    fields = {f.name for f in dataclasses.fields(L.LiveLimits)}
    overrides = dict(settings.get("limits") or {})
    unknown = set(overrides) - fields
    if unknown:
        raise LiveConfigError(f"Unknown limit settings: {sorted(unknown)}")
    if "allowed_pairs" in overrides:
        overrides["allowed_pairs"] = tuple(overrides["allowed_pairs"])
    overrides["capital_limit"] = float(settings.get("capital_limit") or 0.0)
    return L.LiveLimits(**overrides)


def stake_per_trade(limits):
    """Fixed stake per trade: the per-position share of capital, never below the exchange minimum."""
    return max(limits.capital_limit * limits.max_position_pct, 0.0)


def build_freqtrade_config(limits, timeframe, rehearsal, db_url, api_port, api_user, api_password, exchange="binance"):
    capital = limits.capital_limit
    stake = stake_per_trade(limits)
    cfg = {
        "max_open_trades": limits.max_open_positions, "stake_currency": "USDT", "stake_amount": round(stake, 2),
        "tradable_balance_ratio": 1.0, "available_capital": capital, "dry_run": bool(rehearsal),
        "trading_mode": "spot", "timeframe": timeframe, "initial_state": "running", "force_entry_enable": False,
        "cancel_open_orders_on_exit": False, "process_throttle_secs": 5, "db_url": db_url, "bot_name": "jarvis-live",
        "unfilledtimeout": {"entry": 10, "exit": 10, "unit": "minutes"},
        "order_types": {"entry": "limit", "exit": "limit", "stoploss": "market", "stoploss_on_exchange": True,
                        "stoploss_on_exchange_interval": 60},
        "exchange": {"name": exchange, "key": "", "secret": "", "pair_whitelist": list(limits.allowed_pairs), "pair_blacklist": []},
        "entry_pricing": {"price_side": "same", "use_order_book": False},
        "exit_pricing": {"price_side": "same", "use_order_book": False},
        "pairlists": [{"method": "StaticPairList"}],
        "api_server": {"enabled": True, "listen_ip_address": "127.0.0.1", "listen_port": api_port, "verbosity": "error",
                       "enable_openapi": False, "jwt_secret_key": _stdlib_secrets.token_hex(24), "CORS_origins": [],
                       "username": api_user, "password": api_password},
    }
    if rehearsal:
        cfg["dry_run_wallet"] = capital
    assert_safe_live_config(cfg, limits, rehearsal)
    return cfg


def assert_safe_live_config(cfg, limits, rehearsal):
    problems = []
    if bool(cfg.get("dry_run")) != bool(rehearsal):
        problems.append("dry_run must be True in rehearsal and False in live mode, and never anything else")
    if cfg.get("trading_mode", "spot") != "spot":
        problems.append("only spot trading is allowed")
    if cfg.get("leverage") or cfg.get("margin_mode"):
        problems.append("leverage / margin is not allowed")
    ex = cfg.get("exchange", {})
    for key in ("key", "secret", "password", "uid"):
        if ex.get(key):
            problems.append(f"exchange.{key} must not be written to the config file (credentials go through the environment)")
    api = cfg.get("api_server", {})
    if api.get("listen_ip_address") != "127.0.0.1":
        problems.append("the API server may only listen on 127.0.0.1")
    if cfg.get("force_entry_enable"):
        problems.append("force-entry must be disabled")
    if cfg.get("max_open_trades", 0) > limits.max_open_positions:
        problems.append("max_open_trades exceeds the live limits")
    if limits.capital_limit <= 0:
        problems.append("a capital limit must be set")
    if cfg.get("stake_amount", 0) > limits.capital_limit * limits.max_position_pct + 1e-9:
        problems.append("stake_amount exceeds the per-position limit")
    if set(ex.get("pair_whitelist", [])) - set(limits.allowed_pairs):
        problems.append("whitelist contains pairs the live limits do not allow")
    if not cfg.get("order_types", {}).get("stoploss_on_exchange"):
        problems.append("stoploss_on_exchange must be on so positions stay protected if this PC goes offline")
    if problems:
        raise LiveConfigError("; ".join(problems))
    return cfg


def write_config(cfg, folder=None):
    folder = folder or paths.LIVE_DIR
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "freqtrade_live_config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    return path
