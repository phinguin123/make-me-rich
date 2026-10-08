"""Toss realtime WebSocket (wss://openapi-ws.tossinvest.com/ws/v1).

Declarative subscriptions: every send replaces the whole subscription set. This
class keeps the desired set and re-declares on change and after every reconnect.
Trade/orderbook frames are LOSSY (sampled, ~1s timestamps); order events are
lossless only within one connection, so callers must re-sync orders via REST
after `on_reconnect`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime
from typing import Awaitable, Callable

import websockets

from .client import TossClient

LOG = logging.getLogger(__name__)
WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
MAX_TOPICS = 100

TradeCb = Callable[[str, float, float, datetime], None]
BookCb = Callable[[str, float, float, float, float, datetime], None]
OrderCb = Callable[[str, dict], Awaitable[None] | None]


class TossStream:
    def __init__(self, client: TossClient, account_seq: str):
        self.client = client
        self.account_seq = account_seq
        self.trade_symbols: set[str] = set()
        self.book_symbols: set[str] = set()
        self.on_trade: TradeCb | None = None
        self.on_book: BookCb | None = None
        self.on_order: OrderCb | None = None
        self.on_reconnect: Callable[[], Awaitable[None]] | None = None
        self.connected = False
        self.rejected: set[str] = set()
        self._ws = None
        self._dirty = asyncio.Event()
        self._stop = asyncio.Event()

    # ── subscriptions ─────────────────────────────────────────────────────
    def set_symbols(self, trades: set[str], books: set[str]) -> None:
        trades = {s for s in trades if f"trade:us:{s}" not in self.rejected}
        books = {s for s in books if f"orderbook:us:{s}" not in self.rejected}
        budget = MAX_TOPICS - 1  # personal:order uses one slot
        books = set(sorted(books)[: budget // 2])
        trades = set(sorted(trades)[: budget - len(books)])
        if trades != self.trade_symbols or books != self.book_symbols:
            self.trade_symbols, self.book_symbols = trades, books
            self._dirty.set()

    def _declaration(self) -> list[dict]:
        decl: list[dict] = [{"id": f"decl-{random.randint(0, 1_000_000)}"}]
        if self.trade_symbols:
            decl.append({"type": "trade:us", "codes": sorted(self.trade_symbols)})
        if self.book_symbols:
            decl.append({"type": "orderbook:us", "codes": sorted(self.book_symbols)})
        decl.append({"type": "personal:order", "codes": [self.account_seq]})
        return decl

    # ── lifecycle ─────────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop.set()
        self._dirty.set()

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                token = await self.client.token()
                async with websockets.connect(
                    WS_URL,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    ping_interval=None,
                    max_queue=4096,
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    backoff = 1.0
                    LOG.info("Toss WS connected")
                    await ws.send(json.dumps(self._declaration()))
                    if self.on_reconnect:
                        asyncio.create_task(self.on_reconnect())
                    tasks = [
                        asyncio.create_task(self._reader(ws)),
                        asyncio.create_task(self._pinger(ws)),
                        asyncio.create_task(self._declarer(ws)),
                    ]
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    for t in done:
                        if t.exception():
                            raise t.exception()  # type: ignore[misc]
            except websockets.InvalidStatus as exc:
                status = exc.response.status_code
                LOG.error("Toss WS handshake rejected: HTTP %s", status)
                if status == 401:
                    await self.client.token(force=True)
            except Exception as exc:  # noqa: BLE001 — reconnect on anything
                if not self._stop.is_set():
                    LOG.warning("Toss WS disconnected: %s", exc)
            finally:
                self.connected = False
                self._ws = None
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 30.0)

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(60)
            await ws.send("PING")

    async def _declarer(self, ws) -> None:
        while not self._stop.is_set():
            await self._dirty.wait()
            self._dirty.clear()
            if self._stop.is_set():
                await ws.close()
                return
            await asyncio.sleep(0.3)  # coalesce bursts; limit is 5 declarations/s
            await ws.send(json.dumps(self._declaration()))

    async def _reader(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("type")
            if kind == "message":
                self._dispatch(msg.get("topic", ""), msg.get("data") or {})
            elif kind == "subscriptions":
                for item in msg.get("rejected") or []:
                    topic = item.get("topic") or item.get("key") or str(item)
                    self.rejected.add(topic)
                    LOG.warning("Toss WS rejected %s", item)
            elif kind == "error":
                err = msg.get("error") or {}
                LOG.warning("Toss WS error %s: %s", err.get("code"), err.get("message"))
                if err.get("code") == "server-shutdown":
                    return
                if err.get("code") == "rate-limit-exceeded":
                    await asyncio.sleep(1.0)
                    self._dirty.set()

    def _dispatch(self, topic: str, data: dict) -> None:
        parts = topic.split(":")
        if len(parts) < 3:
            return
        channel, symbol = parts[0], parts[-1]
        try:
            if channel == "trade" and self.on_trade:
                ts = datetime.fromisoformat(data["timestamp"])
                self.on_trade(symbol, float(data["price"]), float(data["volume"]), ts)
            elif channel == "orderbook" and self.on_book:
                asks, bids = data.get("asks") or [], data.get("bids") or []
                if asks and bids:
                    ts = datetime.fromisoformat(data["timestamp"]) if data.get("timestamp") else datetime.now().astimezone()
                    self.on_book(
                        symbol, float(bids[0]["price"]), float(bids[0]["volume"]),
                        float(asks[0]["price"]), float(asks[0]["volume"]), ts,
                    )
            elif channel == "personal" and self.on_order:
                res = self.on_order(data.get("event", ""), data.get("order") or {})
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
        except (KeyError, ValueError, TypeError) as exc:
            LOG.debug("bad frame %s: %s", topic, exc)
