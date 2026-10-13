import asyncio
from datetime import datetime, timedelta

import pytest

from trader.broker import PaperBroker
from trader.config import Settings
from trader.market import ET, MarketData
from trader.scalp import ScalpSleeve

import test_scalp_pre as tp

NAMES = ("AAA", "BBB", "CCC")


class BookToss(tp.FakeToss):
    def __init__(self, qqq_up=True):
        self.qqq_up = qqq_up

    async def candles(self, symbol, interval, count=200, before=None):
        if symbol == "QQQ" and interval == "1d":
            days = [tp.TODAY - timedelta(days=i + 1) for i in range(220)]
            closes = [110.0 if self.qqq_up else 90.0] + [100.0] * 219  # yesterday vs the 200-day average
            return {"candles": [{"timestamp": f"{d.isoformat()}T00:00:00-04:00", "closePrice": str(c)} for d, c in zip(days, closes)]}
        return await super().candles(symbol, interval, count, before)


def make(tmp_path, qqq_up=True, slots=2):
    md = MarketData(client=None)
    s = Settings(mode="paper", capital=0.0, swing_capital=0.0, scalp_capital=3000.0, scalp_force_day="", scalp_position_frac=1.0,
                 pre_names=NAMES, pre_slots=slots, pre_qqq_filter=True)
    broker = PaperBroker(md, 0.001, 3000.0)
    sleeve = ScalpSleeve(s, broker, md, BookToss(qqq_up), lambda urgent: True, lambda msg, lvl=20: None)
    sleeve.path = tmp_path / "scalp.json"
    broker.on_fill = lambda o, q, px: sleeve.on_fill(o, q, px)
    return sleeve, md, broker


def quote(md, sym, px):
    st = md.get(sym)
    st.last, st.last_ts, st.bid, st.ask, st.quote_ts = px, datetime.now(ET), px - 0.01, px + 0.01, datetime.now(ET)


def ready(sleeve, md):
    for s in NAMES:
        quote(md, s, 100.0)
    tp.tick(sleeve, tp.at(7, 29))
    tp.tick(sleeve, tp.at(7, 30))
    return {s: sleeve.pre.names[s].st for s in NAMES}


def test_slots_limit_positions_and_free_up(tmp_path):
    sleeve, md, broker = make(tmp_path, slots=2)
    st = ready(sleeve, md)
    assert all(x.status == "watching" for x in st.values())
    for s in NAMES:
        quote(md, s, round(st[s].level * 0.995, 2))  # all three dip through their level at once
    tp.tick(sleeve, tp.at(7, 31, 10))
    holding = [s for s in NAMES if st[s].qty > 0]
    assert len(holding) == 2 and st["CCC"].status == "watching"  # no third slot
    assert all(abs(st[s].qty * st[s].entry - 1500) < 100 for s in holding)  # each a half of the $3,000 pot
    quote(md, "AAA", st["AAA"].target + 0.05)  # AAA takes profit: a slot frees up
    broker.on_quote()
    tp.tick(sleeve, tp.at(7, 32, 10))
    assert st["AAA"].qty == 0 and st["CCC"].qty > 0


def test_qqq_downtrend_stands_aside(tmp_path):
    sleeve, md, _ = make(tmp_path, qqq_up=False)
    st = ready(sleeve, md)
    assert all(x.status == "standing_aside" and "QQQ" in x.note for x in st.values())
    assert sleeve.pre.regime["ok"] is False


def test_book_state_survives_restart_and_reads_the_old_format(tmp_path):
    sleeve, md, broker = make(tmp_path)
    st = ready(sleeve, md)
    quote(md, "BBB", round(st["BBB"].level * 0.995, 2))
    tp.tick(sleeve, tp.at(7, 31, 10))
    again = ScalpSleeve(sleeve.s, broker, md, BookToss(), lambda urgent: True, lambda msg, lvl=20: None)
    again.path = sleeve.path
    again._load()
    assert again.pre.names["BBB"].st.qty == st["BBB"].qty > 0 and again.pre.names["AAA"].st.qty == 0
    old = {"day": "2026-10-13", "status": "in_trade", "qty": 5.0, "entry": 100.0}  # the single-name format
    again.pre.load(old)
    assert again.pre.names.get("SOXL") is None  # not a book name here: ignored, nothing invented


def test_guard_allows_book_names_for_scalp(tmp_path):
    from trader.guard import OrderGuard
    g = OrderGuard(lambda: 10000.0, lambda s: (100.0, 100.1), lambda: [], now=lambda: tp.at(7, 40), scalp_symbols={"SOXL", "SOXS", "TSLA"})
    assert g.check("TSLA", "BUY", 10, 99.0, "scalp-pre-entry") == ""
    assert "may only trade" in g.check("GME", "BUY", 10, 99.0, "scalp-pre-entry")


def test_snapshot_is_plain_json(tmp_path):
    """The dashboard WebSocket sends the snapshot with json.dumps: no datetime/time objects allowed."""
    import json
    sleeve, md, _ = make(tmp_path)
    ready(sleeve, md)
    json.dumps(sleeve.snapshot())
