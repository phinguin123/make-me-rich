"""Pre-market edge for the scalp sleeve: buy liquidity-driven dips below the pre-market VWAP, on a book of
14 liquid names that have no 24-hour futures anchor (SOXL, SOXS, NVDA, MU, AMD, TSLA, MSTR, PLTR, COIN, META,
AAPL, AMZN, TSLL, NVDL; index ETFs like TQQQ don't work: futures keep them at fair value).

Validated in trader/research/scalp_ext.py, pre_multi.py, pre_slots.py (2021-2026 SIP 1-minute bars, Toss costs):
  - between 04:30 and 09:00 ET (17:30-22:00 KST in summer), when a name trades `dip` below its pre-market VWAP (dip = 0.4 x its
    20-day daily volatility, 1.5%-6%) and 0.3% through, buy with a limit order at that level: we provide
    liquidity to forced sellers in a thin market and never chase
  - take profit with a resting sell limit at entry + dip (a DAY order: it keeps working through the open);
    otherwise sell what's left at 10:30 ET with a market order. Selling at 09:25, in the thinnest market of the
    day just before the open's liquidity, gave up the recovery: holding to 10:30 lifted the book from +38% to
    +50%/yr (fair fills; strict +29% to +40%), better in both halves (research/open_flush.py, 2026-10-14).
    On days the breakout may trade (SOXX below its 50-day average, or its regime not known yet) leftovers
    are sold at 09:59 instead, so the breakout has the pot when it can first buy at 10:00.
  - at most 3 positions at once, each a third of the scalp pot; one trade per name per day
  - only while QQQ closed above its 200-day average (dip-buying lost in the 2022 bear market)
  - book result on its own money (3 slots + QQQ filter, 04:30 start, 10:30 exit): +40%/yr with strict fills
    (Sharpe 3.4, max DD -16%), +50%/yr reacting like the live bot (Sharpe 4.1, max DD -14%)
  - SOXL alone, all 2021-2026 days:
    after Toss fees (corrected 2026-10-13): +1.05% if filled on a
    0.3% trade-through (198 trades, t 6.7), +0.78% reacting like the live bot (124, t 4.5), +0.35% if
    filled only when a 1-minute bar closes below the level (123, t 1.6; first half of the sample -0.17%)
Pre-market prints can trade below our price on another venue without filling us (no trade-through
protection outside regular hours), so the realistic result is the lower range.
The Korean-hours day market was tested with ten hypothesis families (Korean semis lead-lag, catch-up,
VWAP reversion, prior-session momentum, stale SOXL/SOXS quotes) and none beat costs: it is not traded.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Callable

import pandas as pd

from .broker import Order
from .journal import jr
from .toss.client import TossAPIError

if TYPE_CHECKING:
    from .scalp import ScalpSleeve

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SEC_FEE = 0.0000206
NAMES = ("SOXL", "SOXS", "NVDA", "MU", "AMD", "TSLA", "MSTR", "PLTR", "COIN", "META", "AAPL", "AMZN", "TSLL", "NVDL")
ACTIVE = ("entry_open", "in_trade", "exiting", "exit_pending")


@dataclass
class PreConfig:
    start: time = time(4, 30)  # entries allowed from (ET): 30 min after the pre-market opens (research: 17:30 KST beat 20:00)
    cutoff: time = time(9, 0)  # no new entries after
    exit_at: time = time(10, 30)  # sell what's left (holding through the open beat selling at 09:25)
    exit_orb: time = time(9, 59)  # instead, on days the breakout may buy from 10:00 and needs the pot
    z: float = 0.4  # dip = z x 20-day daily volatility of SOXL
    min_dip: float = 0.015
    max_dip: float = 0.06
    rest_seconds: float = 600  # an unfilled entry rests at the level this long (backtest: 10 minutes)
    max_attempts: int = 2
    through: float = 0.003  # buy only once the price trades this far below the level (as the research required)
    slots: int = 3  # positions at once across the book, each 1/slots of the scalp pot
    qqq_filter: bool = True  # trade only while QQQ's prior close is above its 200-day average


@dataclass
class PreState:
    symbol: str = "SOXL"
    day: str = ""
    status: str = "idle"  # idle | waiting | watching | entry_open | in_trade | exiting | exit_pending | done | standing_aside
    sigma: float = 0.0
    dip: float = 0.0
    vwap: float = 0.0
    level: float = 0.0
    qty: float = 0.0
    entry: float = 0.0
    target: float = 0.0
    attempts: int = 0
    entry_id: str = ""
    entry_qty: float = 0.0
    rest_until: str = ""  # an unfilled entry order is cancelled at this time (ISO, ET)
    target_id: str = ""
    target_filled: float = 0.0
    source: str = ""  # where the VWAP came from: sip (Alpaca, as in the research) or toss
    note: str = ""


class PremarketDip:
    """One name of the book: its own state, orders and exits. The book prepares it and feeds its VWAP."""

    def __init__(self, book: "PremarketBook", symbol: str):
        self.book, self.sl, self.cfg = book, book.sl, book.cfg
        self.SYMBOL = symbol
        self.st = PreState(symbol=symbol)
        self.bars: dict[datetime, tuple[float, float, float, float]] = {}  # start -> (high, low, close, volume)
        self.busy = False
        self.now: datetime | None = None
        self._minute: datetime | None = None
        self._prep_day = ""
        self._resumed = False
        self._exit_after: datetime | None = None  # back-off between failed exit rounds
        self.target_order: Order | None = None
        self.entry_order: Order | None = None

    # ── helpers ───────────────────────────────────────────────────────────
    def _at(self, day: date, t: time) -> datetime:
        return datetime.combine(day, t, ET)

    def _log(self, msg: str, level: int = logging.INFO) -> None:
        self.sl.log(f"Pre-market {self.SYMBOL}: {msg}", level)

    def active(self) -> bool:
        return self.st.qty > 0 or self.st.status in ACTIVE

    def value(self) -> float:
        return self.st.qty * (self.sl.market.get(self.SYMBOL).last or self.st.entry) if self.st.qty > 0 else 0.0

    # ── scheduler hook (every engine tick on trading days) ──────────────────
    def tick(self, now: datetime, pre: dict, regular_open: datetime, spawn: Callable) -> None:
        self.now = now
        today = now.date()
        if not self._resumed:
            self._resumed = True
            self._resume(spawn)
        if self.busy or self.st.day != today.isoformat():
            return
        start = max(self._at(today, self.cfg.start), pre["open"] + timedelta(minutes=30))
        cutoff = self._at(today, self.cfg.cutoff)
        exit_at = self.book.exit_time(today)
        if self.st.status == "entry_open":  # a resting entry: finished on a fill, when its time is up, or at the cutoff
            o = self.entry_order
            rest_over = bool(self.st.rest_until) and now >= datetime.fromisoformat(self.st.rest_until)
            if o is None or o.done or rest_over or now >= cutoff:
                self.busy = True
                spawn(self._guard(self._finish_entry()))
            return
        if self.st.qty > 0 and (now >= exit_at or self.st.status == "exit_pending"):
            if self.st.status != "exiting" and (self._exit_after is None or now >= self._exit_after):
                self.busy = True
                spawn(self._guard(self._exit("time" if now >= exit_at else "carry", market=now >= regular_open)))
            return
        if self.st.status == "watching":
            if now >= cutoff:
                self.st.status, self.st.note = "done", "no dip to the level before 09:00"
                self.sl.save()
            elif now >= start and self.st.level and not self.sl.entries_blocked():
                px = self._fresh()
                if px and px <= self.st.level * (1 - self.cfg.through) and self.book.slot_free():
                    self.busy = True
                    self.book.reserved.add(self.SYMBOL)  # holds the slot until the order exists
                    spawn(self._guard(self._enter(today)))

    async def _guard(self, coro, hold: bool = True) -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001 — never kill the engine loop
            self._log(f"error {exc!r}", logging.ERROR)
        finally:
            if hold:
                self.busy = False
            self.book.reserved.discard(self.SYMBOL)
            self.sl.save()

    def _fresh(self) -> float:
        st = self.sl.market.get(self.SYMBOL)
        if not st.last_ts:
            return 0.0
        return st.last if (datetime.now(ET) - st.last_ts.astimezone(ET)).total_seconds() < 30 else 0.0

    # ── daily setup (called by the book with the closes it fetched for every name) ──
    def setup(self, today: date, now: datetime, closes: list[float] | None, regime_note: str) -> None:
        if self.st.day == today.isoformat() and self.st.status not in ("idle",):
            return
        carried = (self.st.qty, self.st.entry) if self.st.qty > 0 else None
        self.st = PreState(symbol=self.SYMBOL, day=today.isoformat())
        self.bars.clear()
        if carried:
            self.st.qty, self.st.entry = carried
            self.st.status, self.st.note = "exit_pending", "position carried from a failed exit; selling now"
            self._log(f"{carried[0]:g} carried — selling", logging.WARNING)
            return
        if now.time() >= self.cfg.cutoff:
            self.st.status, self.st.note = "done", "started after the 09:00 entry cutoff"
            return
        if regime_note:
            self.st.status, self.st.note = "standing_aside", regime_note
            return
        if not closes:
            self.st.status, self.st.note = "standing_aside", "no daily history"
            return
        rets = [math.log(a / b) for a, b in zip(closes[:-1], closes[1:]) if a > 0 and b > 0]
        if len(rets) < 15:
            self.st.status, self.st.note = "standing_aside", "not enough daily history"
            return
        if max(abs(r) for r in rets) > 0.5:
            self.st.status, self.st.note = "standing_aside", "split or bad price in the last 20 days"
            return
        mean = sum(rets) / len(rets)
        self.st.sigma = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        self.st.dip = min(self.cfg.max_dip, max(self.cfg.min_dip, self.cfg.z * self.st.sigma))
        if self.sl.edge_size("scalp-pre") <= 0:
            self.st.status, self.st.note = "standing_aside", "switched off by the edge monitor (live results far below the backtest)"
        else:
            self.st.status, self.st.note = "watching", ""
        jr("D", "pre", "prepare", {"sym": self.SYMBOL, "sigma": self.st.sigma, "dip": self.st.dip, "status": self.st.status})

    def set_bars(self, bars: dict, source: str) -> None:
        if source == "sip":
            self.bars = bars  # SIP returns the whole session every minute
        else:
            if self.st.source == "sip":
                self.bars.clear()  # don't mix sources in one VWAP
            self.bars.update(bars)
        self.st.source = source
        self._set_vwap()

    async def refresh_from_toss(self, now: datetime, pre_open: datetime) -> None:
        """Fallback when Alpaca isn't available: Toss 1-minute candles (Toss stamps bars with their end)."""
        before = None
        backfill = not self.bars or self.st.source != "toss"
        got: dict = {}
        for _ in range(4):
            try:
                res = await self.sl.toss.candles(self.SYMBOL, "1m", count=200 if backfill else 10, before=before)
            except TossAPIError as exc:
                self._log(f"1m bars failed ({exc})", logging.WARNING)
                return
            cs = res.get("candles", [])
            oldest = None
            for x in cs:
                ts = datetime.fromisoformat(x["timestamp"]).astimezone(ET)
                oldest = ts if oldest is None or ts < oldest else oldest
                if pre_open < ts <= now:
                    got[ts] = (float(x["highPrice"]), float(x["lowPrice"]), float(x["closePrice"]), float(x["volume"]))
            before = res.get("nextBefore")
            if not backfill or not cs or not before or (oldest and oldest <= pre_open):
                break
        self.set_bars(got, "toss")

    def _set_vwap(self) -> None:
        pv = sum((h + l + c) / 3 * v for h, l, c, v in self.bars.values())
        vol = sum(v for *_, v in self.bars.values())
        if vol > 0:
            self.st.vwap = pv / vol
            self.st.level = math.floor(self.st.vwap * (1 - self.st.dip) * 100) / 100 if self.st.dip else 0.0
            jr("D", "pre", "vwap", {"sym": self.SYMBOL, "vwap": self.st.vwap, "level": self.st.level,
                                    "last": self.sl.market.get(self.SYMBOL).last, "bars": len(self.bars), "src": self.st.source})

    # ── trading ───────────────────────────────────────────────────────────
    async def _enter(self, today: date) -> None:
        level = self.st.level
        sl = self.sl
        frac = sl.cfg.position_frac * sl.edge_size("scalp-pre") / max(1, self.cfg.slots)
        qty = math.floor(min(frac * sl.equity(), sl.cash, await sl.broker.cash()) / (level * (1 + sl.broker.commission)))
        if qty < 1 or not sl.allow_order(False):
            self.st.status, self.st.note = "done", "size < 1 share or order throttle"
            return
        self.st.attempts += 1
        self.st.status = "entry_open"
        jr("D", "pre", "trigger", {"sym": self.SYMBOL, "level": level, "last": sl.market.get(self.SYMBOL).last, "vwap": self.st.vwap, "qty": qty,
                                   "attempt": self.st.attempts})
        self._log(f"BUY {qty} @ {level} limit (dip {self.st.dip:.2%} below VWAP {self.st.vwap:.2f})")
        order = await sl.broker.submit(self.SYMBOL, "BUY", qty, level, "scalp-pre-entry")
        self.entry_order = order
        self.st.entry_id, self.st.entry_qty = order.id, qty
        self.st.rest_until = ((self.now or datetime.now(ET)) + timedelta(seconds=self.cfg.rest_seconds)).isoformat()
        sl.save()
        if order.done:  # filled at once (marketable) or rejected
            await self._finish_entry()

    async def _finish_entry(self) -> None:
        """End a resting entry: cancel what's left, then take-profit (if anything filled) or watch again."""
        o = self.entry_order
        if o is not None and not o.done:
            await self.sl.broker.cancel(o)
            await self.sl._wait(o, 3)
        self.entry_order, self.st.entry_id, self.st.rest_until = None, "", ""
        if self.st.qty > 0:
            await self._place_target()
        elif o is not None and o.status == "REJECTED":
            self.st.status, self.st.note = "done", f"entry rejected ({o.error})"
        elif self.st.attempts < self.cfg.max_attempts:
            self.st.status, self.st.note = "watching", "entry not filled; watching again"
        else:
            self.st.status, self.st.note = "done", "entry not filled"

    async def _place_target(self) -> None:
        self.st.target = math.ceil(self.st.entry * (1 + self.st.dip) * 100) / 100
        self.st.status = "in_trade"
        if not self.sl.allow_order(True):
            self.st.note = "target not placed (order throttle); selling at the time exit"
            return
        jr("D", "pre", "target", {"sym": self.SYMBOL, "entry": self.st.entry, "target": self.st.target, "qty": self.st.qty})
        order = await self.sl.broker.submit(self.SYMBOL, "SELL", self.st.qty, self.st.target, "scalp-pre-target")
        if order.status == "REJECTED":
            self.st.note = f"target rejected ({order.error}); selling at the time exit"
            return
        self.target_order, self.st.target_id, self.st.target_filled = order, order.id, 0.0
        self._log(f"take-profit SELL {self.st.qty:g} @ {self.st.target} resting")

    async def _exit(self, reason: str, market: bool = False) -> None:
        sl = self.sl
        self.st.status = "exiting"
        jr("D", "pre", "exit", {"sym": self.SYMBOL, "reason": reason, "qty": self.st.qty, "market": market})
        if self.target_order and not self.target_order.done:
            await sl.broker.cancel(self.target_order)
            await sl._wait(self.target_order, 3)
        self.target_order, self.st.target_id = None, ""
        cushions = (None,) if market else (0.003, 0.01, 0.02)
        for cushion in cushions:
            if self.st.qty <= 0 or not sl.allow_order(True):
                break
            q = sl.market.get(self.SYMBOL)
            ref = q.bid or q.last
            limit = None if cushion is None else max(0.01, math.floor(ref * (1 - cushion) * 100) / 100)
            self._log(f"SELL {self.st.qty:g} ({reason})")
            order = await sl.broker.submit(self.SYMBOL, "SELL", self.st.qty, limit, f"scalp-pre-{reason}")
            await sl._wait(order, 5)
            if not order.done:
                await sl.broker.cancel(order)
                await sl._wait(order, 3)
        if self.st.qty > 1e-9:
            self.st.status, self.st.note = "exit_pending", "exit not filled; retrying in a minute (market order after the open)"
            self._exit_after = (self.now or datetime.now(ET)) + timedelta(seconds=60)
        else:
            self.st.status = "done"

    async def exit_now(self, reason: str = "manual") -> None:
        if self.st.qty > 0:
            await self._exit(reason, market=bool(self.now and self.now.time() >= time(9, 30)))
            self.sl.save()

    def _resume(self, spawn: Callable) -> None:
        """After a restart: cancel a stale entry, re-attach a resting take-profit (live) or re-place it (paper)."""
        if self.st.status == "entry_open":
            if self.st.entry_id and self.sl.broker.name == "live":
                o = Order(id=self.st.entry_id, client_id="", symbol=self.SYMBOL, side="BUY", qty=self.st.entry_qty,
                          limit=self.st.level, tag="scalp-pre-entry", filled_qty=self.st.qty)
                self.sl.broker.orders[o.id] = o
                self.busy = True
                spawn(self._guard(self._cancel_stale_entry(o)))
            else:
                self.st.status = "in_trade" if self.st.qty > 0 else "watching"
                if self.st.qty > 0:
                    self.busy = True
                    spawn(self._guard(self._place_target()))
            return
        if self.st.qty <= 0 or self.st.status != "in_trade":
            return
        if self.st.target_id and self.sl.broker.name == "live":
            o = Order(id=self.st.target_id, client_id="", symbol=self.SYMBOL, side="SELL", qty=self.st.qty + self.st.target_filled,
                      limit=self.st.target, tag="scalp-pre-target", filled_qty=self.st.target_filled)
            self.sl.broker.orders[o.id] = o
            self.target_order = o
            spawn(self.sl.broker.sync())
        else:
            self.st.target_id = ""
            self.busy = True
            spawn(self._guard(self._place_target()))

    async def _cancel_stale_entry(self, o: Order) -> None:
        await self.sl.broker.cancel(o)
        await self.sl._wait(o, 3)
        self.st.entry_id = ""
        if self.st.qty > 0:
            await self._place_target()
        else:
            self.st.status = "watching"

    # ── fills (routed from ScalpSleeve.on_fill) ────────────────────────────
    def on_fill(self, order: Order, qty: float, price: float) -> None:
        sl = self.sl
        fee = qty * price * sl.broker.commission
        if order.side == "BUY":
            self.st.entry = (self.st.entry * self.st.qty + price * qty + fee) / (self.st.qty + qty)
            self.st.qty += qty
            sl.cash -= qty * price + fee
            self._log(f"BOUGHT {qty:g} @ {price:.2f}")
        else:
            proceeds = qty * price - fee - qty * price * SEC_FEE
            pnl = proceeds - qty * self.st.entry
            sl.realized += pnl
            sl.cash += proceeds
            self.st.qty = round(self.st.qty - qty, 6)
            if order.tag == "scalp-pre-target":
                self.st.target_filled += qty
            if self.st.qty <= 1e-9 and self.st.status == "in_trade":
                self.st.status, self.st.target_id = "done", ""
            reason = order.tag.replace("scalp-pre-", "")
            sl.st.trades.append({"day": self.st.day, "side": self.SYMBOL, "qty": qty, "entry": round(self.st.entry, 4),
                                 "exit": round(price, 4), "pnl": round(pnl, 2), "reason": f"pre {reason}"})
            self._log(f"SOLD {qty:g} @ {price:.2f} P&L {pnl:+.2f} ({reason})")
            sl.edge_closed("scalp-pre", pnl, qty, self.st.entry, self.st.qty, key=self.SYMBOL, side=self.SYMBOL, day=self.st.day)
        sl.save()

    def row(self) -> dict:
        st = self.st
        return {"symbol": self.SYMBOL, "status": st.status, "dip": st.dip, "vwap": st.vwap, "level": st.level,
                "last": self.sl.market.get(self.SYMBOL).last, "qty": st.qty, "entry": st.entry, "target": st.target,
                "source": st.source, "note": st.note}


