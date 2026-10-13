import asyncio
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from trader.broker import PaperBroker
from trader.config import Settings
from trader.market import ET, MarketData
from trader.research.portfolio import PP, backtest, build
from trader.swing import SwingSleeve

TODAY = date(2026, 10, 9)


def make_sleeve(tmp_path, prices):
    md = MarketData(client=None)
    s = Settings(mode="paper", capital=0.0, swing_capital=1000.0)
    broker = PaperBroker(md, 0.001, 1000.0)

    async def price_fn(symbols):
        for sym in symbols:
            st = md.get(sym)
            st.last, st.last_ts = prices[sym], datetime.now(ET)
        return {sym: prices[sym] for sym in symbols}

    sleeve = SwingSleeve(s, broker, price_fn, lambda msg, lvl=20: None)
    sleeve.path = tmp_path / "swing.json"
    sleeve.holdings, sleeve.cost, sleeve.cash = {}, {}, 1000.0
    broker.on_fill = lambda o, q, px: sleeve.on_fill(o, q, px)
    return sleeve, broker


def run_rebalance(sleeve, broker, weights):
    sleeve.model = {"for_day": TODAY.isoformat(), "weights": weights, "exposure": 1.0, "hedge_on": False}

    async def cash():
        return await broker.cash()

    asyncio.run(sleeve.rebalance(TODAY, cash, lambda urgent: True))


def test_rebalance_buys_targets_then_rotates(tmp_path):
    prices = {"AAA": 10.0, "BBB": 20.0, "CCC": 50.0}
    sleeve, broker = make_sleeve(tmp_path, prices)
    run_rebalance(sleeve, broker, {"AAA": 0.5, "BBB": 0.4})
    assert sleeve.holdings["AAA"] * 10 == pytest.approx(500, rel=0.01)
    assert sleeve.holdings["BBB"] * 20 == pytest.approx(400, rel=0.01)
    assert sleeve.cash == pytest.approx(100, abs=2)
    # month end: AAA drops out, CCC comes in, BBB within bands is left alone
    run_rebalance(sleeve, broker, {"BBB": 0.4, "CCC": 0.5})
    assert "AAA" not in sleeve.holdings
    assert sleeve.holdings["CCC"] * 50 == pytest.approx(0.5 * sleeve.value(), rel=0.02)
    assert sleeve.realized < 0  # only fees were paid on a flat round trip


def test_rebalance_skips_small_drift(tmp_path):
    prices = {"AAA": 10.0}
    sleeve, broker = make_sleeve(tmp_path, prices)
    run_rebalance(sleeve, broker, {"AAA": 0.9})
    before = dict(sleeve.holdings)
    run_rebalance(sleeve, broker, {"AAA": 0.8})  # within the 0.7-1.3 band: no trade
    assert sleeve.holdings == before


def test_reconcile_shrinks_to_account(tmp_path):
    sleeve, _ = make_sleeve(tmp_path, {})
    sleeve.holdings, sleeve.cost = {"AAA": 10.0, "BBB": 5.0}, {"AAA": 100.0, "BBB": 50.0}
    sleeve.reconcile({"AAA": 4.0})
    assert sleeve.holdings == {"AAA": 4.0}
    assert sleeve.cost["AAA"] == pytest.approx(40.0)


def synthetic_market(n_days=600, n_stocks=40, seed=1):
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2023-01-02", periods=n_days)
    rows = []
    for k in range(n_stocks):
        drift = rng.normal(0.0008, 0.0006)
        px = 50 * np.exp(np.cumsum(rng.normal(drift, 0.015, n_days)))
        for d, p in zip(days, px):
            rows.append((f"S{k:02d}", d, p, p * 1.01, p * 0.99, p, 2_000_000))
    stocks = pd.DataFrame(rows, columns=["symbol", "date", "open", "high", "low", "close", "volume"])
    etf_rows = []
    for sym, drift in (("SPY", 0.0004), ("SMH", 0.0006), ("SOXS", -0.002)):
        px = 100 * np.exp(np.cumsum(rng.normal(drift, 0.01, n_days)))
        etf_rows += [(sym, d, p, p * 1.01, p * 0.99, p, 5_000_000) for d, p in zip(days, px)]
    return stocks, pd.DataFrame(etf_rows, columns=stocks.columns)


def test_model_runs_and_respects_limits():
    stocks, etfs = synthetic_market()
    w = build(stocks, etfs, "2023-01-02")
    state: dict = {}
    eq, trades = backtest(w, PP(top_n=5, universe_top=40, min_dollar_vol=1e6, start="2024-01-02"), state)
    assert len(eq) > 200 and (trades["side"] == "BUY").any()
    assert len(state["weights"]) <= 5
    assert sum(state["weights"].values()) <= 1.0001
    assert 0.25 <= state["exposure"] <= 1.0


def test_resume_applies_fills_missed_during_restart(tmp_path):
    sleeve, _ = make_sleeve(tmp_path, {})
    sleeve.pending = {"oid-1": {"symbol": "AAA", "side": "BUY", "applied": 0.0}}

    class FakeClient:
        async def order(self, oid):
            return {"status": "FILLED", "execution": {"filledQuantity": "2.5", "averageFilledPrice": "40"}}

    asyncio.run(sleeve.resume(FakeClient()))
    assert sleeve.holdings["AAA"] == pytest.approx(2.5)
    assert sleeve.cash == pytest.approx(1000 - 100 - 0.1)
    assert sleeve.pending == {}
