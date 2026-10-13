"""SOXL/SOXS scalp sleeve: regime-gated opening-range breakout (regular session only).

Validated in trader/research/scalp.py (2021-2026, 1-minute SIP, Toss costs):
  - trade only when SOXX closed below its 50-day average the day before (semis downtrend);
    in uptrends the same breakout loses money, so the sleeve stands aside
  - opening range = first 30 minutes of SOXL; first break up -> buy SOXL, first break down -> buy SOXS
  - entries until 11:30 ET, one trade per day
  - stop: SOXL back through the opposite side of the opening range; otherwise exit at the close
  - bear-regime result: +0.32%/trade after costs (IS +0.34%, OOS +0.30%), ~88 trades/yr
The pre-market runs a different, mean-reversion edge (trader/scalp_pre.py). The Korean-hours day
market showed no edge after costs and is not traded.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .broker import Broker, Order
from .config import DATA_DIR, Settings
from .market import MarketData
from .scalp_pre import PremarketDip
from .toss.client import TossAPIError, TossClient

LOG = logging.getLogger("scalp")
ET = ZoneInfo("America/New_York")
SYMBOLS = ("SOXL", "SOXS", "SOXX")


@dataclass
class ScalpConfig:
    or_minutes: int = 30
    entry_minutes: int = 120  # entries allowed until open + 120 min
    regime_ma: int = 50
    position_frac: float = 0.5  # of sleeve equity per trade (1.0 doubled both return and drawdown in the backtest)
    max_chase: float = 0.004


@dataclass
class ScalpState:
    day: str = ""
    regime: str = "unknown"  # bear (trade) | bull (stand aside)
    soxx_close: float = 0.0
    soxx_ma: float = 0.0
    or_high: float = 0.0
    or_low: float = 0.0
    status: str = "waiting"  # waiting | armed | in_trade | done | standing_aside
    side: str = ""  # SOXL | SOXS
    qty: float = 0.0
    entry: float = 0.0
    note: str = ""
    trades: list = field(default_factory=list)


class ScalpSleeve:
    def __init__(self, settings: Settings, broker: Broker, market: MarketData, toss: TossClient,
                 allow_order: Callable[[bool], bool], log: Callable[[str, int], None],
                 entries_blocked: Callable[[], bool] = lambda: False):
        self.s, self.broker, self.market, self.toss = settings, broker, market, toss
        self.allow_order, self.log, self.entries_blocked = allow_order, log, entries_blocked
        self.cfg = ScalpConfig(position_frac=settings.scalp_position_frac)
        self.path = DATA_DIR / f"scalp_{broker.name}.json"
        self.cash = settings.scalp_capital
        self.realized = 0.0
        self.st = ScalpState()
        self.busy = False
        self.pre = PremarketDip(self) if settings.scalp_pre else None
        self._load()

    # ── persistence ───────────────────────────────────────────────────────
    def _load(self) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            self.cash, self.realized = float(raw.get("cash", self.cash)), float(raw.get("realized", 0.0))
            self.st = ScalpState(**raw.get("state", {}))
            if self.pre and raw.get("pre"):
                from .scalp_pre import PreState
                self.pre.st = PreState(**raw["pre"])

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        data = {"cash": self.cash, "realized": self.realized, "state": asdict(self.st)}
        if self.pre:
            data["pre"] = asdict(self.pre.st)
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def equity(self) -> float:
        pre = self.pre.value() if self.pre else 0.0
        if self.st.qty and self.st.side:
            return self.cash + pre + self.st.qty * (self.market.get(self.st.side).last or self.st.entry)
        return self.cash + pre

    # ── daily setup ───────────────────────────────────────────────────────
    async def prepare(self, today: date) -> None:
        """Regime from SOXX daily closes (known before the open)."""
        if self.st.day == today.isoformat() and self.st.regime != "unknown":
            return
        carry_trades = self.st.trades[-50:]
        carried = (self.st.side, self.st.qty, self.st.entry) if self.st.qty > 0 else None
        self.st = ScalpState(day=today.isoformat(), trades=carry_trades)
        if carried:  # an exit failed yesterday: never forget a position, sell it at the open
            self.st.side, self.st.qty, self.st.entry = carried
            self.st.status, self.st.note = "exit_pending", "position carried from a failed exit; selling at the open"
            self.log(f"Scalp: {carried[1]:g} {carried[0]} carried overnight — will sell at the open", logging.WARNING)
            self.save()
            return
        try:
            res = await self.toss.candles("SOXX", "1d", count=self.cfg.regime_ma + 10)
            closes = [float(c["closePrice"]) for c in res.get("candles", [])
                      if datetime.fromisoformat(c["timestamp"]).date() < today][: self.cfg.regime_ma]
        except TossAPIError as exc:
            self.log(f"Scalp: SOXX history failed ({exc}); standing aside today", logging.WARNING)
            self.st.regime, self.st.status = "unknown", "standing_aside"
            self.save()
            return
        if len(closes) < int(self.cfg.regime_ma * 0.6):
            self.st.regime, self.st.status, self.st.note = "unknown", "standing_aside", "not enough SOXX history"
        else:
            self.st.soxx_close, self.st.soxx_ma = closes[0], sum(closes) / len(closes)
            bear = self.st.soxx_close < self.st.soxx_ma
            self.st.regime = "bear" if bear else "bull"
            self.st.status = "waiting" if bear else "standing_aside"
            self.st.note = "" if bear else "SOXX above its 50-day average: breakouts lose money in uptrends"
            if not bear and self.s.scalp_force_day == today.isoformat():
                self.st.status = "waiting"
                self.st.note = "uptrend, trading anyway: one-day override (backtest: about -0.26%/trade on uptrend days)"
        self.log(f"Scalp regime {self.st.regime}: SOXX {self.st.soxx_close:.2f} vs {self.cfg.regime_ma}-day avg {self.st.soxx_ma:.2f}", logging.INFO)
        self.save()

    # ── intraday ──────────────────────────────────────────────────────────
    def on_minute(self, now: datetime, session_open: datetime) -> None:
        if self.st.status != "waiting":
            return
        rng = self.market.get("SOXL").opening_range(self.cfg.or_minutes)
        if rng:
            self.st.or_high, self.st.or_low = rng[1], rng[2]
            self.st.status = "armed"
            self.log(f"Scalp armed: SOXL opening range {self.st.or_low:.2f} - {self.st.or_high:.2f}", logging.INFO)
            self.save()

    def evaluate(self, now: datetime, session_open: datetime, spawn: Callable) -> None:
        """Called every tick during the regular session."""
        if self.busy:
            return
        if self.st.status == "exit_pending" and self.st.qty > 0:
            self.busy = True
            spawn(self._exit("carry", claimed=True))
            return
        soxl = self._fresh("SOXL")
        if not soxl:
            return
        if self.st.status == "armed":
            if now >= session_open + timedelta(minutes=self.cfg.entry_minutes):
                self.st.status, self.st.note = "done", "no breakout before the entry cutoff"
                self.save()
            elif self.entries_blocked():
                return  # paused / risk halt: keep watching, never enter
            elif soxl > self.st.or_high:
                self.busy = True  # set before the task runs so the next tick can't fire a duplicate
                spawn(self._enter("SOXL", self.st.or_high))
            elif soxl < self.st.or_low:
                self.busy = True
                spawn(self._enter("SOXS", None))
        elif self.st.status == "in_trade" and self.st.qty > 0:
            if (self.st.side == "SOXL" and soxl <= self.st.or_low) or (self.st.side == "SOXS" and soxl >= self.st.or_high):
                self.busy = True
                spawn(self._exit("stop", claimed=True))

    def _fresh(self, sym: str) -> float:
        st = self.market.get(sym)
        if not st.last_ts:
            return 0.0
        return st.last if (datetime.now(ET) - st.last_ts.astimezone(ET)).total_seconds() < 30 else 0.0

    async def _enter(self, side: str, trigger: float | None) -> None:
        try:
            q = self.market.get(side)
            ref = q.ask or q.last
            if not ref:
                return
            limit = math.ceil(ref * (1 + 0.0015) * 100) / 100
            if trigger and ref > trigger * (1 + self.cfg.max_chase):  # SOXL only: don't chase a gap through the range
                self.st.status, self.st.note = "done", f"SOXL ran {ref / trigger - 1:.2%} past the range"
                return
            qty = math.floor(min(self.cfg.position_frac * self.equity(), self.cash, await self.broker.cash()) / (limit * (1 + self.broker.commission)))
            if qty < 1 or not self.allow_order(False):
                self.st.status, self.st.note = "done", "size < 1 share or order throttle"
                return
            self.st.status, self.st.side = "in_trade", side
            self.save()
            self.log(f"Scalp BUY {side} {qty} @ <= {limit} (SOXL broke {'above' if side == 'SOXL' else 'below'} the opening range)", logging.INFO)
            order = await self.broker.submit(side, "BUY", qty, limit, "scalp-entry")
            await self._wait(order, 8)
            if not order.done:
                await self.broker.cancel(order)
                await self._wait(order, 3)
            if self.st.qty <= 0:
                self.st.status, self.st.side, self.st.note = "done", "", f"entry not filled ({order.status})"
        finally:
            self.busy = False
            self.save()

    async def _exit(self, reason: str, claimed: bool = False) -> None:
        if (self.busy and not claimed) or self.st.qty <= 0:
            self.busy = False if claimed else self.busy
            return
        self.busy = True
        try:
            side = self.st.side
            for cushion in (0.003, 0.01, None):
                if self.st.qty <= 0 or not self.allow_order(True):
                    break
                q = self.market.get(side)
                ref = q.bid or q.last
                limit = None if cushion is None else max(0.01, math.floor(ref * (1 - cushion) * 100) / 100)
                self.log(f"Scalp SELL {side} {self.st.qty:g} ({reason})", logging.INFO)
                order = await self.broker.submit(side, "SELL", self.st.qty, limit, f"scalp-{reason}")
                await self._wait(order, 5)
                if not order.done:
                    await self.broker.cancel(order)
                    await self._wait(order, 3)
            if self.st.qty <= 1e-9:
                self.st.status = "done"
        finally:
            self.busy = False
            self.save()

    async def eod(self) -> None:
        if self.st.status in ("waiting", "armed"):
            self.st.status, self.st.note = "done", "no breakout today"
        if self.st.qty > 0:
            await self._exit("close")
        if self.pre and self.pre.st.qty > 0:
            await self.pre.exit_now("close")
        self.save()

    async def _wait(self, order: Order, seconds: float) -> None:
        import asyncio

        end = asyncio.get_running_loop().time() + seconds
        while not order.done and asyncio.get_running_loop().time() < end:
            try_fill = getattr(self.broker, "try_fill", None)
            if try_fill:
                try_fill(order)
            await asyncio.sleep(0.25)
        if not order.done:
            await self.broker.sync()

    # ── fills ─────────────────────────────────────────────────────────────
    def on_fill(self, order: Order, qty: float, price: float) -> None:
        if order.tag.startswith("scalp-pre") and self.pre:
            self.pre.on_fill(order, qty, price)
            return
        fee = qty * price * self.broker.commission
        if order.side == "BUY":
            self.st.entry = (self.st.entry * self.st.qty + price * qty + fee) / (self.st.qty + qty)
            self.st.qty += qty
            self.cash -= qty * price + fee
        else:
            proceeds = qty * price - fee - qty * price * 0.0000206
            pnl = proceeds - qty * self.st.entry
            self.realized += pnl
            self.cash += proceeds
            self.st.qty = round(self.st.qty - qty, 6)
            self.st.trades.append({"day": self.st.day, "side": self.st.side, "qty": qty, "entry": round(self.st.entry, 4),
                                   "exit": round(price, 4), "pnl": round(pnl, 2), "reason": order.tag.replace("scalp-", "")})
            self.log(f"Scalp SOLD {order.symbol} {qty:g} @ {price:.2f} P&L {pnl:+.2f}", logging.INFO)
        self.save()

    def snapshot(self) -> dict:
        return {"enabled": True, "capital": self.s.scalp_capital, "equity": self.equity(), "cash": self.cash,
                "realized": self.realized, "state": asdict(self.st), "config": asdict(self.cfg),
                "quotes": {s: self.market.get(s).last for s in SYMBOLS},
                "pre": self.pre.snapshot() if self.pre else {"enabled": False}}
