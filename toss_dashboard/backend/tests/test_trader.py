import asyncio
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from trader.broker import PaperBroker
from trader.config import Settings
from trader.market import ET, Bar, MarketData
from trader.risk import RiskManager
from trader.strategy import Position, Setup, StrategyConfig, arm, hold_overnight, initial_stop, update_trail
from trader.toss.client import us_price


def test_us_price_truncates_to_tick():
    assert us_price(123.456) == "123.45"
    assert us_price(0.123456) == "0.1234"
    assert us_price(5) == "5.00"


def test_sizing_respects_risk_size_and_cash():
    rm = RiskManager(Settings(risk_per_trade=0.01, max_position_pct=0.35))
    # risk-bound: $30 risk / $0.50 per share = 60, size cap 0.35*3000/50 = 21
    assert rm.shares_for(3000, 3000, entry=50, stop=49.5, commission=0.001) == 21
    # cash-bound
    assert rm.shares_for(3000, 200, entry=50, stop=49.5, commission=0.001) == 3
    # too expensive for one share
    assert rm.shares_for(3000, 3000, entry=1700, stop=1690, commission=0.001) == 0


def test_order_throttle_blocks_entries_not_exits():
    rm = RiskManager(Settings(max_orders_per_10min=2, max_orders_per_day=10))
    assert rm.allow_order() and rm.allow_order()
    assert not rm.allow_order()
    assert rm.allow_order(urgent=True)


def test_daily_loss_halts():
    rm = RiskManager(Settings(daily_loss_limit=0.03))
    rm.new_day(3000)
    rm.check_daily_loss(2950)
    assert not rm.status.halted
    rm.check_daily_loss(2900)
    assert rm.status.halted


def test_arm_requires_green_opening_range():
    cfg = StrategyConfig(require_green=True)
    s = Setup("X", atr=2.0, rvol=3.0, prev_close=10.0, sma50=9.0)
    arm(cfg, s, (10.0, 10.5, 9.8, 9.9))
    assert s.state == "SKIPPED"
    s = Setup("X", atr=2.0, rvol=3.0, prev_close=10.0, sma50=9.0)
    arm(cfg, s, (10.0, 10.5, 9.8, 10.3))
    assert s.state == "ARMED" and s.trigger == 10.51


def test_arm_volatility_trend_and_gap_filters():
    cfg = StrategyConfig(min_atr_pct=0.05, require_trend=True, min_gap=0.02)
    green = (10.5, 11.0, 10.4, 10.9)
    quiet = Setup("Q", atr=0.3, rvol=5.0, prev_close=10.0, sma50=9.0)  # ATR 3%
    arm(cfg, quiet, green)
    assert quiet.state == "SKIPPED" and "ATR" in quiet.note
    downtrend = Setup("D", atr=1.0, rvol=5.0, prev_close=10.0, sma50=11.0)
    arm(cfg, downtrend, green)
    assert downtrend.state == "SKIPPED" and "50-day" in downtrend.note
    flat_open = Setup("F", atr=1.0, rvol=5.0, prev_close=10.4, sma50=9.0)  # gap ~1%
    arm(cfg, flat_open, green)
    assert flat_open.state == "SKIPPED" and "gap" in flat_open.note
    ok = Setup("OK", atr=1.0, rvol=5.0, prev_close=10.0, sma50=9.0)  # gap 5%
    arm(cfg, ok, green)
    assert ok.state == "ARMED"


def test_stop_trail_and_overnight_rules():
    cfg = StrategyConfig(stop_atr=0.5, trail_atr=1.0, exit_mode="hold_strong")
    s = Setup("X", atr=2.0, rvol=3.0)
    stop = initial_stop(cfg, s, 100.0)
    assert stop == 99.0
    pos = Position("X", 10, 100.0, stop, stop, 2.0, datetime.now(ET).isoformat(), highest=100.0)
    update_trail(cfg, pos, 104.0)
    assert pos.stop == 102.0
    update_trail(cfg, pos, 101.0)  # never lowered
    assert pos.stop == 102.0
    assert hold_overnight(cfg, pos, close=103.8, day_high=104.0, day_low=98.0)
    assert not hold_overnight(cfg, pos, close=100.5, day_high=104.0, day_low=98.0)
    assert not hold_overnight(replace(cfg, exit_mode="eod"), pos, close=103.8, day_high=104.0, day_low=98.0)


def test_opening_range_waits_for_window():
    md = MarketData(client=None)
    st = md.get("X")
    day = datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    for k in range(1, 4):
        st.bars[day + timedelta(minutes=k)] = Bar(day + timedelta(minutes=k), 10 + k, 11 + k, 9 + k, 10.5 + k, 100)
    assert st.opening_range(5) is None
    for k in range(4, 7):
        st.bars[day + timedelta(minutes=k)] = Bar(day + timedelta(minutes=k), 10 + k, 11 + k, 9 + k, 10.5 + k, 100)
    o, h, l, c = st.opening_range(5)
    assert (o, h, l, c) == (11, 16, 10, 15.5)


def test_paper_broker_fills_and_cash():
    md = MarketData(client=None)
    md.on_book("X", 99.9, 100, 100.0, 100, datetime.now(ET))
    b = PaperBroker(md, commission=0.001, capital=1000)
    fills = []
    b.on_fill = lambda o, q, px: fills.append((o.side, q, px))

    async def run():
        buy = await b.submit("X", "BUY", 5, 100.2, "entry")
        assert buy.status == "FILLED" and buy.avg_price == 100.0
        resting = await b.submit("X", "SELL", 5, 101.0, "target")
        assert resting.status == "PENDING"
        md.on_book("X", 101.0, 100, 101.1, 100, datetime.now(ET))
        b.on_quote()
        assert resting.status == "FILLED"
        rejected = await b.submit("X", "BUY", 100, 101.1, "entry")
        assert rejected.status == "REJECTED"
        return await b.cash()

    cash = asyncio.run(run())
    assert fills == [("BUY", 5, 100.0), ("SELL", 5, 101.0)]
    assert cash == pytest.approx(1000 - 500 * 1.001 + 505 * (1 - 0.001 - 0.0000206))
