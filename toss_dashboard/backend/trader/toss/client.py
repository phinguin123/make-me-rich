"""Async Toss Securities Open API client (REST).

Spec: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json

- One access token per client_id is valid at a time; issuing a new one revokes the
  previous token. The token is cached on disk so restarts reuse it instead of
  revoking it.
- Every API group has its own TPS limit; each call waits on its group's bucket.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from ..config import DATA_DIR, SETTINGS

LOG = logging.getLogger(__name__)
BASE_URL = "https://openapi.tossinvest.com"
KST = ZoneInfo("Asia/Seoul")
TOKEN_CACHE = DATA_DIR / ".toss_token.json"

# Documented TPS per group, used at ~80% to leave headroom.
GROUP_TPS = {
    "AUTH": 5, "ACCOUNT": 1, "ASSET": 5, "STOCK": 5, "STOCK_ALL": 1, "MARKET_INFO": 3,
    "MARKET_DATA": 15, "MARKET_DATA_CHART": 20, "RANKING": 5, "ORDER": 10,
    "ORDER_HISTORY": 5, "ORDER_INFO": 6, "CONDITIONAL_ORDER": 5, "CONDITIONAL_ORDER_HISTORY": 10,
}


class TossAPIError(Exception):
    def __init__(self, status: int, code: str, message: str, data: Any = None, request_id: str = ""):
        super().__init__(f"[{status} {code}] {message}")
        self.status, self.code, self.message, self.data, self.request_id = status, code, message, data, request_id


class TokenBucket:
    def __init__(self, rate: float):
        self.rate = rate
        self.tokens = rate
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


def us_price(value: float) -> str:
    """Format a US limit price: 2 decimals at >= $1, 4 below (Toss truncates the rest)."""
    q = Decimal("0.01") if value >= 1 else Decimal("0.0001")
    return str(Decimal(str(value)).quantize(q, rounding=ROUND_DOWN))


class TossClient:
    def __init__(self) -> None:
        self.account = SETTINGS.toss_account_seq
        self._token: str | None = None
        self._token_exp = 0.0
        self._token_lock = asyncio.Lock()
        self._buckets = {g: TokenBucket(max(1.0, tps * 0.8)) for g, tps in GROUP_TPS.items()}
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "TossClient":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def open(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    # ── auth ──────────────────────────────────────────────────────────────
    def _load_cached_token(self) -> None:
        try:
            cached = json.loads(TOKEN_CACHE.read_text())
            if cached.get("client_id") == SETTINGS.toss_client_id and cached["exp"] > time.time() + 300:
                self._token, self._token_exp = cached["token"], cached["exp"]
        except (OSError, ValueError, KeyError):
            pass

    async def _issue_token(self) -> None:
        assert self._session is not None
        await self._buckets["AUTH"].acquire()
        form = {
            "grant_type": "client_credentials",
            "client_id": SETTINGS.toss_client_id,
            "client_secret": SETTINGS.toss_client_secret,
        }
        async with self._session.post(f"{BASE_URL}/oauth2/token", data=form) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                raise TossAPIError(resp.status, body.get("error", "auth"), body.get("error_description", str(body)))
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600)) - 120
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps({"client_id": SETTINGS.toss_client_id, "token": self._token, "exp": self._token_exp}))
        TOKEN_CACHE.chmod(0o600)
        LOG.info("Toss access token issued (valid %.1fh)", (self._token_exp - time.time()) / 3600)

    async def token(self, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token is None:
                self._load_cached_token()
            if force or self._token is None or time.time() >= self._token_exp:
                await self._issue_token()
            return self._token  # type: ignore[return-value]

    # ── transport ─────────────────────────────────────────────────────────
    async def request(
        self,
        method: str,
        path: str,
        group: str,
        params: dict | None = None,
        body: dict | None = None,
        account: bool = False,
    ) -> Any:
        await self.open()
        assert self._session is not None
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(5):
            await self._buckets[group].acquire()
            headers = {"Authorization": f"Bearer {await self.token()}"}
            if account:
                headers["X-Tossinvest-Account"] = self.account
            if method != "GET":
                # Toss rejects bodiless POST/DELETE without a JSON content type (415).
                headers["Content-Type"] = "application/json"
                if body is None and method == "POST":
                    body = {}
            try:
                async with self._session.request(method, f"{BASE_URL}{path}", params=params, json=body, headers=headers) as resp:
                    payload = await resp.json(content_type=None) if resp.content_length != 0 else {}
                    if resp.status == 200:
                        return payload.get("result", payload) if isinstance(payload, dict) else payload
                    err = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
                    code = err.get("code", "")
                    if resp.status == 401 and code in ("expired-token", "token-revoked", "invalid-token"):
                        await self.token(force=True)
                        continue
                    if resp.status == 429:
                        wait = float(resp.headers.get("Retry-After") or 2 ** attempt) + random.random() * 0.3
                        LOG.warning("Toss 429 on %s, retry in %.1fs", path, wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status >= 500 and attempt < 4 and method == "GET":
                        await asyncio.sleep(1 + attempt)
                        continue
                    raise TossAPIError(resp.status, code or str(resp.status), err.get("message", str(payload)), err.get("data"), err.get("requestId", ""))
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Never blindly retry order creation without an idempotency key.
                if method != "GET" and not (body or {}).get("clientOrderId"):
                    raise
                LOG.warning("Toss %s %s network error (%s), retrying", method, path, exc)
                await asyncio.sleep(1 + attempt)
        raise TossAPIError(0, "retry-exhausted", f"{method} {path}")

    # ── market data ───────────────────────────────────────────────────────
    async def prices(self, symbols: list[str]) -> list[dict]:
        return await self.request("GET", "/api/v1/prices", "MARKET_DATA", {"symbols": ",".join(symbols)})

    async def orderbook(self, symbol: str) -> dict:
        return await self.request("GET", "/api/v1/orderbook", "MARKET_DATA", {"symbol": symbol})

    async def trades(self, symbol: str, count: int = 50) -> list[dict]:
        return await self.request("GET", "/api/v1/trades", "MARKET_DATA", {"symbol": symbol, "count": count})

    async def candles(self, symbol: str, interval: str = "1m", count: int = 200, before: str | None = None) -> dict:
        params = {"symbol": symbol, "interval": interval, "count": count, "before": before}
        return await self.request("GET", "/api/v1/candles", "MARKET_DATA_CHART", params)

    async def stocks(self, symbols: list[str]) -> list[dict]:
        return await self.request("GET", "/api/v1/stocks", "STOCK", {"symbols": ",".join(symbols)})

    async def stock_warnings(self, symbol: str) -> list[dict]:
        return await self.request("GET", f"/api/v1/stocks/{symbol}/warnings", "STOCK")

    async def rankings(self, kind: str, duration: str = "realtime", count: int = 100, market: str = "US") -> dict:
        params = {"type": kind, "marketCountry": market, "duration": duration, "count": count}
        return await self.request("GET", "/api/v1/rankings", "RANKING", params)

    async def us_calendar(self, day: str | None = None) -> dict:
        return await self.request("GET", "/api/v1/market-calendar/US", "MARKET_INFO", {"date": day})

    # ── account ───────────────────────────────────────────────────────────
    async def holdings(self) -> dict:
        return await self.request("GET", "/api/v1/holdings", "ASSET", account=True)

    async def buying_power(self, currency: str = "USD") -> float:
        res = await self.request("GET", "/api/v1/buying-power", "ORDER_INFO", {"currency": currency}, account=True)
        return float(res["cashBuyingPower"])

    async def sellable_quantity(self, symbol: str) -> float:
        res = await self.request("GET", "/api/v1/sellable-quantity", "ORDER_INFO", {"symbol": symbol}, account=True)
        return float(res["sellableQuantity"])

    async def commissions(self) -> list[dict]:
        return await self.request("GET", "/api/v1/commissions", "ORDER_INFO", account=True)

    # ── orders ────────────────────────────────────────────────────────────
    async def orders(self, status: str = "OPEN", symbol: str | None = None, limit: int | None = None) -> list[dict]:
        res = await self.request("GET", "/api/v1/orders", "ORDER_HISTORY", {"status": status, "symbol": symbol, "limit": limit}, account=True)
        return res.get("orders", [])

    async def order(self, order_id: str) -> dict:
        return await self.request("GET", f"/api/v1/orders/{order_id}", "ORDER_HISTORY", account=True)

    async def create_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "LIMIT",
        price: float | None = None,
        client_order_id: str | None = None,
        time_in_force: str = "DAY",
    ) -> dict:
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "quantity": str(quantity) if order_type == "MARKET" and side == "SELL" else str(int(quantity)),
            "timeInForce": time_in_force,
        }
        if order_type == "LIMIT":
            body["price"] = us_price(float(price))  # type: ignore[arg-type]
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return await self.request("POST", "/api/v1/orders", "ORDER", body=body, account=True)

    async def modify_order(self, order_id: str, price: float) -> dict:
        body = {"orderType": "LIMIT", "price": us_price(price)}
        return await self.request("POST", f"/api/v1/orders/{order_id}/modify", "ORDER", body=body, account=True)

    async def cancel_order(self, order_id: str) -> dict:
        return await self.request("POST", f"/api/v1/orders/{order_id}/cancel", "ORDER", account=True)

    # ── conditional (server-side) orders ──────────────────────────────────
    async def create_stop(self, symbol: str, quantity: int, trigger: float, limit: float, client_order_id: str) -> dict:
        """Server-side SINGLE SELL that fires when price touches `trigger`."""
        today = datetime.now(KST).date()
        body = {
            "symbol": symbol,
            "type": "SINGLE",
            "quantity": str(int(quantity)),
            "orderType": "LIMIT",
            "expireDate": (today + timedelta(days=7)).isoformat(),
            "clientOrderId": client_order_id,
            "first": {"orderSide": "SELL", "triggerPrice": us_price(trigger), "orderPrice": us_price(limit)},
        }
        return await self.request("POST", "/api/v1/conditional-orders", "CONDITIONAL_ORDER", body=body, account=True)

    async def cancel_conditional(self, conditional_id: str) -> Any:
        return await self.request("DELETE", f"/api/v1/conditional-orders/{conditional_id}", "CONDITIONAL_ORDER", account=True)

    async def conditional_orders(self, status: str = "OPEN") -> Any:
        return await self.request("GET", "/api/v1/conditional-orders", "CONDITIONAL_ORDER_HISTORY", {"status": status}, account=True)
