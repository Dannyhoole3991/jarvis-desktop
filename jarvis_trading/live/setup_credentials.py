"""
Run this yourself in a terminal to store exchange API credentials securely:

    trading_lab\\ft_venv\\Scripts\\python.exe -m jarvis_trading.live.setup_credentials

Before you do, create the API key at the exchange with SPOT TRADING ONLY, WITHDRAWALS DISABLED and an IP
restriction to this PC. Nothing is echoed or logged. Credentials are encrypted for your Windows user and stored
outside the project folder.
"""

import getpass
import sys

from . import creds


def main():
    print("Store exchange credentials (encrypted to this Windows user, outside the project folder).")
    print("The key must be: spot trading only, withdrawals DISABLED, IP-restricted. Leave blank to cancel.")
    exchange = input("Exchange id [binance]: ").strip() or "binance"
    key = getpass.getpass("API key: ").strip()
    if not key:
        print("Cancelled.")
        return 1
    secret = getpass.getpass("API secret: ").strip()
    if not secret:
        print("Cancelled.")
        return 1
    path = creds.store_credentials(key, secret, exchange)
    print(f"Stored (encrypted) at {path}. Live trading is still DISABLED until you arm it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
