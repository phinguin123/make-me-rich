"""Live per-symbol market state.

With Alpaca Algo Trader Plus, prices come from the full-market SIP stream (every trade,
consolidated NBBO) and minute bars from Alpaca's SIP bars: the same data the research used.
Toss is the fallback: its sampled WebSocket frames and 1-minute candles are used for any
symbol without fresh SIP data (and for everything when Alpaca is off).
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
    sip_ts: datetime | None = None  # local time of the last SIP trade/quote (None: Toss-only symbol)
    quote_ts: datetime | None = None  # local time bid/ask were last updated

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


SIP_FRESH = 15.0  # seconds: Toss frames are ignored for a symbol with SIP data this recent


class MarketData:
    def __init__(self, client: TossClient, alpaca=None):
        self.client = client
        self.alpaca = alpaca  # AlpacaData with an open session (SIP bars), or None
        self.symbols: dict[str, SymbolState] = {}
        self.bar_symbols: set[str] = set()
        self.bar_source = "toss"

    def get(self, symbol: str) -> SymbolState:
        if symbol not in self.symbols:
            self.symbols[symbol] = SymbolState(symbol)
        return self.symbols[symbol]

    def sip_fresh(self, symbol: str) -> bool:
        s = self.symbols.get(symbol)
        return bool(s and s.sip_ts and (datetime.now(ET) - s.sip_ts).total_seconds() < SIP_FRESH)

    # WebSocket callbacks (src: "toss" sampled frames, "sip" Alpaca full tape)
    def on_trade(self, symbol: str, price: float, volume: float, ts: datetime, src: str = "toss") -> None:
        s = self.get(symbol)
        if src == "sip":
            s.sip_ts = datetime.now(ET)
        elif self.sip_fresh(symbol):
            return
        s.last, s.last_ts = price, ts

    def on_book(self, symbol: str, bid: float, bid_sz: float, ask: float, ask_sz: float, ts: datetime, src: str = "toss") -> None:
        s = self.get(symbol)
        if src == "sip":
            s.sip_ts = datetime.now(ET)
        elif self.sip_fresh(symbol):
            return
        s.bid, s.bid_size, s.ask, s.ask_size = bid, bid_sz, ask, ask_sz
        s.quote_ts = datetime.now(ET)
        if not s.last:
            s.last = (bid + ask) / 2

    async def refresh_bars(self, symbols: set[str] | None = None, now: datetime | None = None) -> None:
        """Today's completed 1-minute bars (regular session): Alpaca SIP, else Toss candles."""
        targets = set(symbols if symbols is not None else self.bar_symbols)
        if not targets:
            return
        if self.alpaca is not None:
            try:
                await self._refresh_sip(targets, now or datetime.now(ET))
                self.bar_source = "sip"
                return
            except Exception as exc:  # noqa: BLE001 — fall back to Toss for this minute
                LOG.warning("SIP bars failed (%s); using Toss candles", exc)
        self.bar_source = "toss"
        await asyncio.gather(*(self._refresh_one(s) for s in targets))

    async def _refresh_sip(self, targets: set[str], now_et: datetime) -> None:
        open_dt = datetime.combine(now_et.date(), OPEN, ET)
        lasts = [max(self.get(s).bars) if self.get(s).bars else open_dt for s in targets]
        start = max(open_dt, min(lasts) - timedelta(minutes=4))  # re-read a few bars: late prints update them
        df = await self.alpaca.bars(sorted(targets), "1Min", start.isoformat(), now_et.isoformat(), feed="sip", adjustment="raw")
        for row in df.itertuples(index=False):
            end = row.ts.tz_convert(ET).to_pydatetime() + timedelta(minutes=1)
            if end.date() != now_et.date() or not (time(9, 31) <= end.time() <= time(16, 0)) or end > now_et:
                continue
            self.get(row.symbol).bars[end] = Bar(end, float(row.open), float(row.high), float(row.low), float(row.close), float(row.volume))
        for s in targets:
            st = self.get(s)
            if st.bars and not st.last:
                st.last = st.bars[max(st.bars)].close

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
