"""
The tradable universe, fixed in advance by a rule (not by looking at results):
the largest coins of early 2019 that still have a USDT spot pair on Binance.

Known limitation (survivorship bias): coins that were large in 2019 but have
since been delisted or collapsed (e.g. EOS no longer has a Binance USDT
pair) cannot be tested, so this universe is flattered relative to what a
trader standing in 2019 would have faced. That bias inflates results; it can
only make a false positive MORE likely, which is why the gates and the paper
stage must not be relaxed because of it.
"""

UNIVERSE = ["BTC/USDT", "ETH/USDT", "XRP/USDT", "BCH/USDT", "LTC/USDT", "BNB/USDT", "ADA/USDT", "XLM/USDT", "TRX/USDT"]
CORE = ["BTC/USDT", "ETH/USDT"]
