"""Order routing. PaperBroker and TossBroker share one interface so paper trading
exercises the same strategy/engine code as live trading.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from .market import MarketData
from .toss.client import TossAPIError, TossClient

LOG = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
SEC_FEE = 0.0000206
DONE = {"FILLED", "CANCELED", "REJECTED", "REPLACED"}


@dataclass
class Order:
    id: str
    client_id: str
    symbol: str
    side: str
    qty: float
    limit: float | None
    tag: str
    status: str = "PENDING"
    filled_qty: float = 0.0
    avg_price: float = 0.0
    commission: float = 0.0
    created: datetime = field(default_factory=lambda: datetime.now(ET))
    error: str = ""

    @property
    def done(self) -> bool:
        return self.status in DONE

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["created"] = self.created.isoformat()
        return d


FillCb = Callable[[Order, float, float], None]  # order, fill qty delta, fill price


def client_order_id(symbol: str, tag: str) -> str:
    return f"tb-{symbol.replace('.', '_')}-{tag[:6]}-{uuid.uuid4().hex[:12]}"[:36]


class Broker:
    name = "base"

    def __init__(self, market: MarketData, commission: float):
        self.market = market
        self.commission = commission
        self.orders: dict[str, Order] = {}
        self.on_fill: FillCb | None = None
        self.on_update: Callable[[Order], None] | None = None

    async def submit(self, symbol: str, side: str, qty: float, limit: float | None, tag: str) -> Order:
        raise NotImplementedError

    async def cancel(self, order: Order) -> None:
        raise NotImplementedError

    async def replace(self, order: Order, limit: float) -> Order:
        raise NotImplementedError

    async def cash(self) -> float:
        raise NotImplementedError

    async def sync(self) -> None:
        """Reconcile order state with the broker (no-op for paper)."""

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self.orders.values() if not o.done and (symbol is None or o.symbol == symbol)]

    def _apply_fill(self, order: Order, cum_qty: float, avg_price: float, commission: float | None = None) -> None:
        delta = cum_qty - order.filled_qty
        if delta <= 1e-9:
            return
        # price of this increment, derived from cumulative average
        prev_notional = order.filled_qty * order.avg_price
        inc_price = (cum_qty * avg_price - prev_notional) / delta if delta else avg_price
        order.filled_qty, order.avg_price = cum_qty, avg_price
        if commission is not None:
            order.commission = commission
        else:
            fee = self.commission + (SEC_FEE if order.side == "SELL" else 0.0)
            order.commission += delta * inc_price * fee
        if self.on_fill:
            self.on_fill(order, delta, inc_price)


class PaperBroker(Broker):
    """Simulated fills against live Toss quotes. Buys fill at the ask, sells at the bid."""

    name = "paper"

    def __init__(self, market: MarketData, commission: float, capital: float):
        super().__init__(market, commission)
        self._cash = capital

    async def submit(self, symbol: str, side: str, qty: float, limit: float | None, tag: str) -> Order:
        o = Order(id=f"paper-{uuid.uuid4().hex[:10]}", client_id=client_order_id(symbol, tag), symbol=symbol, side=side, qty=qty, limit=limit, tag=tag)
        if side == "BUY":
            need = qty * (limit or self.market.get(symbol).ask or self.market.get(symbol).last) * (1 + self.commission)
            if need > self._cash + 1e-6:
                o.status, o.error = "REJECTED", "insufficient-buying-power"
                self.orders[o.id] = o
                return o
            self._cash -= need  # reserve
            o._reserved = need  # type: ignore[attr-defined]
        self.orders[o.id] = o
        self.try_fill(o)
        return o

    def try_fill(self, o: Order) -> None:
        if o.done:
            return
        s = self.market.get(o.symbol)
        if o.side == "BUY":
            px = s.ask or s.last
            if px and (o.limit is None or px <= o.limit):
                self._fill(o, px)
        else:
            px = s.bid or s.last
            if px and (o.limit is None or px >= o.limit):
                self._fill(o, px)

    def _fill(self, o: Order, px: float) -> None:
        notional = o.qty * px
        if o.side == "BUY":
            self._cash += getattr(o, "_reserved", 0.0) - notional * (1 + self.commission)
        else:
            self._cash += notional * (1 - self.commission - SEC_FEE)
        o.status = "FILLED"
        self._apply_fill(o, o.qty, px)
        if self.on_update:
            self.on_update(o)

    def on_quote(self) -> None:
        for o in self.open_orders():
            self.try_fill(o)

    async def cancel(self, order: Order) -> None:
        if order.done:
            return
        order.status = "CANCELED"
        if order.side == "BUY":
            self._cash += getattr(order, "_reserved", 0.0)
        if self.on_update:
            self.on_update(order)

    async def replace(self, order: Order, limit: float) -> Order:
        await self.cancel(order)
        return await self.submit(order.symbol, order.side, order.qty - order.filled_qty, limit, order.tag)

    async def cash(self) -> float:
        return self._cash


class TossBroker(Broker):
    """Real orders through the Toss Open API. Fills arrive via the personal:order stream."""

    name = "live"

    def __init__(self, market: MarketData, commission: float, client: TossClient):
        super().__init__(market, commission)
        self.client = client

    async def submit(self, symbol: str, side: str, qty: float, limit: float | None, tag: str) -> Order:
        cid = client_order_id(symbol, tag)
        o = Order(id="", client_id=cid, symbol=symbol, side=side, qty=qty, limit=limit, tag=tag)
        try:
            res = await self.client.create_order(symbol, side, qty, "LIMIT" if limit else "MARKET", limit, cid)
            o.id = res.get("orderId", "")
            LOG.info("LIVE %s %s x%s @ %s [%s] -> %s", side, symbol, qty, limit or "MKT", tag, o.id[:12])
        except TossAPIError as exc:
            o.status, o.error = "REJECTED", exc.code
            o.id = f"rejected-{cid}"
            LOG.error("Order rejected %s %s x%s: %s", side, symbol, qty, exc)
        self.orders[o.id] = o
        return o

    async def cancel(self, order: Order) -> None:
        if order.done or order.id.startswith("rejected"):
            return
        try:
            await self.client.cancel_order(order.id)
            order.status = "PENDING_CANCEL"
        except TossAPIError as exc:
            if exc.code in ("already-filled", "already-canceled", "already-rejected", "already-modified"):
                await self._refresh(order)
            else:
                LOG.error("Cancel failed %s: %s", order.id[:12], exc)

    async def replace(self, order: Order, limit: float) -> Order:
        """US modify changes price only and returns a new order id."""
        try:
            res = await self.client.modify_order(order.id, limit)
        except TossAPIError as exc:
            LOG.warning("Modify failed %s: %s", order.id[:12], exc)
            await self._refresh(order)
            return order
        new = Order(id=res["orderId"], client_id=order.client_id, symbol=order.symbol, side=order.side, qty=order.qty, limit=limit, tag=order.tag)
        new.filled_qty, new.avg_price, new.commission = order.filled_qty, order.avg_price, order.commission
        order.status = "REPLACED"
        self.orders[new.id] = new
        return new

    async def cash(self) -> float:
        return await self.client.buying_power("USD")

    def on_order_event(self, event: str, snap: dict) -> None:
        o = self.orders.get(snap.get("orderId", ""))
        if o is None:
            return  # not ours (manual app order)
        self._update_from_snapshot(o, snap)

    def _update_from_snapshot(self, o: Order, snap: dict) -> None:
        ex = snap.get("execution") or {}
        cum = float(ex.get("filledQuantity") or 0)
        avg = float(ex.get("averageFilledPrice") or 0)
        comm = float(ex["commission"]) if ex.get("commission") not in (None, "") else None
        if cum > o.filled_qty and avg:
            self._apply_fill(o, cum, avg, comm)
        o.status = snap.get("status", o.status)
        if self.on_update:
            self.on_update(o)

    async def _refresh(self, o: Order) -> None:
        try:
            self._update_from_snapshot(o, await self.client.order(o.id))
        except TossAPIError as exc:
            LOG.warning("Order refresh failed %s: %s", o.id[:12], exc)

    async def sync(self) -> None:
        for o in self.open_orders():
            if o.id and not o.id.startswith("rejected"):
                await self._refresh(o)
                await asyncio.sleep(0)