class PremarketBook:
    """The pre-market dip on many names: daily setup, one SIP bar request per minute for every name's VWAP,
    the QQQ trend filter and the shared slots. Each name trades through its own PremarketDip."""

    def __init__(self, sleeve: "ScalpSleeve", names: tuple[str, ...] = NAMES, cfg: PreConfig | None = None):
        self.sl = sleeve
        self.cfg = cfg or PreConfig()
        self.names = {s: PremarketDip(self, s) for s in names}
        self.reserved: set[str] = set()
        self.regime = {"ok": True, "qqq": 0.0, "ma200": 0.0, "note": ""}
        self.busy = False
        self.now: datetime | None = None
        self._prep_day = ""
        self._minute: datetime | None = None

    # ── shared state ──────────────────────────────────────────────────────
    def slot_free(self) -> bool:
        used = {s for s, n in self.names.items() if n.active()} | self.reserved
        return len(used) < self.cfg.slots

    def exit_time(self, today: date) -> datetime:
        """When leftovers are sold: 10:30 ET, or 09:59 on days the breakout may buy from 10:00 (it sizes from the pot's cash)."""
        return datetime.combine(today, self.cfg.exit_orb if self.sl.orb_may_trade(today) else self.cfg.exit_at, ET)

    def active_names(self) -> list["PremarketDip"]:
        return [n for n in self.names.values() if n.active() or n.busy]

    def value(self) -> float:
        return sum(n.value() for n in self.names.values())

    def holding(self) -> bool:
        return any(n.st.qty > 0 or n.st.status == "entry_open" for n in self.names.values())

    @property
    def any_busy(self) -> bool:
        return self.busy or any(n.busy for n in self.names.values())

    def order_ids(self) -> set[str]:
        return {i for n in self.names.values() for i in (n.st.entry_id, n.st.target_id) if i}

    def claims(self) -> list[tuple[str, PreState, str]]:
        return [(f"pre-market {s}", n.st, s) for s, n in self.names.items() if n.st.qty > 0]

    def dump(self) -> dict:
        return {"names": {s: asdict(n.st) for s, n in self.names.items()}, "regime": self.regime}

    def load(self, raw: dict) -> None:
        states = raw.get("names") if "names" in raw else {raw.get("symbol", "SOXL"): raw}  # old single-name format
        for s, st in (states or {}).items():
            if s in self.names:
                self.names[s].st = PreState(**{**st, "symbol": s})
        self.regime = raw.get("regime", self.regime)

    # ── scheduling ────────────────────────────────────────────────────────
    def tick(self, now: datetime, pre: dict, regular_open: datetime, spawn: Callable) -> None:
        today = now.date()
        if self._prep_day != today.isoformat() and pre["open"] <= now < regular_open + timedelta(hours=6):
            self._prep_day = today.isoformat()
            self.busy = True
            spawn(self._guarded(self.prepare(today, now)))
            return
        if self.busy:
            return
        cutoff = datetime.combine(today, self.cfg.cutoff, ET)
        minute = now.replace(second=0, microsecond=0)
        if pre["open"] <= now < cutoff and minute != self._minute and now.second >= 2:
            self._minute = minute
            spawn(self._guarded(self.refresh(now, pre["open"]), hold=False))
        for n in self.names.values():
            n.tick(now, pre, regular_open, spawn)

    async def _guarded(self, coro, hold: bool = True) -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001 — never kill the engine loop
            self.sl.log(f"Pre-market book error {exc!r}", logging.ERROR)
        finally:
            if hold:
                self.busy = False
            self.sl.save()

    async def prepare(self, today: date, now: datetime) -> None:
        self.now = now
        for n in self.names.values():
            n.now = now
        closes = await self._daily_closes(today, list(self.names) + (["QQQ"] if self.cfg.qqq_filter else []), 260)
        note = ""
        if self.cfg.qqq_filter:
            q = closes.get("QQQ") or []
            if len(q) >= 150:
                ma = sum(q[:200]) / len(q[:200])
                self.regime = {"ok": q[0] > ma, "qqq": q[0], "ma200": ma, "note": ""}
                if q[0] <= ma:
                    note = f"QQQ {q[0]:.2f} below its 200-day average {ma:.2f}: dip-buying lost in bear markets (2022)"
            else:
                self.regime = {"ok": False, "qqq": 0.0, "ma200": 0.0, "note": "no QQQ history"}
                note = "QQQ history unavailable: standing aside"
            self.regime["note"] = note
            jr("D", "pre", "regime", self.regime)
        for s, n in self.names.items():
            n.setup(today, now, (closes.get(s) or [])[:21], note)
        watching = [s for s, n in self.names.items() if n.st.status == "watching"]
        self.sl.log(f"Pre-market book: watching {len(watching)} names {', '.join(watching)} "
                    f"({self.cfg.slots} slots{'; ' + note if note else ''})", logging.INFO)

    async def _daily_closes(self, today: date, symbols: list[str], count: int) -> dict[str, list[float]]:
        """Daily closes before today, newest first: Alpaca (one request for all names), else Toss per name."""
        alpaca = getattr(self.sl.market, "alpaca", None)
        out: dict[str, list[float]] = {}
        if alpaca is not None:
            try:
                start = datetime.combine(today - timedelta(days=int(count * 1.6)), time(0, 0), ET)
                df = await alpaca.bars(symbols, "1Day", start.isoformat(), datetime.combine(today, time(0, 0), ET).isoformat(),
                                       feed="sip", adjustment="split")
                for sym, g in df.groupby("symbol"):
                    g = g[g["ts"].dt.tz_convert(ET).dt.date < today].sort_values("ts", ascending=False)
                    out[sym] = [float(x) for x in g["close"].head(count)]
            except Exception as exc:  # noqa: BLE001 — fall back to Toss
                self.sl.log(f"Pre-market: Alpaca daily bars failed ({exc}); using Toss", logging.WARNING)
        for sym in symbols:
            if out.get(sym):
                continue
            for attempt in range(30):
                try:
                    res = await self.sl.toss.candles(sym, "1d", count=min(count, 300))
                    out[sym] = [float(c["closePrice"]) for c in res.get("candles", [])
                                if datetime.fromisoformat(c["timestamp"]).date() < today][:count]
                    break
                except TossAPIError as exc:  # e.g. the IP not yet allowlisted: keep trying until 09:00
                    if (self.now or datetime.now(ET)).time() >= self.cfg.cutoff and attempt:
                        break
                    if attempt == 0:
                        self.sl.log(f"Pre-market: {sym} history failed ({exc}); retrying", logging.WARNING)
                    await asyncio.sleep(self.sl.retry_seconds)
        return out

    async def refresh(self, now: datetime, pre_open: datetime) -> None:
        """Every name's completed 1-minute bars since 04:00 ET: one Alpaca SIP request (the research data)."""
        alpaca = getattr(self.sl.market, "alpaca", None)
        live = [s for s, n in self.names.items() if n.st.status in ("watching",) + ACTIVE]
        if not live:
            return
        if alpaca is not None:
            try:
                df = await alpaca.bars(live, "1Min", pre_open.isoformat(), now.isoformat(), feed="sip", adjustment="raw")
                if len(df):
                    # plain arrays: bar end = start + 1 minute (Alpaca stamps the start); keys are epoch ns
                    ends = df["ts"].to_numpy(dtype="datetime64[ns]").astype("int64") + 60_000_000_000
                    lo_ns, hi_ns = pd.Timestamp(pre_open).value, pd.Timestamp(now).value
                    keep = (ends > lo_ns) & (ends <= hi_ns)
                    syms = df["symbol"].to_numpy()
                    hlcv = df[["high", "low", "close", "volume"]].to_numpy(dtype=float)
                    for sym in set(syms[keep]):
                        if sym in self.names:
                            sel = keep & (syms == sym)
                            self.names[sym].set_bars(dict(zip(ends[sel].tolist(), map(tuple, hlcv[sel].tolist()))), "sip")
                return
            except Exception as exc:  # noqa: BLE001 — fall back to Toss candles
                self.sl.log(f"Pre-market: SIP bars failed ({exc}); using Toss candles", logging.WARNING)
        for s in live:
            await self.names[s].refresh_from_toss(now, pre_open)

    # ── fills and exits ───────────────────────────────────────────────────
    def on_fill(self, order: Order, qty: float, price: float) -> None:
        n = self.names.get(order.symbol)
        if n is not None:
            n.on_fill(order, qty, price)

    async def exit_now(self, reason: str = "manual") -> None:
        for n in self.names.values():
            if n.st.qty > 0:
                await n.exit_now(reason)

    def snapshot(self) -> dict:
        rows = [n.row() for n in self.names.values()]
        return {"enabled": True, "regime": self.regime, "slots": self.cfg.slots,
                "used": len({s for s, n in self.names.items() if n.active()}), "names": rows,
                "config": {k: str(v) for k, v in asdict(self.cfg).items()}}
