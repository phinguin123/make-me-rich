import asyncio
from datetime import datetime, timedelta

from trader.broker import PaperBroker
from trader.config import Settings
from trader.market import ET, MarketData
from trader.scalp import ScalpSleeve

TODAY = datetime.now(ET).date()


def at(h, m, s=5):
    return datetime.combine(TODAY, datetime.min.time()).replace(hour=h, minute=m, second=s, tzinfo=ET)


PRE = {"open": at(4, 0, 0), "close": at(9, 30, 0)}
OPEN = at(9, 30, 0)


class FakeToss:
    """SOXL daily closes alternating +-5% (20-day vol ~5% -> dip ~2%) and flat pre-market bars at 100."""

    async def candles(self, symbol, interval, count=200, before=None):
        if interval == "1d":
            closes, px = [], 100.0
            for i in range(30):
                closes.append(px)
                px *= 1.05 if i % 2 else 1 / 1.05
            days = [TODAY - timedelta(days=i + 1) for i in range(30)]
            return {"candles": [{"timestamp": f"{d.isoformat()}T00:00:00-04:00", "closePrice": str(c)} for d, c in zip(days, closes)]}
        bars = []
        end = at(4, 1, 0)
        while end <= at(9, 0, 0):
            bars.append({"timestamp": end.isoformat(), "highPrice": "100.2", "lowPrice": "99.8", "closePrice": "100", "volume": "1000"})
            end += timedelta(minutes=1)
        return {"candles": list(reversed(bars))}


def make(tmp_path):
    md = MarketData(client=None)
    s = Settings(mode="paper", capital=0.0, swing_capital=0.0, scalp_capital=2000.0, scalp_force_day="", scalp_position_frac=0.5,
                 pre_names=("SOXL",), pre_slots=1, pre_qqq_filter=False)
    broker = PaperBroker(md, 0.001, 2000.0)
    sleeve = ScalpSleeve(s, broker, md, FakeToss(), lambda urgent: True, lambda msg, lvl=20: None)
    sleeve.path = tmp_path / "scalp.json"
    broker.on_fill = lambda o, q, px: sleeve.on_fill(o, q, px)
    return sleeve, md, broker


def quote(md, px):
    st = md.get("SOXL")
    st.last, st.last_ts, st.bid, st.ask = px, datetime.now(ET), px - 0.01, px + 0.01


def tick(sleeve, now):
    tasks = []
    sleeve.pre.tick(now, PRE, OPEN, tasks.append)

    async def go():
        for t in tasks:
            await t

    asyncio.run(go())


def ready(sleeve, md):
    quote(md, 100.0)
    tick(sleeve, at(7, 29))  # prepare: volatility and dip size
    tick(sleeve, at(7, 30))  # first VWAP refresh
    pre = sleeve.pre.names["SOXL"].st
    assert pre.status == "watching" and 0.019 < pre.dip < 0.022
    assert abs(pre.vwap - 100.0) < 1e-9 and pre.level == round(int(100 * (1 - pre.dip) * 100) / 100, 2)
    return pre


def test_dip_buys_at_level_and_takes_profit(tmp_path):
    sleeve, md, broker = make(tmp_path)
    pre = ready(sleeve, md)
    quote(md, round(pre.level * 0.995, 2))
    tick(sleeve, at(7, 31, 10))
    assert pre.status == "in_trade" and pre.qty == 10  # 50% of $2,000 at ~$98
    assert pre.target > pre.entry * (1 + pre.dip) - 0.01
    assert sleeve.equity() < 2000.0 + 1e-6
    quote(md, pre.target + 0.05)
    broker.on_quote()
    assert pre.qty == 0 and pre.status == "done" and sleeve.realized > 0
    assert sleeve.st.trades[-1]["reason"] == "pre target"


def test_unfilled_target_sold_before_the_open(tmp_path):
    sleeve, md, broker = make(tmp_path)
    pre = ready(sleeve, md)
    quote(md, round(pre.level * 0.995, 2))
    tick(sleeve, at(7, 31, 10))
    assert pre.qty == 10
    quote(md, pre.level + 0.2)
    tick(sleeve, at(9, 25, 1))
    assert pre.qty == 0 and pre.status == "done"
    assert sleeve.st.trades[-1]["reason"] == "pre close"
    assert not broker.open_orders()  # take-profit was cancelled


def test_no_entry_before_seven_when_paused_or_after_cutoff(tmp_path):
    sleeve, md, _ = make(tmp_path)
    pre = ready(sleeve, md)
    quote(md, round(pre.level * 0.995, 2))
    tick(sleeve, at(6, 59))
    assert pre.qty == 0 and pre.status == "watching"
    sleeve.entries_blocked = lambda: True
    tick(sleeve, at(7, 40))
    assert pre.qty == 0 and pre.status == "watching"
    sleeve.entries_blocked = lambda: False
    quote(md, 100.0)
    tick(sleeve, at(9, 0, 30))
    assert pre.qty == 0 and pre.status == "done"


def test_unfilled_entry_is_cancelled_and_rearmed(tmp_path):
    sleeve, md, broker = make(tmp_path)
    pre = ready(sleeve, md)
    sleeve.pre.cfg.rest_seconds = 0.6
    st = md.get("SOXL")
    st.last, st.last_ts, st.bid, st.ask = round(pre.level * 0.996, 2), datetime.now(ET), round(pre.level * 0.995, 2), pre.level + 0.10  # a print through the level, ask above our limit
    tick(sleeve, at(7, 45))
    assert pre.status == "entry_open" and pre.attempts == 1 and broker.open_orders()  # resting at the level
    tick(sleeve, at(7, 46))  # its time is up: cancelled, watching again
    assert pre.qty == 0 and pre.status == "watching" and not broker.open_orders()
    tick(sleeve, at(7, 50))
    tick(sleeve, at(7, 51))
    assert pre.status == "done" and pre.attempts == 2  # second miss: done for the day


def test_state_survives_restart(tmp_path):
    sleeve, md, broker = make(tmp_path)
    pre = ready(sleeve, md)
    quote(md, round(pre.level * 0.995, 2))
    tick(sleeve, at(7, 31, 10))
    again = ScalpSleeve(sleeve.s, broker, md, FakeToss(), lambda urgent: True, lambda msg, lvl=20: None)
    again.path = sleeve.path
    again._load()
    assert again.pre.names["SOXL"].st.qty == 10 and again.pre.names["SOXL"].st.status == "in_trade"
    assert abs(again.equity() - sleeve.equity()) < 1e-6


def test_history_failure_is_retried(tmp_path):
    from trader.toss.client import TossAPIError

    sleeve, md, _ = make(tmp_path)
    sleeve.retry_seconds = 0.01
    good, calls = sleeve.toss.candles, {"n": 0}

    async def flaky(symbol, interval, count=200, before=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise TossAPIError(403, "access_denied", "IP address not allowed")
        return await good(symbol, interval, count, before)

    sleeve.toss.candles = flaky
    tick(sleeve, at(7, 29))
    assert sleeve.pre.names["SOXL"].st.status == "watching" and calls["n"] == 3
