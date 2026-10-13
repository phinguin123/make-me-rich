"""Swing sleeve: monthly momentum rotation in liquid large caps with volatility targeting.

The model is `trader/research/portfolio.py` (validated 2019-2026 incl. the 2022 bear market):
  - universe: 1,000 most-traded US stocks (price >= $10, 50-day $ volume >= $30M), above
    their 200-day average, annualized volatility <= 60%
  - rank by IBD-style relative strength, hold the top 10, keep holdings while ranked <= 20
  - rebalance at month end (signal at the close, trade at the next open)
  - scale stock exposure to a 20% volatility target (25%-100% invested)
  - optional SOXS hedge when semis and the broad market break down together (off by default)

Live, the model is re-run every morning on fresh daily bars and the sleeve copies its target
weights. That keeps live trading identical to the backtest, including catch-up after days
the PC was off.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .alpaca import AlpacaData
from .broker import Broker, Order
from .config import DATA_DIR, Settings
from .research.portfolio import PP, backtest, build

LOG = logging.getLogger("swing")
ET = ZoneInfo("America/New_York")
STORE = DATA_DIR / "live" / "swing_daily.parquet"
EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
FUND_RE = re.compile(
    r"\b(?:ETF|ETN|ETP|Fund|Trust|iShares|SPDR|ProShares|Direxion|Invesco|Vanguard|Leveraged|Inverse|"
    r"Ultra|UltraPro|Bull|Bear|2X|3X|-1X|Daily|Portfolio|Index|Notes|Warrant|Rights?|Units?)\b",
    re.IGNORECASE,
)
ETFS = ["SPY", "SMH", "SOXS"]
MODEL_ANCHOR = "2024-01-02"  # the model's simulated track record starts here and extends daily
MIN_TRADE = 25.0


class DailyStore:
    """Local cache of daily bars for every non-fund US stock (+ a few ETFs), kept current."""

    def __init__(self) -> None:
        self.stocks: pd.DataFrame | None = None
        self.etfs: pd.DataFrame | None = None

    def load(self) -> bool:
        if not STORE.exists():
            return False
        d = pd.read_parquet(STORE)
        self.etfs = d[d["symbol"].isin(ETFS)]
        self.stocks = d[~d["symbol"].isin(ETFS)]
        return True

    @property
    def last_date(self) -> pd.Timestamp | None:
        return None if self.stocks is None or self.stocks.empty else self.stocks["date"].max()

    async def update(self, today: date) -> None:
        have = self.load()
        end = datetime.combine(today - timedelta(days=1), datetime.max.time()).replace(microsecond=0)
        end_iso = end.replace(tzinfo=ET).isoformat()
        async with AlpacaData() as api:
            assets = pd.DataFrame(await api.assets())
            assets = assets[(assets["status"] == "active") & assets["exchange"].isin(EXCHANGES)]
            assets = assets[~assets["symbol"].str.contains(r"[^A-Z.]") & ~assets["name"].fillna("").str.contains(FUND_RE)]
            symbols = sorted(set(assets["symbol"]) - set(ETFS))
            full_start = (pd.Timestamp(MODEL_ANCHOR) - pd.Timedelta(days=420)).date().isoformat()
            if have and self.last_date is not None:
                start = (self.last_date - pd.Timedelta(days=7)).date().isoformat()
            else:
                start = full_start
            new_st = await self._fetch(api, symbols, start, end_iso, "split")
            new_etf = await self._fetch(api, ETFS, start if have else full_start, end_iso, "all")
            if have:
                refetch = self._split_suspects(new_st)
                if refetch:
                    LOG.info("Re-downloading %d symbols with changed split adjustments", len(refetch))
                    fresh = await self._fetch(api, refetch, full_start, end_iso, "split")
                    self.stocks = self.stocks[~self.stocks["symbol"].isin(refetch)]
                    new_st = pd.concat([new_st[~new_st["symbol"].isin(refetch)], fresh])
        st = pd.concat([self.stocks, new_st]) if have else new_st
        etf = pd.concat([self.etfs, new_etf]) if have else new_etf
        allbars = pd.concat([st, etf]).drop_duplicates(["symbol", "date"], keep="last").sort_values(["symbol", "date"])
        STORE.parent.mkdir(parents=True, exist_ok=True)
        allbars.to_parquet(STORE)
        self.load()
        LOG.info("Daily store: %d symbols through %s", allbars["symbol"].nunique(), allbars["date"].max().date())

    def _split_suspects(self, new: pd.DataFrame) -> list[str]:
        old = self.stocks.set_index(["symbol", "date"])["close"]
        cmp = new.set_index(["symbol", "date"])["close"].to_frame("new").join(old.rename("old"), how="inner")
        bad = cmp[(cmp["new"] / cmp["old"] - 1).abs() > 0.02]
        return sorted(bad.index.get_level_values(0).unique())

    @staticmethod
    async def _fetch(api: AlpacaData, symbols: list[str], start: str, end: str, adjustment: str) -> pd.DataFrame:
        sem = asyncio.Semaphore(8)

        async def one(batch):
            async with sem:
                return await api.bars(batch, "1Day", start, end, adjustment=adjustment)

        frames = await asyncio.gather(*(one(symbols[i : i + 100]) for i in range(0, len(symbols), 100)))
        d = pd.concat([f for f in frames if not f.empty], ignore_index=True)
        if d.empty:
            return pd.DataFrame(columns=["symbol", "date", "open", "high", "low", "close", "volume"])
        d["date"] = d["ts"].dt.tz_convert(ET).dt.tz_localize(None).dt.normalize()
        return d[["symbol", "date", "open", "high", "low", "close", "volume"]]


def run_model(stocks: pd.DataFrame, etfs: pd.DataFrame, p: PP, today: date) -> dict:
    """Run the validated model through the last close plus a synthetic flat day, so the
    returned weights are the model's book right after it trades at today's open."""
    w = build(stocks, etfs, (pd.Timestamp(MODEL_ANCHOR) - pd.Timedelta(days=400)).date().isoformat())
    last = w["days"][-1]
    nxt = pd.Timestamp(today)
    final_complete = nxt.to_period("M") != last.to_period("M")
    flat = {}
    for k, v in w.items():
        if isinstance(v, pd.DataFrame) and k != "is_stock":
            row = v.iloc[[-1]].copy()
            row.index = pd.DatetimeIndex([nxt])
            flat[k] = pd.concat([v, row])
        else:
            flat[k] = v
    flat["open"].iloc[-1] = flat["close"].iloc[-2]  # trade at the last close level (no P&L noise)
    flat["days"] = flat["close"].index
    state: dict = {}
    eq, trades = backtest(flat, replace(p, start=MODEL_ANCHOR), state, final_complete=final_complete)
    state["model_equity"] = eq
    state["data_through"] = last
    state["rebalanced_today"] = final_complete
    state["risk"] = risk_readings(w)
    return state


def risk_readings(w: dict) -> dict:
    """Market context from the same daily data (shown on the dashboard)."""
    c, s200, s50 = w["close"], w["sma200"], w["sma50"]
    stocks = w["is_stock"].to_numpy()
    liq = (w["dv50"].iloc[-1] >= 20e6).to_numpy() & stocks
    above200 = float((c.iloc[-1] > s200.iloc[-1]).to_numpy()[liq].mean())
    above50 = float((c.iloc[-1] > s50.iloc[-1]).to_numpy()[liq].mean())
    smh = c["SMH"]
    return {
        "breadth_above_200dma": above200,
        "breadth_above_50dma": above50,
        "smh_vs_200dma": float(smh.iloc[-1] / s200["SMH"].iloc[-1] - 1),
        "smh_vs_50dma": float(smh.iloc[-1] / s50["SMH"].iloc[-1] - 1),
        "spy_vs_200dma": float(c["SPY"].iloc[-1] / s200["SPY"].iloc[-1] - 1),
        "spy_off_high": float(c["SPY"].iloc[-1] / c["SPY"].iloc[-252:].max() - 1),
    }


class SwingSleeve:
    def __init__(self, settings: Settings, broker: Broker, prices: Callable[[list[str]], "asyncio.Future"], log: Callable[[str, int], None]):
        self.s = settings
        self.broker = broker
        self.prices = prices  # async fn(symbols) -> {symbol: last price}
        self.log = log
        self.params = replace(PP(), hedge=settings.swing_hedge, capital=settings.swing_capital)
        self.store = DailyStore()
        self.path = DATA_DIR / f"swing_{broker.name}.json"
        self.holdings: dict[str, float] = {}
        self.cost: dict[str, float] = {}
        self.cash = settings.swing_capital
        self.realized = 0.0
        self.last_run: str = ""
        self.model: dict = {}
        self.risk: dict = {}
        self.last_prices: dict[str, float] = {}
        self.pending: dict[str, dict] = {}  # order id -> {symbol, side, applied} for restart recovery
        self.busy = False
        self.force_next = False  # after an allocation change: trade to the new budget with tight bands
        self.fresh = not self.path.exists()  # never ran: owns no money yet (see allocator)
        self._load()

    # ── state ─────────────────────────────────────────────────────────────
    def _load(self) -> None:
        if self.path.exists():
            st = json.loads(self.path.read_text())
            self.holdings, self.cost = st.get("holdings", {}), st.get("cost", {})
            self.cash, self.realized = float(st.get("cash", self.cash)), float(st.get("realized", 0.0))
            self.last_run, self.model = st.get("last_run", ""), st.get("model", {})
            self.pending = st.get("pending", {})
            self.force_next = bool(st.get("force_next", False))

    def save(self) -> None:
        st = {"holdings": self.holdings, "cost": self.cost, "cash": self.cash, "realized": self.realized,
              "last_run": self.last_run, "model": self.model, "pending": self.pending, "force_next": self.force_next,
              "saved": datetime.now(ET).isoformat()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, default=str))
        tmp.replace(self.path)

    def value(self) -> float:
        return self.cash + sum(q * self.last_prices.get(s, self.cost.get(s, 0) / q if q else 0) for s, q in self.holdings.items())

    # ── daily cycle ───────────────────────────────────────────────────────
    async def prepare(self, today: date) -> None:
        """Refresh daily bars and run the model (before the open)."""
        if self.store.last_date is None or self.store.last_date.date() < today - timedelta(days=1):
            await self.store.update(today)
        state = await asyncio.to_thread(run_model, self.store.stocks, self.store.etfs, self.params, today)
        eq = state.pop("model_equity")
        self.risk = state.pop("risk")
        weights = {k: float(v) for k, v in state["weights"].items() if v > 1e-4}
        self.model = {
            "as_of": str(state["data_through"].date()), "for_day": today.isoformat(), "weights": weights,
            "exposure": float(state["exposure"]), "hedge_on": bool(state["hedge_on"]),
            "rebalance_day": bool(state["rebalanced_today"]),
            "model_return_since_anchor": float(eq.iloc[-1] / eq.iloc[0] - 1),
            "model_drawdown": float(eq.iloc[-1] / eq.cummax().iloc[-1] - 1),
        }
        self.save()
        self.log(f"Swing model ({self.model['as_of']}): {len(weights)} names, exposure {self.model['exposure']:.0%}"
                 f"{', SOXS hedge ON' if self.model['hedge_on'] else ''}; rebalance day: {self.model['rebalance_day']}", logging.INFO)

    async def rebalance(self, today: date, broker_cash: Callable[[], "asyncio.Future"], allow_order: Callable[[bool], bool],
                        buys_allowed: Callable[[], bool] = lambda: True) -> None:
        """Bring the sleeve to the model's weights (sells first, then amount-based buys)."""
        if self.busy or self.model.get("for_day") != today.isoformat():
            return
        self.busy = True
        try:
            targets = self.model["weights"]
            symbols = sorted(set(targets) | set(self.holdings))
            self.last_prices.update(await self.prices(symbols))
            total = self.value()
            band = 0.05 if self.force_next else 0.3  # tight once after the allocator changed the budget
            sells, buys = [], []
            for s in symbols:
                px = self.last_prices.get(s)
                if not px:
                    continue
                have = self.holdings.get(s, 0.0) * px
                want = targets.get(s, 0.0) * total
                if want <= 0 and have > 0:
                    sells.append((s, self.holdings[s]))
                elif want > 0 and have > want * (1 + band) and have - want >= MIN_TRADE:
                    sells.append((s, (have - want) / px))
                elif want > 0 and have < want * (1 - band) and want - have >= MIN_TRADE:
                    buys.append((s, want - have))
            if not sells and not buys:
                self.log("Swing: book already matches the model — no trades", logging.INFO)
            for s, qty in sells:
                if allow_order(True):
                    self._track(await self._submit_sell(s, qty))
            if sells:
                await asyncio.sleep(5)
                await self.broker.sync()
            if buys and not buys_allowed():
                self.log(f"Swing: {len(buys)} buys skipped (new entries paused)", logging.WARNING)
                buys = []
            for s, amount in sorted(buys, key=lambda x: -x[1]):
                avail = min(self.cash, await broker_cash()) * 0.995
                amount = min(amount, avail)
                if amount < MIN_TRADE or not allow_order(False):
                    continue
                self._track(await self._submit_buy(s, amount))
            await asyncio.sleep(5)
            await self.broker.sync()
            for oid in [k for k, v in self.pending.items() if self.broker.orders.get(k) and self.broker.orders[k].done]:
                self.pending.pop(oid, None)
            self.last_run = today.isoformat()
            self.force_next = False
            self.save()
        finally:
            self.busy = False

    async def _submit_sell(self, symbol: str, qty: float) -> Order:
        qty = round(min(qty, self.holdings.get(symbol, 0.0)), 6)
        self.log(f"Swing SELL {symbol} {qty:g} sh (model rebalance)", logging.INFO)
        o = await self.broker.submit(symbol, "SELL", qty, None, "swing-sell")
        if o.status == "REJECTED" and int(qty) >= 1 and int(qty) != qty:  # fractional not supported
            self.log(f"Swing: fractional sell rejected ({o.error}); selling {int(qty)} whole shares", logging.WARNING)
            o = await self.broker.submit(symbol, "SELL", int(qty), None, "swing-sell")
        return o

    async def _submit_buy(self, symbol: str, amount: float) -> Order:
        self.log(f"Swing BUY {symbol} ${amount:,.2f} (model weight {self.model['weights'].get(symbol, 0):.1%})", logging.INFO)
        o = await self.broker.submit_amount(symbol, round(amount, 2), "swing-buy")
        px = self.last_prices.get(symbol, 0.0)
        if o.status == "REJECTED" and px and amount >= px * 1.005:  # e.g. fractional trading unavailable
            qty = int(amount // (px * 1.005))
            self.log(f"Swing: amount order rejected ({o.error}); buying {qty} whole shares instead", logging.WARNING)
            o = await self.broker.submit(symbol, "BUY", qty, round(px * 1.003, 2), "swing-buy")
        return o

    def _track(self, order: Order) -> None:
        if order.id and not order.id.startswith("rejected") and not order.done:
            self.pending[order.id] = {"symbol": order.symbol, "side": order.side, "applied": order.filled_qty}
            self.save()

    async def resume(self, client) -> None:
        """After a restart, apply fills of swing orders placed by the previous run."""
        for oid, info in list(self.pending.items()):
            try:
                snap = await client.order(oid)
            except Exception as exc:  # noqa: BLE001
                self.log(f"Swing: could not check order {oid[:10]}: {exc}", logging.WARNING)
                continue
            ex = snap.get("execution") or {}
            cum, avg = float(ex.get("filledQuantity") or 0), float(ex.get("averageFilledPrice") or 0)
            if cum > info["applied"] and avg:
                o = Order(id=oid, client_id="", symbol=info["symbol"], side=info["side"], qty=cum, limit=None, tag="swing-resume")
                self.on_fill(o, cum - info["applied"], avg)
            if snap.get("status") in ("FILLED", "CANCELED", "REJECTED", "REPLACED"):
                self.pending.pop(oid, None)
        self.save()

    # ── fills ─────────────────────────────────────────────────────────────
    def on_fill(self, order: Order, qty: float, price: float) -> None:
        s = order.symbol
        if order.id in self.pending:
            self.pending[order.id]["applied"] = self.pending[order.id].get("applied", 0.0) + qty
        fee = qty * price * self.broker.commission
        if order.side == "BUY":
            self.holdings[s] = round(self.holdings.get(s, 0.0) + qty, 6)
            self.cost[s] = self.cost.get(s, 0.0) + qty * price + fee
            self.cash -= qty * price + fee
        else:
            held = self.holdings.get(s, 0.0)
            basis = self.cost.get(s, 0.0) * (qty / held) if held else 0.0
            proceeds = qty * price - fee - qty * price * 0.0000206
            self.realized += proceeds - basis
            self.cash += proceeds
            self.holdings[s] = round(held - qty, 6)
            self.cost[s] = self.cost.get(s, 0.0) - basis
            if self.holdings[s] <= 1e-6:
                self.holdings.pop(s, None)
                self.cost.pop(s, None)
        self.last_prices[s] = price
        self.log(f"Swing {order.side} filled {s} {qty:g} @ {price:.2f}", logging.INFO)
        self.save()

    def reconcile(self, account: dict[str, float]) -> None:
        for s, q in list(self.holdings.items()):
            held = account.get(s, 0.0)
            if held + 1e-6 < q:
                self.log(f"Swing: account holds {held} {s}, sleeve expected {q} — adjusting", logging.WARNING)
                if held <= 0:
                    self.holdings.pop(s)
                    self.cost.pop(s, None)
                else:
                    self.cost[s] = self.cost.get(s, 0.0) * held / q
                    self.holdings[s] = held
        self.save()

    def snapshot(self) -> dict:
        total = self.value()
        rows = []
        for s in sorted(set(self.holdings) | set(self.model.get("weights", {}))):
            q = self.holdings.get(s, 0.0)
            px = self.last_prices.get(s, 0.0)
            val = q * px
            rows.append({"symbol": s, "qty": q, "price": px, "value": val, "weight": val / total if total else 0,
                         "target": self.model.get("weights", {}).get(s, 0.0),
                         "pnl": val - self.cost.get(s, 0.0) if q else 0.0})
        return {"enabled": True, "capital": self.s.swing_capital, "equity": total, "cash": self.cash,
                "realized": self.realized, "last_run": self.last_run, "model": self.model, "risk": self.risk,
                "holdings": rows, "params": {k: v for k, v in asdict(self.params).items() if k not in ("capital", "start")}}
