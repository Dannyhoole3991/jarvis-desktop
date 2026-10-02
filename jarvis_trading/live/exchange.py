"""
Independent exchange client for the live subsystem. Freqtrade does the day-to-day order work;
this client exists so the watchdog does not depend on Freqtrade for safety-critical facts:

  - is the price feed fresh?  (ticker timestamp)
  - what does the account really hold? (reconciliation)
  - what permissions does the API key have? (withdrawals must be DISABLED; spot only)
  - can I flatten everything if Freqtrade is hung? (emergency path)

`SimulatedExchange` implements the same interface in memory for tests and rehearsal.
`CcxtExchange` talks to a real exchange (public calls need no key). No method here can withdraw funds.
"""

import time


class ExchangeError(Exception):
    pass


class ExchangeClient:
    name = "abstract"

    def fetch_ticker(self, pair):            # -> {"last": float, "timestamp": seconds}
        raise NotImplementedError

    def fetch_balance(self):                 # -> {asset: total}
        raise NotImplementedError

    def fetch_open_orders(self, pair=None):  # -> [{"id", "client_id", "pair", "side", "amount"}]
        raise NotImplementedError

    def cancel_all(self, pair):
        raise NotImplementedError

    def market_sell(self, pair, amount, client_id):
        raise NotImplementedError

    def api_restrictions(self):              # -> dict or None when the exchange cannot report it
        raise NotImplementedError

    def market_limits(self, pair):           # -> {"min_cost", "min_amount", "taker_fee"}
        raise NotImplementedError


class SimulatedExchange(ExchangeClient):
    name = "simulated"

    def __init__(self, prices=None, balances=None, restrictions=None, clock=time.time):
        self.prices = dict(prices or {"BTC/USDT": 60000.0, "ETH/USDT": 3000.0})
        self.balances = dict(balances or {"USDT": 100.0})
        self.restrictions = restrictions if restrictions is not None else {
            "enable_withdrawals": False, "enable_spot_trading": True, "enable_margin": False, "enable_futures": False, "ip_restricted": True}
        self.clock = clock
        self.ticker_ts = None
        self.open_orders = []
        self.sold = []
        self.fail = False
        self.min_cost = 5.0

    def _check(self):
        if self.fail:
            raise ExchangeError("simulated exchange outage")

    def fetch_ticker(self, pair):
        self._check()
        return {"last": self.prices[pair], "timestamp": self.ticker_ts if self.ticker_ts is not None else self.clock()}

    def fetch_balance(self):
        self._check()
        return dict(self.balances)

    def fetch_open_orders(self, pair=None):
        self._check()
        return [o for o in self.open_orders if pair is None or o["pair"] == pair]

    def cancel_all(self, pair):
        self._check()
        self.open_orders = [o for o in self.open_orders if o["pair"] != pair]

    def market_sell(self, pair, amount, client_id):
        self._check()
        if any(s["client_id"] == client_id for s in self.sold):
            raise ExchangeError(f"duplicate client order id {client_id}")
        base = pair.split("/")[0]
        if self.balances.get(base, 0.0) + 1e-12 < amount:
            raise ExchangeError("insufficient balance (would be a short)")
        self.balances[base] = self.balances.get(base, 0.0) - amount
        self.balances["USDT"] = self.balances.get("USDT", 0.0) + amount * self.prices[pair]
        self.sold.append({"pair": pair, "amount": amount, "client_id": client_id})
        return {"id": f"sim-{len(self.sold)}", "pair": pair, "amount": amount}

    def api_restrictions(self):
        self._check()
        return dict(self.restrictions) if self.restrictions is not None else None

    def market_limits(self, pair):
        return {"min_cost": self.min_cost, "min_amount": 0.0, "taker_fee": 0.001}


class CcxtExchange(ExchangeClient):
    """Real exchange through ccxt. Public methods work without credentials."""

    def __init__(self, exchange_id="binance", key=None, secret=None):
        import ccxt  # installed in the Freqtrade venv
        self.name = exchange_id
        params = {"enableRateLimit": True, "options": {"defaultType": "spot"}}
        if key and secret:
            params.update({"apiKey": key, "secret": secret})
        self.x = getattr(ccxt, exchange_id)(params)
        self._markets = None

    def _wrap(self, fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception as error:  # ccxt raises many types; callers treat all of them as "the exchange failed"
            raise ExchangeError(f"{type(error).__name__}: {str(error)[:200]}") from None

    def markets(self):
        if self._markets is None:
            self._markets = self._wrap(self.x.load_markets)
        return self._markets

    def fetch_ticker(self, pair):
        t = self._wrap(self.x.fetch_ticker, pair)
        return {"last": float(t["last"]), "timestamp": (t.get("timestamp") or time.time() * 1000) / 1000.0}

    def fetch_balance(self):
        b = self._wrap(self.x.fetch_balance)
        return {asset: float(v) for asset, v in b.get("total", {}).items() if v}

    def fetch_open_orders(self, pair=None):
        orders = self._wrap(self.x.fetch_open_orders, pair) if pair else self._wrap(self.x.fetch_open_orders)
        return [{"id": o["id"], "client_id": o.get("clientOrderId"), "pair": o["symbol"], "side": o["side"], "amount": o["amount"]} for o in orders]

    def cancel_all(self, pair):
        self._wrap(self.x.cancel_all_orders, pair)

    def market_sell(self, pair, amount, client_id):
        o = self._wrap(self.x.create_order, pair, "market", "sell", amount, None, {"newClientOrderId": client_id})
        return {"id": o["id"], "pair": pair, "amount": amount}

    def api_restrictions(self):
        if self.name != "binance":
            return None  # cannot be verified for this exchange, so live mode refuses to start
        r = self._wrap(self.x.sapiGetAccountApiRestrictions)
        truthy = lambda v: str(v).lower() == "true"
        return {"enable_withdrawals": truthy(r.get("enableWithdrawals")), "enable_spot_trading": truthy(r.get("enableSpotAndMarginTrading")),
                "enable_margin": truthy(r.get("enableMargin")), "enable_futures": truthy(r.get("enableFutures")),
                "ip_restricted": truthy(r.get("ipRestrict"))}

    def market_limits(self, pair):
        m = self.markets()[pair]
        lim = m.get("limits", {})
        return {"min_cost": float((lim.get("cost") or {}).get("min") or 0.0), "min_amount": float((lim.get("amount") or {}).get("min") or 0.0),
                "taker_fee": float(m.get("taker") or 0.001)}


def check_account_safety(client, require_ip_restriction=True):
    """
    Live-mode precondition. Returns a list of problems (empty = safe to proceed). Fails CLOSED: if the exchange
    cannot report the key's permissions, that is a problem.
    """
    try:
        r = client.api_restrictions()
    except ExchangeError as error:
        return [f"could not read the API key's permissions ({error})"]
    if r is None:
        return ["this exchange cannot report API key permissions, so withdrawal protection cannot be verified"]
    problems = []
    if r.get("enable_withdrawals"):
        problems.append("the API key can WITHDRAW funds; create a key with withdrawals disabled")
    if r.get("enable_margin"):
        problems.append("the API key has margin trading enabled")
    if r.get("enable_futures"):
        problems.append("the API key has futures trading enabled")
    if not r.get("enable_spot_trading"):
        problems.append("the API key does not have spot trading enabled")
    if require_ip_restriction and not r.get("ip_restricted"):
        problems.append("the API key is not restricted to specific IP addresses")
    return problems
