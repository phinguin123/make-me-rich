from datetime import datetime

from trader.broker import Order
from trader.market import ET, MarketData
from trader.replay import ReplayBroker, path


def test_path_walks_through_levels_in_small_steps():
    p = path((100.0, 99.0, 101.0, 100.5))
    assert p[0] == 100.0 and p[-1] == 100.5 and min(p) == 99.0 and max(p) == 101.0
    assert max(abs(b / a - 1) for a, b in zip(p[:-1], p[1:])) <= 0.0035  # <= 2.02% / 6 steps


def test_replay_fills_follow_the_research_rules():
    md = MarketData(client=None)
    b = ReplayBroker(md, 0.001, 100000.0)
    st = md.get("SOXL")
    st.last, st.bid, st.ask, st.last_ts = 100.0, 99.99, 100.01, datetime.now(ET)
    import asyncio
    resting = asyncio.run(b.submit("SOXL", "BUY", 10, 99.99, "scalp-pre-entry"))  # below the ask: rests
    assert not resting.done
    st.last, st.bid, st.ask = 99.99, 99.98, 100.0
    b.try_fill(resting)
    assert not resting.done  # a touch of the level is not a fill
    st.last, st.bid, st.ask = 99.6, 99.59, 99.61  # 0.4% through
    b.try_fill(resting)
    assert resting.done and resting.avg_price == 99.99  # at its limit, not at the later, better price
    marketable = asyncio.run(b.submit("SOXL", "BUY", 10, 99.8, "scalp-entry"))  # at/above the ask: fills now
    assert marketable.done and marketable.avg_price == 99.61
