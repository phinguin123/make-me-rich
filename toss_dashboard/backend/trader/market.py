"""Live per-symbol market state.

Prices come from the Toss WebSocket (sampled trade + top-of-book frames). Minute
bars with volume come from polling Toss 1-minute candles, which are complete,
unlike the sampled WebSocket tape.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from .toss.client import TossAPIError, TossClient

LOG = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
OPEN = time(9, 30)


@dataclass
class Bar:
    end: datetime  # bar end time (Toss convention), ET
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class SymbolState:
    symbol: str
    last: float = 0.0
    last_ts: datetime | None = None
    bid: float = 0.0
    ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    bars: dict[datetime, Bar] = field(default_factory=dict)  # today's regular-session bars by end time

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid and self.ask else self.last

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mid if self.bid and self.ask and self.mid else 0.0

    def session_bars(self) -> list[Bar]:
        return [self.bars[k] for k in sorted(self.bars)]

    def vwap(self) -> float:
        num = den = 0.0
        for b in self.bars.values():
            typical = (b.high + b.low + b.close) / 3
            num += typical * b.volume
            den += b.volume
        return num / den if den else self.last

    def opening_range(self, minutes: int) -> tuple[float, float, float, float] | None:
        """(open, high, low, close) of the first `minutes` of the session, once complete."""
        bars = self.session_bars()
        if not bars:
            return None
        day = bars[0].end.date()
        cutoff = datetime.combine(day, OPEN, ET) + timedelta(minutes=minutes)
        window = [b for b in bars if b.end <= cutoff]
        if len(window) < max(1, minutes - 1) or bars[-1].end < cutoff:
            return None
        return window[0].open, max(b.high for b in window), min(b.low for b in window), window[-1].close

    def day_high(self) -> float:
        return max((b.high for b in self.bars.values()), default=self.last)

    def day_low(self) -> float:
        return min((b.low for b in self.bars.values()), default=self.last)

    def day_open(self) -> float:
        bars = self.session_bars()
        return bars[0].open if bars else self.last


class MarketData:
    def __init__(self, client: TossClient):
        self.client = client
        self.symbols: dict[str, SymbolState] = {}
        self.bar_symbols: set[str] = set()

    def get(self, symbol: str) -> SymbolState:
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolState(symbol)
        return self.symbols[symbol]

    # WebSocket callbacks
    def on_trade(self, symbol: str, price: float, volume: float, ts: datetime) -> None:
        s = self.get(symbol)
        s.last, s.last_ts = price, ts

    def on_book(self, symbol: str, bid: float, bid_sz: float, ask: float, ask_sz: float, ts: datetime) -> None:
        s = self.get(symbol)
        s.bid, s.bid_size, s.ask, s.ask_size = bid, bid_sz, ask, ask_sz
        if not s.last:
            s.last = (bid + ask) / 2

    async def refresh_bars(self, symbols: set[str] | None = None) -> None:
        """Pull today's 1-minute candles (regular session) for the tracked symbols."""
        targets = symbols if symbols is not None else self.bar_symbols
        await asyncio.gather(*(self._refresh_one(s) for s in targets))

    async def _refresh_one(self, symbol: str) -> None:
        try:
            res = await self.client.candles(symbol, "1m", count=200)
        except TossAPIError as exc:
            LOG.debug("candles %s failed: %s", symbol, exc)
            return
        state = self.get(symbol)
        now_et = datetime.now(ET)
        for c in res.get("candles", []):
            end = datetime.fromisoformat(c["timestamp"]).astimezone(ET)
            if end.date() != now_et.date() or not (time(9, 31) <= end.time() <= time(16, 0)):
                continue
            if end > now_et:  # still-forming bar
                continue
            state.bars[end] = Bar(end, float(c["openPrice"]), float(c["highPrice"]), float(c["lowPrice"]), float(c["closePrice"]), float(c["volume"]))
        bars = state.session_bars()
        if bars and not state.last:
            state.last = bars[-1].close

    def reset_day(self) -> None:
        for s in self.symbols.values():
            s.bars.clear()
