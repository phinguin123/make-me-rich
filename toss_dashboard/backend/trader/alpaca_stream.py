"""Alpaca real-time market data stream (Algo Trader Plus: full-market SIP, unlimited symbols).

Every trade and the consolidated NBBO for each subscribed symbol, unlike Toss's sampled
(~1 s) WebSocket frames. This is the same tape the research used, so live signals see
what the backtests saw. Orders still go through Toss only.

Alpaca allows ONE stream connection per account: run the engine in one place only, and
don't open another stream (for example from a research script) while it is running.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime
from typing import Callable

import websockets

LOG = logging.getLogger(__name__)
URL = "wss://stream.data.alpaca.markets/v2/{feed}"

# Trades that don't set the consolidated last-sale price (odd lots, out-of-sequence, average-price,
# cash/next-day settlement, prior-reference prints, official open/close re-reports ...).
NOT_LAST_SALE = {"I", "W", "C", "N", "R", "Z", "U", "4", "7", "B", "G", "H", "P", "M", "Q"}

TradeCb = Callable[[str, float, float, datetime], None]
QuoteCb = Callable[[str, float, float, float, float, datetime], None]


def parse_ts(raw: str) -> datetime:
    """RFC 3339 with nanoseconds ("2026-10-12T23:59:59.848347916Z") -> aware datetime."""
    raw = raw.replace("Z", "+00:00")
    m = re.match(r"^(.*?\.)(\d+)(.*)$", raw)
    if m:  # Python parses at most microseconds
        raw = m.group(1) + m.group(2)[:6].ljust(6, "0") + m.group(3)
    return datetime.fromisoformat(raw)


class AlpacaStream:
    def __init__(self, key: str, secret: str, feed: str = "sip"):
        self.key, self.secret, self.feed = key, secret, feed
        self.trade_symbols: set[str] = set()
        self.quote_symbols: set[str] = set()
        self._subscribed: dict[str, set[str]] = {"trades": set(), "quotes": set()}
        self.on_trade: TradeCb | None = None
        self.on_quote: QuoteCb | None = None
        self.connected = False
        self.error = ""  # last fatal error, shown on the dashboard
        self.messages = 0
        self._dirty = asyncio.Event()
        self._stop = asyncio.Event()

    # ── subscriptions ─────────────────────────────────────────────────────
    def set_symbols(self, trades: set[str], quotes: set[str]) -> None:
        if trades != self.trade_symbols or quotes != self.quote_symbols:
            self.trade_symbols, self.quote_symbols = set(trades), set(quotes)
            self._dirty.set()

    def _diff(self) -> list[dict]:
        want = {"trades": self.trade_symbols, "quotes": self.quote_symbols}
        add = {k: sorted(want[k] - self._subscribed[k]) for k in want}
        drop = {k: sorted(self._subscribed[k] - want[k]) for k in want}
        msgs = []
        if any(add.values()):
            msgs.append({"action": "subscribe", **{k: v for k, v in add.items() if v}})
        if any(drop.values()):
            msgs.append({"action": "unsubscribe", **{k: v for k, v in drop.items() if v}})
        return msgs

    # ── lifecycle ─────────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop.set()
        self._dirty.set()

    async def run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(URL.format(feed=self.feed), max_queue=8192, ping_interval=20) as ws:
                    await self._handshake(ws)
                    self._subscribed = {"trades": set(), "quotes": set()}
                    self.connected, self.error, backoff = True, "", 2.0
                    LOG.info("Alpaca %s stream connected", self.feed.upper())
                    self._dirty.set()
                    tasks = [asyncio.create_task(self._reader(ws)), asyncio.create_task(self._subscriber(ws))]
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    for t in done:
                        if t.exception():
                            raise t.exception()  # type: ignore[misc]
            except PermissionError as exc:  # bad keys / no subscription / connection limit: retry slowly
                self.error = str(exc)
                LOG.error("Alpaca stream: %s", exc)
                backoff = 60.0
            except Exception as exc:  # noqa: BLE001 — reconnect on anything
                if not self._stop.is_set():
                    LOG.warning("Alpaca stream disconnected: %s", exc)
            finally:
                self.connected = False
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60.0)

    async def _handshake(self, ws) -> None:
        await ws.recv()  # [{"T":"success","msg":"connected"}]
        await ws.send(json.dumps({"action": "auth", "key": self.key, "secret": self.secret}))
        for item in json.loads(await ws.recv()):
            if item.get("T") == "error":
                raise PermissionError(f"{item.get('code')} {item.get('msg')}")  # 402 auth, 406 limit, 409 plan

    async def _subscriber(self, ws) -> None:
        while not self._stop.is_set():
            await self._dirty.wait()
            self._dirty.clear()
            if self._stop.is_set():
                await ws.close()
                return
            await asyncio.sleep(0.2)  # coalesce bursts
            for msg in self._diff():
                await ws.send(json.dumps(msg))

    async def _reader(self, ws) -> None:
        async for raw in ws:
            try:
                items = json.loads(raw)
            except ValueError:
                continue
            for item in items if isinstance(items, list) else [items]:
                self.handle(item)

    def handle(self, m: dict) -> None:
        kind = m.get("T")
        if kind == "t":
            self.messages += 1
            if self.on_trade and not (set(m.get("c") or ()) & NOT_LAST_SALE):
                self.on_trade(m["S"], float(m["p"]), float(m.get("s") or 0), parse_ts(m["t"]))
        elif kind == "q":
            self.messages += 1
            if self.on_quote and m.get("bp") and m.get("ap"):
                self.on_quote(m["S"], float(m["bp"]), float(m.get("bs") or 0), float(m["ap"]), float(m.get("as") or 0), parse_ts(m["t"]))
        elif kind == "subscription":
            self._subscribed = {"trades": set(m.get("trades") or ()), "quotes": set(m.get("quotes") or ())}
        elif kind == "error":
            LOG.warning("Alpaca stream error %s: %s", m.get("code"), m.get("msg"))
