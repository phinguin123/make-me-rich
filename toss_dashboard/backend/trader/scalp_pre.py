"""Pre-market edge for the scalp sleeve: buy liquidity-driven SOXL dips below the pre-market VWAP.

Validated in trader/research/scalp_ext.py (2021-2026 SIP 1-minute bars, Toss costs):
  - between 07:00 and 09:00 ET, when SOXL trades `dip` below its pre-market VWAP (dip = 0.4 x SOXL's
    20-day daily volatility, at least 1.5%), buy with a limit order AT that level: we provide liquidity
    to forced sellers in a thin market and never chase
  - take profit with a resting sell limit at entry + dip; otherwise sell at 09:25 ET, before the open
  - one trade per day, both semis regimes (it worked in uptrends and downtrends alike)
  - result per trade after Toss fees: +1.50% if every touch fills (87 trades, t 7.6), +1.02% if we are
    filled only when a 1-minute bar closes below our price (52 trades, t 3.5); 10-15 trades a year,
    both halves of the sample positive, 2026 weaker; worst trade about -3.7%, worst dip inside a trade -11%
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

from .broker import Order
from .journal import jr
from .toss.client import TossAPIError

if TYPE_CHECKING:
    from .scalp import ScalpSleeve

from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SEC_FEE = 0.0000206


@dataclass
class PreConfig:
    start: time = time(7, 0)  # entries allowed from (ET)
    cutoff: time = time(9, 0)  # no new entries after
    exit_at: time = time(9, 25)  # sell what's left before the regular open
    z: float = 0.4  # dip = z x 20-day daily volatility of SOXL
    min_dip: float = 0.015
    max_dip: float = 0.06
    rest_seconds: float = 600  # an unfilled entry rests at the level this long (backtest: 10 minutes)
    max_attempts: int = 2
    through: float = 0.003  # buy only once the price trades this far below the level (as the research required)


@dataclass
class PreState:
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
    SYMBOL = "SOXL"

    def __init__(self, sleeve: "ScalpSleeve"):
        self.sl = sleeve
        self.cfg = PreConfig()
        self.st = PreState()
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
        self.sl.log(f"Pre-market: {msg}", level)

    def value(self) -> float:
        return self.st.qty * (self.sl.market.get(self.SYMBOL).last or self.st.entry) if self.st.qty > 0 else 0.0

    # ── scheduler hook (every engine tick on trading days) ──────────────────
    def tick(self, now: datetime, pre: dict, regular_open: datetime, spawn: Callable) -> None:
        self.now = now
        today = now.date()
        if not self._resumed:
            self._resumed = True
            self._resume(spawn)
        if self._prep_day != today.isoformat() and pre["open"] <= now < regular_open + timedelta(hours=6):
            self._prep_day = today.isoformat()
            self.busy = True
            spawn(self._guard(self.prepare(today, now)))
            return
        if self.busy or self.st.day != today.isoformat():
            return
        start = max(self._at(today, self.cfg.start), pre["open"] + timedelta(minutes=30))
        cutoff = self._at(today, self.cfg.cutoff)
        exit_at = min(self._at(today, self.cfg.exit_at), pre["close"] - timedelta(minutes=4))
        minute = now.replace(second=0, microsecond=0)
        if pre["open"] <= now < cutoff and minute != self._minute and now.second >= 2:
            self._minute = minute
            spawn(self._guard(self.refresh_vwap(now, pre["open"]), hold=False))
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
                spawn(self._guard(self._exit("close" if now < regular_open else "open", market=now >= regular_open)))
            return
        if self.st.status == "watching":
            if now >= cutoff:
                self.st.status, self.st.note = "done", "no dip to the level before 09:00"
                self.sl.save()
            elif now >= start and self.st.level and not self.sl.entries_blocked():
                px = self._fresh()
                if px and px <= self.st.level * (1 - self.cfg.through):
                    self.busy = True
                    spawn(self._guard(self._enter(today)))

    async def _guard(self, coro, hold: bool = True) -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001 — never kill the engine loop
            self._log(f"error {exc!r}", logging.ERROR)
        finally:
            if hold:
                self.busy = False
            self.sl.save()

    def _fresh(self) -> float:
        st = self.sl.market.get(self.SYMBOL)
        if not st.last_ts:
            return 0.0
        return st.last if (datetime.now(ET) - st.last_ts.astimezone(ET)).total_seconds() < 30 else 0.0

    # ── daily setup ───────────────────────────────────────────────────────
    async def prepare(self, today: date, now: datetime) -> None:
        if self.st.day == today.isoformat() and self.st.status not in ("idle",):
            return
        carried = (self.st.qty, self.st.entry) if self.st.qty > 0 else None
        self.st = PreState(day=today.isoformat())
        self.bars.clear()
        if carried:
            self.st.qty, self.st.entry = carried
            self.st.status, self.st.note = "exit_pending", "pre-market position carried from a failed exit; selling now"
            self._log(f"{carried[0]:g} SOXL carried — selling", logging.WARNING)
            return
        if now.time() >= self.cfg.cutoff:
            self.st.status, self.st.note = "done", "started after the 09:00 entry cutoff"
            return
        self.st.status, self.st.note = "standing_aside", "loading SOXL history"
        closes = None
        while closes is None:
            try:
                res = await self.sl.toss.candles(self.SYMBOL, "1d", count=30)
                closes = [float(c["closePrice"]) for c in res.get("candles", [])
                          if datetime.fromisoformat(c["timestamp"]).date() < today][:21]
            except TossAPIError as exc:  # e.g. this machine's IP not yet in the Toss allowlist: keep trying
                if (self.now or datetime.now(ET)).time() >= self.cfg.cutoff:
                    self.st.note = f"SOXL history unavailable before 09:00 ({exc})"
                    return
                if self.st.note == "loading SOXL history":
                    self._log(f"SOXL history failed ({exc}); retrying every minute", logging.WARNING)
                self.st.note = f"SOXL history failed ({exc}); retrying"
                self.sl.save()
                await asyncio.sleep(self.sl.retry_seconds)
        rets = [math.log(a / b) for a, b in zip(closes[:-1], closes[1:]) if a > 0 and b > 0]
        if len(rets) < 15:
            self.st.status, self.st.note = "standing_aside", "not enough SOXL history"
            return
        if max(abs(r) for r in rets) > 0.5:
            self.st.status, self.st.note = "standing_aside", "split or bad price in the last 20 days"
            return
        mean = sum(rets) / len(rets)
        self.st.sigma = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        self.st.dip = min(self.cfg.max_dip, max(self.cfg.min_dip, self.cfg.z * self.st.sigma))
        if self.sl.edge_size("scalp-pre") <= 0:
            self.st.status, self.st.note = "standing_aside", "switched off by the edge monitor (live results far below the backtest)"
            jr("D", "pre", "prepare", {"sigma": self.st.sigma, "dip": self.st.dip, "status": self.st.status})
            return
        self.st.status, self.st.note = "watching", ""
        jr("D", "pre", "prepare", {"sigma": self.st.sigma, "dip": self.st.dip, "status": "watching"})
        self._log(f"watching for SOXL {self.st.dip:.2%} below the pre-market VWAP, 07:00-09:00 ET "
                  f"(20-day vol {self.st.sigma:.1%})")

    async def refresh_vwap(self, now: datetime, pre_open: datetime) -> None:
        """Pre-market VWAP from completed 1-minute bars since 04:00 ET: Alpaca SIP bars (the research data),
        else Toss candles (Toss stamps bars with their end)."""
        alpaca = getattr(self.sl.market, "alpaca", None)
        if alpaca is not None:
            try:
                df = await alpaca.bars([self.SYMBOL], "1Min", pre_open.isoformat(), now.isoformat(), feed="sip", adjustment="raw")
                bars = {}
                for row in df.itertuples(index=False):
                    end = row.ts.tz_convert(ET).to_pydatetime() + timedelta(minutes=1)  # Alpaca stamps the start
                    if pre_open < end <= now:
                        bars[end] = (float(row.high), float(row.low), float(row.close), float(row.volume))
                if bars:
                    self.bars, self.st.source = bars, "sip"
                    self._set_vwap()
                    return
            except Exception as exc:  # noqa: BLE001 — fall back to Toss candles
                self._log(f"SIP bars failed ({exc}); using Toss candles", logging.WARNING)
        if self.st.source == "sip":
            self.bars.clear()  # don't mix sources in one VWAP
        self.st.source = "toss"
        before = None
        backfill = not self.bars
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
                    self.bars[ts] = (float(x["highPrice"]), float(x["lowPrice"]), float(x["closePrice"]), float(x["volume"]))
            before = res.get("nextBefore")
            if not backfill or not cs or not before or (oldest and oldest <= pre_open):
                break
        self._set_vwap()

    def _set_vwap(self) -> None:
        pv = sum((h + l + c) / 3 * v for h, l, c, v in self.bars.values())
        vol = sum(v for *_, v in self.bars.values())
        if vol > 0:
            self.st.vwap = pv / vol
            self.st.level = math.floor(self.st.vwap * (1 - self.st.dip) * 100) / 100 if self.st.dip else 0.0
            jr("D", "pre", "vwap", {"vwap": self.st.vwap, "level": self.st.level, "last": self.sl.market.get(self.SYMBOL).last,
                                    "bars": len(self.bars), "src": self.st.source})

    # ── trading ───────────────────────────────────────────────────────────
    async def _enter(self, today: date) -> None:
        level = self.st.level
        sl = self.sl
        frac = sl.cfg.position_frac * sl.edge_size("scalp-pre")
        qty = math.floor(min(frac * sl.equity(), sl.cash, await sl.broker.cash()) / (level * (1 + sl.broker.commission)))
        if qty < 1 or not sl.allow_order(False):
            self.st.status, self.st.note = "done", "size < 1 share or order throttle"
            return
        self.st.attempts += 1
        self.st.status = "entry_open"
        jr("D", "pre", "trigger", {"level": level, "last": sl.market.get(self.SYMBOL).last, "vwap": self.st.vwap, "qty": qty,
                                   "attempt": self.st.attempts})
        self._log(f"BUY SOXL {qty} @ {level} limit (dip {self.st.dip:.2%} below VWAP {self.st.vwap:.2f})")
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
            self.st.note = "target not placed (order throttle); selling at 09:25"
            return
        jr("D", "pre", "target", {"entry": self.st.entry, "target": self.st.target, "qty": self.st.qty})
        order = await self.sl.broker.submit(self.SYMBOL, "SELL", self.st.qty, self.st.target, "scalp-pre-target")
        if order.status == "REJECTED":
            self.st.note = f"target rejected ({order.error}); selling at 09:25"
            return
        self.target_order, self.st.target_id, self.st.target_filled = order, order.id, 0.0
        self._log(f"take-profit SELL {self.st.qty:g} @ {self.st.target} resting")

    async def _exit(self, reason: str, market: bool = False) -> None:
        sl = self.sl
        self.st.status = "exiting"
        jr("D", "pre", "exit", {"reason": reason, "qty": self.st.qty, "market": market})
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
            self._log(f"SELL SOXL {self.st.qty:g} ({reason})")
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
            self._log(f"BOUGHT SOXL {qty:g} @ {price:.2f}")
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
            sl.st.trades.append({"day": self.st.day, "side": "SOXL", "qty": qty, "entry": round(self.st.entry, 4),
                                 "exit": round(price, 4), "pnl": round(pnl, 2), "reason": f"pre {reason}"})
            self._log(f"SOLD SOXL {qty:g} @ {price:.2f} P&L {pnl:+.2f} ({reason})")
            sl.edge_closed("scalp-pre", pnl, qty, self.st.entry, self.st.qty, side="SOXL", day=self.st.day)
        sl.save()

    def snapshot(self) -> dict:
        return {"enabled": True, "state": asdict(self.st), "config": {k: str(v) for k, v in asdict(self.cfg).items()}}
