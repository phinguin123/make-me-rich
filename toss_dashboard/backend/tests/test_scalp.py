import asyncio
from datetime import date, datetime, timedelta

import pytest

from trader.broker import PaperBroker
from trader.config import Settings
from trader.market import ET, Bar, MarketData
from trader.scalp import ScalpSleeve

TODAY = datetime.now(ET).date()
OPEN = datetime.combine(TODAY, datetime.min.time()).replace(hour=9, minute=30, tzinfo=ET)


class FakeToss:
    def __init__(self, closes):
        self.closes = closes  # newest first, all before today

    async def candles(self, symbol, interval, count=200, before=None):
        days = [TODAY - timedelta(days=i + 1) for i in range(len(self.closes))]
        return {"candles": [{"timestamp": f"{d.isoformat()}T00:00:00-04:00", "closePrice": str(c)} for d, c in zip(days, self.closes)]}


def make(tmp_path, closes):
    md = MarketData(client=None)
    s = Settings(mode="paper", capital=0.0, swing_capital=0.0, scalp_capital=2000.0, scalp_force_day="", scalp_position_frac=0.5)
    broker = PaperBroker(md, 0.001, 2000.0)
    sleeve = ScalpSleeve(s, broker, md, FakeToss(closes), lambda urgent: True, lambda msg, lvl=20: None)
    sleeve.path = tmp_path / "scalp.json"
    broker.on_fill = lambda o, q, px: sleeve.on_fill(o, q, px)
    return sleeve, md


def quote(md, sym, px):
    st = md.get(sym)
    st.last, st.last_ts, st.bid, st.ask, st.quote_ts = px, datetime.now(ET), px - 0.01, px + 0.01, datetime.now(ET)


def opening_range(md, low=100.0, high=104.0):
    st = md.get("SOXL")
    for k in range(1, 31):
        end = OPEN + timedelta(minutes=k)
        st.bars[end] = Bar(end, 102, high if k == 10 else 102.5, low if k == 20 else 101.5, 102, 1000)


def run(sleeve, now):
    tasks = []
    sleeve.evaluate(now, OPEN, tasks.append)

    async def go():
        for t in tasks:
            await t

    asyncio.run(go())


def test_bull_regime_stands_aside(tmp_path):
    sleeve, md = make(tmp_path, [110] + [100] * 49)  # SOXX above its 50-day average
    asyncio.run(sleeve.prepare(TODAY))
    assert sleeve.st.regime == "bull" and sleeve.st.status == "standing_aside"
    opening_range(md)
    sleeve.on_minute(OPEN + timedelta(minutes=31), OPEN)
    quote(md, "SOXL", 110)
    run(sleeve, OPEN + timedelta(minutes=40))
    assert sleeve.st.qty == 0


def test_bear_up_break_buys_soxl_then_stops_out(tmp_path):
    sleeve, md = make(tmp_path, [90] + [100] * 49)
    asyncio.run(sleeve.prepare(TODAY))
    assert sleeve.st.regime == "bear"
    opening_range(md)
    sleeve.on_minute(OPEN + timedelta(minutes=31), OPEN)
    assert sleeve.st.status == "armed" and sleeve.st.or_high == 104 and sleeve.st.or_low == 100
    quote(md, "SOXL", 104.2)
    run(sleeve, OPEN + timedelta(minutes=35))
    assert sleeve.st.side == "SOXL" and sleeve.st.qty == pytest.approx(9)  # 50% of $2,000 at ~$104
    quote(md, "SOXL", 99.9)  # back below the range low -> stop
    run(sleeve, OPEN + timedelta(minutes=60))
    assert sleeve.st.qty == 0 and sleeve.st.status == "done"
    assert sleeve.st.trades[-1]["reason"] == "stop" and sleeve.realized < 0


