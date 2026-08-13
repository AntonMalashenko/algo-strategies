"""bot/bybit_exec.py — minimal signed Bybit v5 REST client for order execution.

Shared broker-I/O layer (kept in bot/, like the cTrader adapter). Used by
strategy runners to read the account and place orders. Only `requests` is
needed (no pybit). Signing per Bybit v5:
    sign = HMAC_SHA256(api_secret, timestamp + api_key + recv_window + payload)
where payload is the query string (GET) or the raw JSON body (POST).

SAFETY: `env` selects the base URL — "testnet" / "demo" / "mainnet". Placing
orders on **mainnet is refused** unless `allow_mainnet=True` is passed explicitly.
Keys come from configs/accounts.yml (BYBIT section, see bot/accounts_config.py)
first, falling back to BYBIT_API_KEY/BYBIT_API_SECRET/BYBIT_TESTNET in the repo
`.env` per field; never log secrets.

Pass `name=` (the yml entry's own `name:` label) whenever more than one BYBIT
row can exist for the same `username` — e.g. one person running a dedicated
sub-account per strategy. `username` alone cannot disambiguate those (see
bot/accounts_config.py's module docstring for the 2026-08-06 incident this
guards against: omitting `name` here made a strategy silently fall back to
unrelated `.env` mainnet keys instead of its own configured sub-account).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
from dataclasses import dataclass

import requests

from bot import accounts_config as _accounts

BASE_URLS = {
    "mainnet": "https://api.bybit.com",
    "testnet": "https://api-testnet.bybit.com",
    "demo": "https://api-demo.bybit.com",
}
RECV_WINDOW = "5000"
CATEGORY = "linear"


@dataclass
class Instrument:
    symbol: str
    qty_step: float
    min_qty: float
    tick_size: float


class BybitExec:
    def __init__(self, env: str | None = None, api_key: str | None = None,
                 api_secret: str | None = None, category: str = CATEGORY,
                 allow_mainnet: bool = False, timeout: int = 20,
                 username: str | None = None, name: str | None = None):
        yml = _accounts.bybit_creds(username, name)
        yml_testnet = yml.get("testnet")   # bool from accounts.yml, or None if unset/no entry
        yml_env = ("testnet" if yml_testnet else "mainnet") if yml_testnet is not None else None
        self.env = (env or os.getenv("BYBIT_ENV") or yml_env
                    or ("mainnet" if os.getenv("BYBIT_TESTNET", "true").lower() in ("0", "false", "no")
                        else "testnet")).lower()
        if self.env not in BASE_URLS:
            raise ValueError(f"BYBIT_ENV must be one of {list(BASE_URLS)}, got {self.env!r}")
        self.base = BASE_URLS[self.env]
        self.key = api_key or yml.get("api_key") or os.getenv("BYBIT_API_KEY", "")
        self.secret = api_secret or yml.get("api_secret") or os.getenv("BYBIT_API_SECRET", "")
        self.category = category
        self.allow_mainnet = allow_mainnet
        self.timeout = timeout

    # ---- signing ----------------------------------------------------------
    def _headers(self, payload: str) -> dict:
        ts = str(int(time.time() * 1000))
        sign = hmac.new(self.secret.encode(), (ts + self.key + RECV_WINDOW + payload).encode(),
                        hashlib.sha256).hexdigest()
        return {"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts,
                "X-BAPI-RECV-WINDOW": RECV_WINDOW, "X-BAPI-SIGN": sign}

    def _get(self, path: str, params: dict, signed: bool = True) -> dict:
        qs = urllib.parse.urlencode(params)
        headers = self._headers(qs) if signed else {}
        r = requests.get(f"{self.base}{path}?{qs}", headers=headers, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if j.get("retCode") not in (0, None):
            raise RuntimeError(f"Bybit GET {path} error {j.get('retCode')}: {j.get('retMsg')}")
        return j.get("result", {})

    def _post(self, path: str, body: dict) -> dict:
        raw = json.dumps(body)
        headers = self._headers(raw)
        headers["Content-Type"] = "application/json"
        r = requests.post(f"{self.base}{path}", data=raw, headers=headers, timeout=self.timeout)
        r.raise_for_status()
        j = r.json()
        if j.get("retCode") not in (0, None):
            raise RuntimeError(f"Bybit POST {path} error {j.get('retCode')}: {j.get('retMsg')}")
        return j.get("result", {})

    # ---- reads ------------------------------------------------------------
    def wallet_equity(self, coin: str = "USDT") -> float:
        res = self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        for acct in res.get("list", []):
            for c in acct.get("coin", []):
                if c.get("coin") == coin:
                    return float(c.get("equity") or c.get("walletBalance") or 0.0)
            eq = acct.get("totalEquity")
            if eq:
                return float(eq)
        return 0.0

    def positions(self) -> dict[str, float]:
        """Return {symbol: signed qty} (long +, short −) for all open linear positions."""
        res = self._get("/v5/position/list", {"category": self.category, "settleCoin": "USDT"})
        out: dict[str, float] = {}
        for p in res.get("list", []):
            sz = float(p.get("size") or 0.0)
            if sz == 0:
                continue
            out[p["symbol"]] = sz if p.get("side") == "Buy" else -sz
        return out

    def ticker_price(self, symbol: str) -> float:
        res = self._get("/v5/market/tickers", {"category": self.category, "symbol": symbol}, signed=False)
        return float(res["list"][0]["lastPrice"])

    def instrument(self, symbol: str) -> Instrument:
        res = self._get("/v5/market/instruments-info", {"category": self.category, "symbol": symbol}, signed=False)
        it = res["list"][0]
        lot = it["lotSizeFilter"]; pf = it["priceFilter"]
        return Instrument(symbol, float(lot["qtyStep"]), float(lot["minOrderQty"]), float(pf["tickSize"]))

    def recent_fill_price(self, symbol: str, order_id: str) -> float | None:
        """Average execution price for an order id (for slippage measurement)."""
        try:
            res = self._get("/v5/execution/list", {"category": self.category, "symbol": symbol, "limit": 50})
        except Exception:
            return None
        px, qty = 0.0, 0.0
        for e in res.get("list", []):
            if e.get("orderId") == order_id:
                q = float(e["execQty"]); px += float(e["execPrice"]) * q; qty += q
        return (px / qty) if qty > 0 else None

    # ---- writes -----------------------------------------------------------
    def place_market(self, symbol: str, side: str, qty: float) -> dict:
        if self.env == "mainnet" and not self.allow_mainnet:
            raise PermissionError("Refusing to place a MAINNET order (allow_mainnet=False).")
        body = {"category": self.category, "symbol": symbol, "side": side,
                "orderType": "Market", "qty": str(qty)}
        return self._post("/v5/order/create", body)

    def close_position(self, symbol: str, side: str) -> dict:
        """Close an entire open position in one shot, without the caller computing
        a quantity. Bybit v5: a Market order with qty="0" + reduceOnly=True +
        closeOnTrigger=True closes the full position regardless of its exact size
        (see https://bybit-exchange.github.io/docs/v5/order/create-order).

        Use this for full exits instead of sizing a closing order from the local
        position snapshot: computing "current qty" ourselves and rounding it via
        `s009_paper._floor_step` proved unsafe (IEEE-754 float division can
        under-round by exactly one qty_step, e.g. 24.2/0.1 == 241.99999999999997
        -> 24.1, leaving a stuck dust remainder — the 2026-08-09 ATOM incident,
        see decisions-log.md). Asking the exchange to close "everything" sidesteps
        that class of bug entirely.

        `side` is the CLOSING side — opposite of the held position ("Sell" to
        close a long, "Buy" to close a short), same convention callers already
        use for `place_market`.
        """
        if self.env == "mainnet" and not self.allow_mainnet:
            raise PermissionError("Refusing to place a MAINNET order (allow_mainnet=False).")
        body = {"category": self.category, "symbol": symbol, "side": side,
                "orderType": "Market", "qty": "0",
                "reduceOnly": True, "closeOnTrigger": True}
        return self._post("/v5/order/create", body)
