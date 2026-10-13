import asyncio
from datetime import date, datetime

import pytest

from trader.allocator import Allocator
from trader.config import Settings
from trader.market import ET
from trader.swing import SwingSleeve

TARGETS = {"scalp": 0.3, "swing": 0.7}


def make(tmp_path, scalp_fresh=False, swing_fresh=True):
    import test_scalp_pre as tp

    sleeve, md, broker = tp.make(tmp_path)
    sleeve.cash, sleeve.fresh = 12400.0, scalp_fresh

    async def prices(symbols):
        return {s: 100.0 for s in symbols}

    swing = SwingSleeve(Settings(mode="paper", swing_capital=8680.0), broker, prices, lambda msg, lvl=20: None)
    swing.path, swing.fresh = tmp_path / "swing.json", swing_fresh
    swing.holdings, swing.cost, swing.cash, swing.model = {}, {}, 8680.0, {}
    alloc = Allocator(TARGETS, sleeve, swing, broker, tmp_path / "alloc.json", lambda msg, lvl=20: None)
    return alloc, sleeve, swing


def tick(alloc, when: datetime):
    tasks = []
    alloc.tick(when, when.replace(hour=9, minute=30), tasks.append)

    async def go():
        for t in tasks:
            await t

    asyncio.run(go())


def test_first_switch_counts_only_money_the_sleeves_own(tmp_path):
    alloc, scalp, swing = make(tmp_path)  # scalp has run (owns $12,400); swing never ran
    tick(alloc, datetime(2026, 10, 13, 1, 0, tzinfo=ET))
    assert scalp.cash == pytest.approx(3720.0) and swing.cash == pytest.approx(8680.0)
    assert swing.force_next and alloc.due(date(2026, 10, 13)) == ""
    assert alloc.state["history"][-1]["total"] == pytest.approx(12400.0)


def test_waits_while_a_trade_is_open(tmp_path):
    alloc, scalp, swing = make(tmp_path)
    scalp.st.qty = 10
    tick(alloc, datetime(2026, 10, 13, 10, 0, tzinfo=ET))
    assert scalp.cash == 12400.0 and "waiting" in alloc.note
    scalp.st.qty = 0
    tick(alloc, datetime(2026, 10, 13, 10, 1, tzinfo=ET))
    assert scalp.cash == pytest.approx(3720.0)


def test_yearly_rebalance_restores_targets_after_drift(tmp_path):
    alloc, scalp, swing = make(tmp_path, swing_fresh=False)
    alloc.state = {"targets": dict(TARGETS), "last": "2026-10-13", "history": []}
    scalp.cash = 5000.0  # scalp did well
    swing.holdings, swing.cost, swing.cash = {"AAA": 90.0}, {"AAA": 8000.0}, 1000.0  # 90 x $100 + $1,000 = $10,000
    assert alloc.due(date(2026, 12, 31)) == ""
    tick(alloc, datetime(2027, 1, 4, 8, 0, tzinfo=ET))  # first trading day, before 09:26: wait
    assert scalp.cash == 5000.0 and "09:26" in alloc.note
    tick(alloc, datetime(2027, 1, 4, 9, 27, tzinfo=ET))
    assert scalp.cash == pytest.approx(4500.0)  # 30% of $15,000
    assert swing.value() == pytest.approx(10500.0) and swing.cash == pytest.approx(1500.0)
    assert swing.holdings == {"AAA": 90.0} and swing.force_next  # cash moved, nothing sold by the allocator
    assert alloc.state["last"] == "2027-01-04" and alloc.due(date(2027, 6, 1)) == ""


def test_swing_uses_tight_bands_once_after_allocation(tmp_path):
    alloc, scalp, swing = make(tmp_path, swing_fresh=False)
    swing.holdings, swing.cost, swing.cash = {"AAA": 90.0}, {"AAA": 8000.0}, 1500.0
    swing.last_prices = {"AAA": 100.0}
    today = date(2027, 1, 4)
    swing.model = {"for_day": today.isoformat(), "weights": {"AAA": 1.0}}
    bought = []

    async def submit_amount(symbol, amount, tag):
        bought.append((symbol, amount))
        from trader.broker import Order
        return Order(id="x", client_id="", symbol=symbol, side="BUY", qty=0, limit=None, tag=tag, status="FILLED")

    swing.broker.submit_amount = submit_amount

    async def cash():
        return 100000.0

    swing.force_next = True
    asyncio.run(swing.rebalance(today, cash, lambda urgent: True))
    assert bought and bought[0][0] == "AAA" and bought[0][1] == pytest.approx(1500.0 * 0.995, rel=0.01)
    assert not swing.force_next  # back to normal bands afterwards