def test_bear_down_break_buys_soxs(tmp_path):
    sleeve, md = make(tmp_path, [90] + [100] * 49)
    asyncio.run(sleeve.prepare(TODAY))
    opening_range(md)
    sleeve.on_minute(OPEN + timedelta(minutes=31), OPEN)
    quote(md, "SOXL", 99.5)
    quote(md, "SOXS", 40.0)
    run(sleeve, OPEN + timedelta(minutes=33))
    assert sleeve.st.side == "SOXS" and sleeve.st.qty == pytest.approx(24)
    asyncio.run(sleeve.eod())
    assert sleeve.st.qty == 0 and sleeve.st.trades[-1]["reason"] == "close"


def test_no_entry_after_cutoff(tmp_path):
    sleeve, md = make(tmp_path, [90] + [100] * 49)
    asyncio.run(sleeve.prepare(TODAY))
    opening_range(md)
    sleeve.on_minute(OPEN + timedelta(minutes=31), OPEN)
    quote(md, "SOXL", 105)
    run(sleeve, OPEN + timedelta(minutes=121))
    assert sleeve.st.qty == 0 and sleeve.st.status == "done"


def test_carried_position_is_sold_next_open(tmp_path):
    sleeve, md = make(tmp_path, [90] + [100] * 49)
    sleeve.st.day, sleeve.st.side, sleeve.st.qty, sleeve.st.entry = (TODAY - timedelta(days=1)).isoformat(), "SOXS", 10.0, 40.0
    sleeve.cash = 1600.0
    asyncio.run(sleeve.prepare(TODAY))
    assert sleeve.st.status == "exit_pending" and sleeve.st.qty == 10
    quote(md, "SOXS", 41.0)
    run(sleeve, OPEN + timedelta(minutes=1))
    assert sleeve.st.qty == 0 and sleeve.st.trades[-1]["reason"] == "carry"


def test_pause_blocks_entries_but_not_exits(tmp_path):
    sleeve, md = make(tmp_path, [90] + [100] * 49)
    paused = {"on": True}
    sleeve.entries_blocked = lambda: paused["on"]
    asyncio.run(sleeve.prepare(TODAY))
    opening_range(md)
    sleeve.on_minute(OPEN + timedelta(minutes=31), OPEN)
    quote(md, "SOXL", 104.2)
    run(sleeve, OPEN + timedelta(minutes=35))
    assert sleeve.st.qty == 0 and sleeve.st.status == "armed"  # paused: no entry, still watching
    paused["on"] = False
    run(sleeve, OPEN + timedelta(minutes=36))
    assert sleeve.st.side == "SOXL" and sleeve.st.qty > 0
    paused["on"] = True
    quote(md, "SOXL", 99.0)
    run(sleeve, OPEN + timedelta(minutes=50))
    assert sleeve.st.qty == 0  # exits still work while paused


def test_one_day_override_trades_in_uptrend(tmp_path):
    sleeve, md = make(tmp_path, [110] + [100] * 49)
    sleeve.s = Settings(mode="paper", scalp_capital=2000.0, scalp_force_day=TODAY.isoformat())
    asyncio.run(sleeve.prepare(TODAY))
    assert sleeve.st.regime == "bull" and sleeve.st.status == "waiting"
    sleeve.s = Settings(mode="paper", scalp_capital=2000.0, scalp_force_day=(TODAY - timedelta(days=1)).isoformat())
    sleeve.st.day = ""
    asyncio.run(sleeve.prepare(TODAY))
    assert sleeve.st.status == "standing_aside"  # override for another day has no effect


def test_single_fat_finger_print_does_not_stop_out(tmp_path):
    sleeve, md = make(tmp_path, [90] + [100] * 49)
    asyncio.run(sleeve.prepare(TODAY))
    opening_range(md)
    sleeve.on_minute(OPEN + timedelta(minutes=31), OPEN)
    quote(md, "SOXL", 104.2)
    run(sleeve, OPEN + timedelta(minutes=35))
    assert sleeve.st.side == "SOXL" and sleeve.st.qty > 0
    st = md.get("SOXL")
    st.last, st.last_ts = 92.0, datetime.now(ET)  # one bad print far below the range, bid/ask still normal
    st.bid, st.ask, st.quote_ts = 104.1, 104.3, datetime.now(ET)
    t0 = OPEN + timedelta(minutes=40)
    run(sleeve, t0)
    assert sleeve.st.qty > 0  # ignored: the quote doesn't confirm it
    st.last = 104.15  # price is back above: the crossing resets
    run(sleeve, t0 + timedelta(seconds=1))
    st.last = 99.5  # a real break: trades stay below for 3 seconds even if quotes are stale
    st.quote_ts = datetime.now(ET) - timedelta(seconds=30)
    run(sleeve, t0 + timedelta(seconds=2))
    assert sleeve.st.qty > 0
    run(sleeve, t0 + timedelta(seconds=5.5))
    assert sleeve.st.qty == 0 and sleeve.st.trades[-1]["reason"] == "stop"


