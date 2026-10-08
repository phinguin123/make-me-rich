"""Trading engine: schedules the day from the Toss US calendar, runs the scanner and
strategy, routes orders and keeps persistent state for crash recovery.

Only positions opened by the bot are managed. Holdings you bought yourself are
never sold by the engine.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import deque
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .broker import Broker, Order, PaperBroker, TossBroker
from .config import DATA_DIR, SETTINGS, Settings
from .market import MarketData
from .risk import RiskManager
from .scanner import Scanner
from .strategy import Position, Setup, StrategyConfig, arm, hold_overnight, initial_stop, update_trail
from .toss.client import TossAPIError, TossClient, us_price
from .toss.stream import TossStream

LOG = logging.getLogger("engine")
ET = ZoneInfo("America/New_York")
MACRO = ["SPY", "QQQ", "IWM", "SMH", "TLT", "UUP", "GLD", "USO", "VIXY", "IBIT", "HYG"]
MACRO_LABELS = {
    "SPY": "S&P 500", "QQQ": "Nasdaq 100 (NQ proxy)", "IWM": "Russell 2000", "SMH": "Semis",
    "TLT": "20y Treasury (yield ↑ = TLT ↓)", "UUP": "Dollar index", "GLD": "Gold", "USO": "Oil",
    "VIXY": "VIX futures", "IBIT": "Bitcoin ETF", "HYG": "High-yield credit", "BTC": "Bitcoin (24h)",
}


class Engine:
    def __init__(self, settings: Settings = SETTINGS):
        self.s = settings
        self.cfg = StrategyConfig.load()
        self.toss = TossClient()
        self.market = MarketData(self.toss)
        self.stream = TossStream(self.toss, settings.toss_account_seq)
        self.scanner = Scanner(self.toss, top_n=self.cfg.top_n)
        self.risk = RiskManager(settings)
        self.commission = settings.commission_rate
        self.broker: Broker = (
            TossBroker(self.market, self.commission, self.toss)
            if settings.live
            else PaperBroker(self.market, self.commission, settings.capital)
        )
        self.broker.on_fill = self._on_fill
        self.state_path = DATA_DIR / f"state_{self.broker.name}.json"
        self.trades_path = DATA_DIR / f"trades_{self.broker.name}.jsonl"

        self.setups: dict[str, Setup] = {}
        self.positions: dict[str, Position] = {}
        self.realized_pnl = 0.0
        self.recent_trades: deque[dict] = deque(maxlen=200)
        self.events: deque[dict] = deque(maxlen=300)
        self.macro: dict[str, dict] = {}
        self.prev_close: dict[str, float] = {}
        self.warnings: list[str] = []

        self.running = False
        self.paused = False
        self.day: date | None = None
        self.session: dict[str, datetime] | None = None
        self._session_day: date | None = None
        self.scanned = False
        self.eod_done = False
        self._last_minute: datetime | None = None
        self._tasks: set[asyncio.Task] = set()
        self._entering: set[str] = set()
        self._prepare_task: asyncio.Task | None = None
        self._exiting: set[str] = set()
        self._stop = asyncio.Event()

    # ── helpers ───────────────────────────────────────────────────────────
    def log(self, msg: str, level: int = logging.INFO) -> None:
        LOG.log(level, msg)
        self.events.append({"ts": datetime.now(ET).strftime("%H:%M:%S"), "level": logging.getLevelName(level), "msg": msg})

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._task_done)

    def _task_done(self, t: asyncio.Task) -> None:
        self._tasks.discard(t)
        if not t.cancelled() and t.exception():
            self.log(f"task error: {t.exception()!r}", logging.ERROR)

    def unrealized(self) -> float:
        return sum((self.market.get(p.symbol).last or p.entry) * p.qty - p.entry * p.qty for p in self.positions.values())

    def equity(self) -> float:
        return self.s.capital + self.realized_pnl + self.unrealized()

    async def available_cash(self) -> float:
        invested = sum(p.entry * p.qty for p in self.positions.values())
        bot_cash = self.s.capital + self.realized_pnl - invested
        try:
            broker_cash = await self.broker.cash()
        except TossAPIError as exc:
            self.log(f"buying power check failed: {exc}", logging.WARNING)
            return 0.0
        return max(0.0, min(bot_cash, broker_cash))

    # ── persistence ───────────────────────────────────────────────────────
    def _save(self) -> None:
        state = {
            "positions": {k: p.as_dict() for k, p in self.positions.items()},
            "realized_pnl": self.realized_pnl,
            "day": self.day.isoformat() if self.day else None,
            "day_start_equity": self.risk.day_start_equity,
            "paper_cash": getattr(self.broker, "_cash", None),
            "saved": datetime.now(ET).isoformat(),
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(self.state_path)

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        st = json.loads(self.state_path.read_text())
        self.realized_pnl = float(st.get("realized_pnl", 0.0))
        self.positions = {k: Position(**v) for k, v in st.get("positions", {}).items()}
        for p in self.positions.values():
            p.exiting = False
        if isinstance(self.broker, PaperBroker) and st.get("paper_cash") is not None:
            self.broker._cash = float(st["paper_cash"])
        if st.get("day"):
            self.day = date.fromisoformat(st["day"])
            self.risk.day_start_equity = float(st.get("day_start_equity") or self.s.capital)
        if self.trades_path.exists():
            for line in self.trades_path.read_text().splitlines()[-200:]:
                self.recent_trades.append(json.loads(line))
        self.log(f"Restored {len(self.positions)} position(s), realized P&L {self.realized_pnl:+.2f}")

    async def _reconcile_live(self) -> None:
        """Shrink/drop bot positions that no longer exist in the Toss account."""
        if not self.s.live or not self.positions:
            return
        try:
            holdings = {h["symbol"]: float(h["quantity"]) for h in (await self.toss.holdings())["items"]}
        except TossAPIError as exc:
            self.log(f"holdings reconcile failed: {exc}", logging.WARNING)
            return
        for sym, pos in list(self.positions.items()):
            held = holdings.get(sym, 0.0)
            if held + 1e-9 < pos.qty:
                self.log(f"{sym}: account holds {held}, bot expected {pos.qty} — adjusting", logging.WARNING)
                pos.qty = held
                if held <= 0:
                    del self.positions[sym]
        self._save()

    # ── lifecycle ─────────────────────────────────────────────────────────
    async def run(self) -> None:
        self.running = True
        self._stop.clear()
        await self.toss.open()
        self._load()
        await self._check_account()
        await self._reconcile_live()
        self.stream.on_trade = self._on_trade
        self.stream.on_book = self._on_book
        if isinstance(self.broker, TossBroker):
            self.stream.on_order = self.broker.on_order_event
        self.stream.on_reconnect = self.broker.sync
        self._spawn(self.stream.run())
        self._update_subscriptions()
        self.log(f"Engine started in {self.broker.name.upper()} mode, capital ${self.s.capital:,.0f}")
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception as exc:  # noqa: BLE001 — keep the loop alive
                    self.log(f"tick error: {exc!r}", logging.ERROR)
                    await asyncio.sleep(2)
                await asyncio.sleep(0.25)
        finally:
            await self._shutdown()

    async def stop(self) -> None:
        self._stop.set()

    async def _shutdown(self) -> None:
        if self.s.live and self.s.use_server_stops:
            for pos in self.positions.values():
                if not pos.server_stop_id:
                    await self._place_server_stop(pos)
        self.stream.stop()
        for t in list(self._tasks):
            t.cancel()
        self._save()
        await self.toss.close()
        self.running = False
        self.log("Engine stopped")

    async def _check_account(self) -> None:
        try:
            await self.toss.token()
            for c in await self.toss.commissions():
                if c["marketCountry"] == "US":
                    rate = float(c["commissionRate"])
                    if abs(rate - self.commission) > 1e-9:
                        self._warn(f"Toss US commission is {rate:.3%}, config says {self.commission:.3%} — using Toss value")
                        self.commission = self.broker.commission = rate
                    if rate > self.s.max_commission:
                        self.paused = True
                        self._warn(
                            f"US commission {rate:.3%} is above the {self.s.max_commission:.3%} the strategy was validated at — "
                            "new entries paused. Raise TRADER_MAX_COMMISSION only after re-running the backtest at this rate."
                        )
                    if c.get("endDate") and c["endDate"] < "2027":
                        self._warn(f"Toss US commission {rate:.2%} valid until {c['endDate']} — check the rate after that date")
        except TossAPIError as exc:
            self._warn(f"Toss account check failed: {exc}")

    def _warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)
            self.log(msg, logging.WARNING)

    # ── scheduling ────────────────────────────────────────────────────────
    async def _load_session(self, today: date) -> None:
        self._session_day = today
        self.session = None
        try:
            cal = await self.toss.us_calendar(today.isoformat())
            reg = (cal.get("today") or {}).get("regularMarket")
            if reg:
                self.session = {
                    "open": datetime.fromisoformat(reg["startTime"]).astimezone(ET),
                    "close": datetime.fromisoformat(reg["endTime"]).astimezone(ET),
                }
        except TossAPIError as exc:
            self.log(f"calendar fetch failed ({exc}); assuming 09:30-16:00", logging.WARNING)
            if today.weekday() < 5:
                self.session = {
                    "open": datetime.combine(today, time(9, 30), ET),
                    "close": datetime.combine(today, time(16, 0), ET),
                }

    def _new_day(self, today: date) -> None:
        self.day = today
        self.setups.clear()
        self.scanned = False
        self.eod_done = False
        self.market.reset_day()
        self.risk.new_day(self.equity())
        self._spawn(self._check_account())  # the Toss commission rate can change between sessions
        for p in self.positions.values():
            p.last_bar_end = ""
        self._save()
        self.log(f"New session {today}: equity ${self.equity():,.2f}")

    async def _tick(self) -> None:
        now = datetime.now(ET)
        today = now.date()
        if self._session_day != today:
            await self._load_session(today)
            self._spawn(self._load_prev_closes())
        if not self.session:
            await asyncio.sleep(5)
            return
        open_, close_ = self.session["open"], self.session["close"]
        if self.day != today and now >= open_ - timedelta(hours=2):
            self._new_day(today)
        if open_ - timedelta(minutes=90) <= now < close_ and self.scanner.prepared_for != today:
            self._ensure_prepare(today)
        if open_ - timedelta(minutes=1) <= now <= close_ + timedelta(minutes=2):
            minute = now.replace(second=0, microsecond=0)
            if now.second >= 3 and minute != self._last_minute:
                self._last_minute = minute
                self._spawn(self._on_minute(now))
        scan_at = open_ + timedelta(minutes=5, seconds=10)  # ranks on the first 5-minute bar
        if not self.scanned and scan_at <= now < close_ and now.time() < self.cfg.cutoff:
            self.scanned = True
            self._spawn(self._scan(today))
        if open_ <= now < close_:
            if open_ + timedelta(seconds=5) <= now:
                self._cancel_server_stops_at_open()
            self._evaluate(now)
        if not self.eod_done and now >= close_ - timedelta(minutes=3) and now < close_ + timedelta(minutes=1):
            self.eod_done = True
            self._spawn(self._eod())
        if isinstance(self.broker, PaperBroker):
            self.broker.on_quote()

    def _ensure_prepare(self, today: date) -> asyncio.Task:
        """One shared universe-build task per day (the scheduler and the scan both wait on it)."""
        if self._prepare_task is None or (self._prepare_task.done() and self.scanner.prepared_for != today):
            self._prepare_task = asyncio.create_task(self._prepare(today))
        return self._prepare_task

    async def _prepare(self, today: date) -> None:
        try:
            self.log("Preparing universe (daily stats + opening-volume baselines)…")
            await self.scanner.prepare(today)
        except Exception as exc:  # noqa: BLE001
            self.log(f"universe prepare failed: {exc!r}", logging.ERROR)
            await asyncio.sleep(60)  # back off before the scheduler retries

    async def _scan(self, today: date) -> None:
        if self.scanner.prepared_for != today:
            await self._ensure_prepare(today)
        cands = await self.scanner.scan(today)
        for c in cands:
            if c.symbol in self.positions:
                continue
            self.setups[c.symbol] = Setup(c.symbol, atr=c.atr14, rvol=c.rvol, prev_close=c.prev_close, sma50=c.sma50)
        self._update_subscriptions()
        self.log(f"Scan complete: {len(self.setups)} setups — {', '.join(self.setups)}")
        await self._on_minute(datetime.now(ET))

    def _update_subscriptions(self) -> None:
        live_setups = {s for s, st in self.setups.items() if st.state in ("WAIT_OR", "ARMED", "ENTERING")}
        active = live_setups | set(self.positions)
        self.stream.set_symbols(trades=set(MACRO) | active, books=active)
        regime_syms = {"none": set(), "qqq_vwap": {"QQQ"}, "qqq_green": {"QQQ"}, "risk_on": {"QQQ", "VIXY"}}[self.cfg.regime]
        self.market.bar_symbols = active | regime_syms

    async def _load_prev_closes(self) -> None:
        for sym in MACRO:
            try:
                res = await self.toss.candles(sym, "1d", count=3)
                closes = [c for c in res.get("candles", []) if datetime.fromisoformat(c["timestamp"]).date() < datetime.now(ET).date()]
                if closes:
                    self.prev_close[sym] = float(closes[0]["closePrice"])
            except TossAPIError:
                pass

    # ── per-minute work ───────────────────────────────────────────────────
    async def _on_minute(self, now: datetime) -> None:
        await self.market.refresh_bars()
        for setup in self.setups.values():
            if setup.state == "WAIT_OR":
                st = self.market.get(setup.symbol)
                rng = st.opening_range(self.cfg.or_minutes)
                if rng:
                    arm(self.cfg, setup, rng)
                    # Only the first breakout counts (as in the backtest). After a late start
                    # or restart, skip names that already broke out while we were not watching.
                    or_end = self.session["open"] + timedelta(minutes=self.cfg.or_minutes)
                    crossed = [b for b in st.session_bars() if b.end > or_end and b.high >= setup.trigger]
                    if setup.state == "ARMED" and crossed:
                        setup.state, setup.note = "SKIPPED", f"broke out at {crossed[0].end:%H:%M} before arming"
                    detail = f"trigger {setup.trigger}" if setup.state == "ARMED" else setup.note
                    self.log(f"{setup.symbol}: {setup.state} — {detail}")
        for pos in self.positions.values():
            st = self.market.get(pos.symbol)
            for bar in st.session_bars():
                if bar.end.isoformat() > max(pos.last_bar_end, pos.opened):
                    update_trail(self.cfg, pos, bar.high)
                    pos.last_bar_end = bar.end.isoformat()
        self._update_macro()
        self.risk.check_daily_loss(self.equity())
        self._save()

    def _update_macro(self) -> None:
        for sym in MACRO:
            st = self.market.get(sym)
            if st.last:
                pc = self.prev_close.get(sym)
                self.macro[sym] = {"label": MACRO_LABELS[sym], "last": st.last, "chg": (st.last / pc - 1) if pc else None}
        self._spawn(self._btc())

    async def _btc(self) -> None:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                url = "https://data.alpaca.markets/v1beta3/crypto/us/snapshots"
                headers = {"APCA-API-KEY-ID": self.s.alpaca_key, "APCA-API-SECRET-KEY": self.s.alpaca_secret}
                async with s.get(url, params={"symbols": "BTC/USD"}, headers=headers) as r:
                    snap = (await r.json())["snapshots"]["BTC/USD"]
            last = snap["latestTrade"]["p"]
            prev = snap.get("prevDailyBar", {}).get("c")
            self.macro["BTC"] = {"label": MACRO_LABELS["BTC"], "last": last, "chg": (last / prev - 1) if prev else None}
        except Exception:  # noqa: BLE001 — cosmetic only
            pass

    # ── realtime ──────────────────────────────────────────────────────────
    def _on_trade(self, symbol: str, price: float, volume: float, ts: datetime) -> None:
        self.market.on_trade(symbol, price, volume, ts)

    def _on_book(self, symbol: str, bid: float, bsz: float, ask: float, asz: float, ts: datetime) -> None:
        self.market.on_book(symbol, bid, bsz, ask, asz, ts)

    def _fresh_price(self, symbol: str) -> float:
        st = self.market.get(symbol)
        if not st.last_ts:
            return 0.0
        age = (datetime.now(ET) - st.last_ts.astimezone(ET)).total_seconds()
        return st.last if age < 30 else 0.0

    def _evaluate(self, now: datetime) -> None:
        for setup in self.setups.values():
            if setup.state != "ARMED":
                continue
            if now.time() >= self.cfg.cutoff:
                setup.state, setup.note = "SKIPPED", "entry cutoff"
                continue
            px = self._fresh_price(setup.symbol)
            if px and px >= setup.trigger and setup.symbol not in self._entering:
                self._entering.add(setup.symbol)
                setup.state = "ENTERING"
                self._spawn(self._enter(setup))
        for pos in list(self.positions.values()):
            if pos.qty <= 0 or pos.exiting or pos.symbol in self._exiting or pos.symbol in self._entering:
                continue
            px = self._fresh_price(pos.symbol)
            if not px:
                continue
            if px <= pos.stop:
                self._spawn(self._exit(pos, "stop" if pos.stop < pos.entry else "trail"))
            elif self.cfg.target_r and px >= pos.entry + self.cfg.target_r * pos.risk:
                self._spawn(self._exit(pos, "target"))

    def _regime_ok(self) -> bool:
        """Same definitions as the backtest, on the last completed minute bar."""
        if self.cfg.regime == "none":
            return True
        q = self.market.get("QQQ")
        bars = q.session_bars()
        if not bars:
            return False
        if self.cfg.regime == "qqq_green":
            return bars[-1].close > q.day_open()
        above_vwap = bars[-1].close > q.vwap()
        if self.cfg.regime == "qqq_vwap":
            return above_vwap
        v = self.market.get("VIXY")
        vbars = v.session_bars()
        return above_vwap and bool(vbars) and vbars[-1].close < v.day_open()

    # ── entries ───────────────────────────────────────────────────────────
    async def _enter(self, setup: Setup) -> None:
        sym = setup.symbol
        try:
            reason = self._entry_block_reason()
            if reason:
                setup.state, setup.note = "SKIPPED", reason
                self.log(f"{sym}: breakout skipped — {reason}")
                return
            st = self.market.get(sym)
            if st.spread_pct > self.cfg.max_spread:
                setup.state, setup.note = "SKIPPED", f"spread {st.spread_pct:.2%} too wide"
                self.log(f"{sym}: {setup.note}")
                return
            ref = max(st.ask or 0.0, st.last, setup.trigger)
            cap = setup.trigger * (1 + self.cfg.max_chase)
            if ref > cap:
                setup.state, setup.note = "SKIPPED", f"ran {ref / setup.trigger - 1:.2%} past trigger"
                self.log(f"{sym}: {setup.note}")
                return
            limit = math.ceil(min(cap, ref * 1.0015) * 100) / 100
            stop = initial_stop(self.cfg, setup, limit)
            qty = self.risk.shares_for(self.equity(), await self.available_cash(), limit, stop, self.commission)
            if qty < 1:
                setup.state, setup.note = "SKIPPED", "size < 1 share"
                self.log(f"{sym}: skipped — position size under 1 share at ${limit}")
                return
            if not self.risk.allow_order():
                setup.state = "ARMED"
                return
            self.positions[sym] = Position(sym, 0.0, limit, stop, stop, setup.atr, datetime.now(ET).isoformat(), highest=limit, meta={"rvol": setup.rvol, "trigger": setup.trigger})
            order = await self.broker.submit(sym, "BUY", qty, limit, "entry")
            self.log(f"{sym}: BUY {qty} @ ≤{limit} (trigger {setup.trigger}, stop {stop}, rvol {setup.rvol:.1f}x)")
            await self._await_fill(order, timeout=8.0)
            if not order.done:
                if self.risk.allow_order(urgent=True):
                    await self.broker.cancel(order)
                await self._await_fill(order, timeout=3.0)
            pos = self.positions.get(sym)
            if pos is None or pos.qty <= 0:
                self.positions.pop(sym, None)
                setup.state, setup.note = "SKIPPED", f"no fill ({order.status} {order.error})".strip()
                self.log(f"{sym}: entry not filled — {setup.note}")
            else:
                setup.state = "DONE"
                pos.initial_stop = pos.stop = initial_stop(self.cfg, setup, order.avg_price or pos.entry)
                self.log(f"{sym}: LONG {pos.qty} @ {pos.entry:.2f}, stop {pos.stop}")
            self._update_subscriptions()
            self._save()
        finally:
            self._entering.discard(sym)

    def _entry_block_reason(self) -> str:
        if self.paused:
            return "paused"
        if self.risk.status.halted:
            return self.risk.status.reason
        if len([p for p in self.positions.values() if p.qty > 0]) + len(self._entering) - 1 >= self.s.max_positions:
            return "max positions"
        if not self._regime_ok():
            return f"regime off ({self.cfg.regime})"
        return ""

    async def _await_fill(self, order: Order, timeout: float) -> None:
        end = asyncio.get_running_loop().time() + timeout
        while not order.done and asyncio.get_running_loop().time() < end:
            if isinstance(self.broker, PaperBroker):
                self.broker.try_fill(order)
            await asyncio.sleep(0.25)
        if not order.done and isinstance(self.broker, TossBroker):
            await self.broker.sync()

    # ── exits ─────────────────────────────────────────────────────────────
    async def _exit(self, pos: Position, reason: str) -> None:
        sym = pos.symbol
        if pos.exiting or sym in self._exiting:
            return
        pos.exiting = True
        self._exiting.add(sym)
        try:
            if pos.server_stop_id:
                await self._cancel_server_stop(pos)
            qty = pos.qty
            if self.s.live:
                try:
                    qty = min(qty, await self.toss.sellable_quantity(sym))
                except TossAPIError as exc:
                    self.log(f"{sym}: sellable check failed ({exc})", logging.WARNING)
            if qty <= 0:
                self.log(f"{sym}: nothing sellable; dropping position", logging.WARNING)
                self.positions.pop(sym, None)
                return
            self.log(f"{sym}: EXIT ({reason}) {qty} shares, last {self.market.get(sym).last}")
            for attempt, cushion in enumerate((0.003, 0.01, None)):
                if pos.qty <= 0:
                    break
                st = self.market.get(sym)
                ref = st.bid or st.last
                limit = None if cushion is None else max(0.01, math.floor(ref * (1 - cushion) * 100) / 100)
                if not self.risk.allow_order(urgent=True):
                    await asyncio.sleep(5)
                    continue
                order = await self.broker.submit(sym, "SELL", pos.qty, limit, reason)
                await self._await_fill(order, timeout=5.0)
                if not order.done:
                    await self.broker.cancel(order)
                    await self._await_fill(order, timeout=3.0)
            if pos.qty > 0:
                self.log(f"{sym}: exit incomplete, {pos.qty} shares left — will retry", logging.ERROR)
        finally:
            pos.exiting = False
            self._exiting.discard(sym)
            self._update_subscriptions()
            self._save()

    # ── fills ─────────────────────────────────────────────────────────────
    def _on_fill(self, order: Order, qty: float, price: float) -> None:
        pos = self.positions.get(order.symbol)
        if order.side == "BUY":
            if pos is None:
                return
            fee = qty * price * self.commission
            new_qty = pos.qty + qty
            pos.entry = (pos.entry * pos.qty + price * qty + fee) / new_qty if pos.qty else (price * qty + fee) / qty
            pos.qty = new_qty
            pos.highest = max(pos.highest, price)
        else:
            if pos is None:
                return
            proceeds = qty * price * (1 - self.commission - 0.0000206)
            pnl = proceeds - qty * pos.entry
            self.realized_pnl += pnl
            pos.qty = round(pos.qty - qty, 6)
            trade = {
                "symbol": pos.symbol, "qty": qty, "entry": round(pos.entry, 4), "exit": round(price, 4),
                "pnl": round(pnl, 2), "ret": round(proceeds / (qty * pos.entry) - 1, 5), "reason": order.tag,
                "opened": pos.opened, "closed": datetime.now(ET).isoformat(), "overnight": pos.overnight,
                "r_multiple": round((price - pos.entry) / pos.risk, 2) if pos.risk else None,
            }
            self.recent_trades.append(trade)
            with self.trades_path.open("a") as f:
                f.write(json.dumps(trade) + "\n")
            self.log(f"{pos.symbol}: SOLD {qty} @ {price:.2f}  P&L {pnl:+.2f} ({trade['r_multiple']}R)")
            if pos.qty <= 1e-6:
                self.positions.pop(pos.symbol, None)
        self._save()

    # ── end of day ────────────────────────────────────────────────────────
    async def _eod(self) -> None:
        for setup in self.setups.values():
            if setup.state in ("WAIT_OR", "ARMED"):
                setup.state, setup.note = "SKIPPED", "end of day"
        today = datetime.now(ET).date().isoformat()
        for pos in list(self.positions.values()):
            st = self.market.get(pos.symbol)
            carried = pos.overnight and not pos.opened.startswith(today)
            if not carried and hold_overnight(self.cfg, pos, st.last, st.day_high(), st.day_low()) and self.s.hold_overnight:
                pos.overnight = True
                pos.stop = max(pos.stop, round(pos.entry, 2))
                self.log(f"{pos.symbol}: holding overnight (strong close), stop raised to {pos.stop}")
                if self.s.live and self.s.use_server_stops:
                    await self._place_server_stop(pos)
            else:
                await self._exit(pos, "close_d1" if carried else "close")
        self._save()

    # ── server-side protective stops (live only) ──────────────────────────
    async def _place_server_stop(self, pos: Position) -> None:
        try:
            limit = max(0.01, pos.stop * 0.99)
            res = await self.toss.create_stop(pos.symbol, int(pos.qty), pos.stop, limit, f"tbs-{pos.symbol.replace('.', '_')}-{int(datetime.now().timestamp())}")
            pos.server_stop_id = res.get("conditionalOrderId")
            self.log(f"{pos.symbol}: server-side stop placed at {pos.stop}")
        except TossAPIError as exc:
            self.log(f"{pos.symbol}: server stop failed: {exc}", logging.WARNING)
            if exc.code == "condition-already-met":
                await self._exit(pos, "stop")

    async def _cancel_server_stop(self, pos: Position, sid: str | None = None) -> None:
        sid = sid or pos.server_stop_id
        pos.server_stop_id = None
        if not sid:
            return
        try:
            await self.toss.cancel_conditional(sid)
            self.log(f"{pos.symbol}: server-side stop cancelled, bot managing exits")
        except TossAPIError as exc:
            self.log(f"{pos.symbol}: cancel server stop: {exc}", logging.WARNING)

    def _cancel_server_stops_at_open(self) -> None:
        for pos in self.positions.values():
            if pos.server_stop_id and not pos.exiting:
                sid, pos.server_stop_id = pos.server_stop_id, None
                self._spawn(self._cancel_server_stop(pos, sid))

    # ── controls ──────────────────────────────────────────────────────────
    async def flatten(self) -> None:
        self.paused = True
        for pos in list(self.positions.values()):
            await self._exit(pos, "manual")

    def snapshot(self) -> dict[str, Any]:
        now = datetime.now(ET)
        positions = []
        for p in self.positions.values():
            st = self.market.get(p.symbol)
            last = st.last or p.entry
            positions.append({**p.as_dict(), "last": last, "pnl": (last - p.entry) * p.qty, "ret": last / p.entry - 1 if p.entry else 0})
        return {
            "mode": self.broker.name,
            "running": self.running,
            "paused": self.paused,
            "now_et": now.strftime("%Y-%m-%d %H:%M:%S"),
            "session": {k: v.isoformat() for k, v in (self.session or {}).items()},
            "connected": self.stream.connected,
            "equity": self.equity(),
            "capital": self.s.capital,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized(),
            "day_pnl": self.equity() - self.risk.day_start_equity,
            "risk": {"halted": self.risk.status.halted, "reason": self.risk.status.reason, "orders_today": self.risk.orders_today},
            "positions": positions,
            "setups": [s.as_dict() | {"last": self.market.get(s.symbol).last} for s in self.setups.values()],
            "macro": self.macro,
            "trades": list(self.recent_trades)[-50:],
            "orders": [o.as_dict() for o in list(self.broker.orders.values())[-30:]],
            "events": list(self.events)[-150:],
            "config": self.cfg.__dict__,
            "warnings": self.warnings,
        }
