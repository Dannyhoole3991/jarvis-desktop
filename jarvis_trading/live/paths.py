"""File locations for the live subsystem. Everything lives under trading_lab/live (git-ignored)."""

import os

from .. import freqtrade_adapter as A

LIVE_DIR = os.path.join(A.LAB_DIR, "live")
BUNDLE_DIR = os.path.join(LIVE_DIR, "bundles")
USER_DIR = os.path.join(LIVE_DIR, "ft_user_data")
LOG_DIR = os.path.join(LIVE_DIR, "logs")

CONFIG_FILE = os.path.join(LIVE_DIR, "live_config.json")        # human-edited: capital, pairs, limit overrides
ARMED_FILE = os.path.join(LIVE_DIR, "ARMED.json")               # exists only while live trading is explicitly enabled
EMERGENCY_FILE = os.path.join(LIVE_DIR, "EMERGENCY_STOP")       # create this file to trigger an emergency stop
EMERGENCY_LATCH = os.path.join(LIVE_DIR, "EMERGENCY_LATCHED.json")  # set after an emergency stop; a human must clear it
HALT_FILE = os.path.join(LIVE_DIR, "RISK_HALT.json")            # persistent Risk Engine halt (drawdown etc.)
GATE_FILE = os.path.join(LIVE_DIR, "risk_gate.json")            # read by the frozen strategy before every entry
STATUS_FILE = os.path.join(LIVE_DIR, "status.json")             # written by the trader each loop for the HUD
PID_FILE = os.path.join(LIVE_DIR, "trader.pid")
STATE_FILE = os.path.join(LIVE_DIR, "state.json")               # peak equity, session baselines (restart recovery)
TRADES_LOG = os.path.join(LOG_DIR, "live_trades.jsonl")         # complete trade log (append-only)
EVENTS_LOG = os.path.join(LOG_DIR, "live_events.jsonl")         # vetoes, halts, API failures, reconciliations


def ensure_dirs():
    for d in (LIVE_DIR, BUNDLE_DIR, USER_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)
