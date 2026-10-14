"""A restart right after the pre-market entry cutoff (09:01 ET / 22:01 KST) with a book position still held."""
import asyncio
from dataclasses import replace

from trader.broker import PaperBroker
from trader.scalp import ScalpSleeve

import test_scalp_pre as tp


def test_held_pre_market_position_survives_a_restart_after_the_cutoff(tmp_path):
    sleeve, md, broker = tp.make(tmp_path)
    pre = tp.ready(sleeve, md)
    tp.quote(md, round(pre.level * 0.995, 2))
    tp.tick(sleeve, tp.at(7, 31, 10))
    assert pre.status == "in_trade" and pre.qty == 10 and pre.target_id
    cash_after_buy, target_id = sleeve.cash, pre.target_id

    # the container restarts at 09:01 ET: a new process, an empty broker, the live re-attach path
    broker2 = PaperBroker(md, 0.001, 2000.0)
    broker2.name = "live"
    synced = []

    async def sync():
        synced.append(True)

    broker2.sync = sync
    again = ScalpSleeve(sleeve.s, broker2, md, tp.FakeToss(), lambda urgent: True, lambda msg, lvl=20: None)
    again.path = sleeve.path
    again._load()
    broker2.on_fill = lambda o, q, px: again.on_fill(o, q, px)
    again.st.day, again.st.regime, again.st.status = tp.TODAY.isoformat(), "bull", "standing_aside"
    tp.quote(md, pre.level + 0.2)
    tp.tick(again, tp.at(9, 1, 5))  # the book re-prepares (state kept)
    tp.tick(again, tp.at(9, 1, 20))  # the name resumes
    st, target = again.pre.names["SOXL"].st, again.pre.names["SOXL"].target_order
    assert st.status == "in_trade" and st.qty == 10
    assert target is not None and target.id == target_id and target in broker2.open_orders() and synced  # same Toss order, re-read
    assert again.pre.reserved_cash() == {}  # nothing resting to reserve after the cutoff
    assert abs(again.free_cash() - cash_after_buy) < 1e-9  # the held position's money stays out of the relay's free cash
    assert abs(again.equity() - (cash_after_buy + 10 * md.get("SOXL").last)) < 1e-6
    assert again.pre.exit_time(tp.TODAY) == tp.at(10, 30, 0)  # uptrend day: still held to 10:30

    broker2._apply_fill(target, 10, st.target)  # the take-profit fills after the restart (or while down: sync reads it)
    assert st.qty == 0 and st.status == "done" and again.realized > 0
    assert again.st.trades[-1]["reason"] == "pre target"


def test_startup_cleanup_keeps_the_books_resting_take_profit(tmp_path):
    from test_relay_live import make

    e = make(tmp_path)
    e.s = replace(e.s, mode="live")
    n = e.scalp.pre.names["SOXL"]
    n.st.status, n.st.qty, n.st.target_id = "in_trade", 10, "T1"
    cancelled = []

    class Toss:
        async def conditional_orders(self, status):
            return {"conditionalOrders": []}

        async def orders(self, status):
            return [{"orderId": "T1", "clientOrderId": "tb-1", "side": "SELL", "symbol": "SOXL", "quantity": 10},  # the book's target
                    {"orderId": "O2", "clientOrderId": "tb-2", "side": "BUY", "symbol": "SOXL", "quantity": 5},  # a real orphan
                    {"orderId": "U3", "clientOrderId": "", "side": "BUY", "symbol": "IONQ", "quantity": 1}]  # yours, from the app

        async def cancel_order(self, oid):
            cancelled.append(oid)

        async def cancel_conditional(self, cid):
            cancelled.append(cid)

    e.toss = Toss()
    asyncio.run(e._cleanup_orphans())
    assert cancelled == ["O2"]