class FakeTossStops(FakeToss):
    def __init__(self, closes):
        super().__init__(closes)
        self.stops, self.cancelled, self.fired, self.left = {}, [], False, None

    async def create_stop(self, symbol, quantity, trigger, limit, cid):
        self.stops["c1"] = (symbol, quantity, trigger, limit)
        return {"conditionalOrderId": "c1"}

    async def cancel_conditional(self, sid):
        from trader.toss.client import TossAPIError
        if self.fired:
            raise TossAPIError(400, "already-executed", "done")
        self.cancelled.append(sid)

    async def conditional_orders(self, status="OPEN"):
        return [] if self.fired else [{"conditionalOrderId": "c1"}]

    async def sellable_quantity(self, symbol):
        return self.left


def live_sleeve(tmp_path):
    sleeve, md = make(tmp_path, [90] + [100] * 49)
    sleeve.toss = FakeTossStops([90] + [100] * 49)
    sleeve.s = Settings(mode="live", scalp_capital=2000.0, scalp_force_day="", scalp_position_frac=0.5)
    asyncio.run(sleeve.prepare(TODAY))
    opening_range(md)
    sleeve.on_minute(OPEN + timedelta(minutes=31), OPEN)
    quote(md, "SOXL", 104.2)
    run(sleeve, OPEN + timedelta(minutes=35))  # 10:05: entry, too early for the backup stop
    assert sleeve.st.qty > 0 and not sleeve.st.backup_id
    run(sleeve, OPEN + timedelta(minutes=16 + 30))  # 10:16 > 09:45: backup placed
    return sleeve, md


def test_backup_stop_far_below_and_removed_before_the_bot_sells(tmp_path):
    sleeve, md = live_sleeve(tmp_path)
    sym, qty, trigger, limit = sleeve.toss.stops["c1"]
    assert sleeve.st.backup_id == "c1" and sym == "SOXL" and qty == int(sleeve.st.qty)
    assert abs(trigger / sleeve.st.entry - 0.90) < 0.002 and limit < trigger
    asyncio.run(sleeve.eod())
    assert sleeve.toss.cancelled == ["c1"] and sleeve.st.qty == 0 and sleeve.st.trades[-1]["reason"] == "close"


def test_backup_stop_that_already_fired_is_booked_not_sold_twice(tmp_path):
    sleeve, md = live_sleeve(tmp_path)
    sleeve.toss.fired, sleeve.toss.left = True, 0.0  # the bot was down; Toss sold at the trigger
    sells = []
    orig = sleeve.broker.submit

    async def spy(*a, **k):
        sells.append(a)
        return await orig(*a, **k)

    sleeve.broker.submit = spy
    asyncio.run(sleeve.eod())
    assert sleeve.st.qty == 0 and sleeve.st.trades[-1]["reason"] == "backup-stop" and not sells


def test_no_backup_stop_in_paper_mode(tmp_path):
    sleeve, md = make(tmp_path, [90] + [100] * 49)
    sleeve.toss = FakeTossStops([90] + [100] * 49)
    asyncio.run(sleeve.prepare(TODAY))
    opening_range(md)
    sleeve.on_minute(OPEN + timedelta(minutes=31), OPEN)
    quote(md, "SOXL", 104.2)
    run(sleeve, OPEN + timedelta(minutes=35))
    run(sleeve, OPEN + timedelta(minutes=60))
    assert sleeve.st.qty > 0 and not sleeve.toss.stops
