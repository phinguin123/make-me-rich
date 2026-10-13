import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd

from trader.alpaca_stream import AlpacaStream
from trader.edges import EdgeMonitor, edge_for_tag
from trader.market import ET, MarketData

TODAY = datetime.now(ET).date()


def at(h, m):
    return datetime.combine(TODAY, datetime.min.time()).replace(hour=h, minute=m, tzinfo=ET)


class FakeAlpaca:
    """Returns 1-minute SIP bars (start-stamped, like Alpaca) for the requested window."""

    def __init__(self, first: datetime, n: int, px: float = 100.0):
        self.rows = [{"symbol": "SOXL", "ts": pd.Timestamp(first + timedelta(minutes=i)).tz_convert("UTC"),
                      "open": px, "high": px + 0.5, "low": px - 0.5, "close": px, "volume": 1000.0} for i in range(n)]
        self.calls = 0

    async def bars(self, symbols, timeframe, start, end, feed="sip", adjustment="raw"):
        self.calls += 1
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        return pd.DataFrame([r for r in self.rows if r["symbol"] in symbols and s <= r["ts"] < e])


def test_stream_filters_non_last_sale_trades():
    s = AlpacaStream("k", "s")
    got = []
    s.on_trade = lambda *a: got.append(a)
    s.handle({"T": "t", "S": "SOXL", "p": 101.5, "s": 100, "c": ["@", "T"], "t": "2026-10-13T08:00:00.123456789Z"})
    s.handle({"T": "t", "S": "SOXL", "p": 95.0, "s": 3, "c": ["@", "I"], "t": "2026-10-13T08:00:00.2Z"})  # odd lot
    assert len(got) == 1 and got[0][1] == 101.5 and got[0][3] == datetime(2026, 10, 13, 8, 0, 0, 123456, tzinfo=timezone.utc)
    s.handle({"T": "subscription", "trades": ["SOXL"], "quotes": []})
    s.set_symbols({"SOXL", "SOXS"}, {"SOXL"})
    assert s._diff() == [{"action": "subscribe", "trades": ["SOXS"], "quotes": ["SOXL"]}]


def test_sip_prices_win_over_toss_frames():
    md = MarketData(client=None)
    now = datetime.now(ET)
    md.on_trade("SOXL", 100.0, 10, now, src="sip")
    md.on_book("SOXL", 99.99, 5, 100.01, 5, now, src="sip")
    md.on_trade("SOXL", 99.0, 10, now)  # sampled Toss frame: ignored while SIP is fresh
    md.on_book("SOXL", 98.9, 5, 99.1, 5, now)
    st = md.get("SOXL")
    assert (st.last, st.bid, st.ask) == (100.0, 99.99, 100.01)
    st.sip_ts = now - timedelta(seconds=30)  # SIP went quiet: Toss takes over
    md.on_trade("SOXL", 99.0, 10, now)
    assert st.last == 99.0


def test_regular_session_bars_from_sip():
    alp = FakeAlpaca(at(9, 25), 40)
    md = MarketData(client=None, alpaca=alp)
    md.bar_symbols = {"SOXL"}
    asyncio.run(md.refresh_bars(now=at(10, 5)))
    st = md.get("SOXL")
    ends = sorted(st.bars)
    assert md.bar_source == "sip" and ends[0] == at(9, 31) and ends[-1] == at(10, 5)  # end-stamped like Toss
    assert st.opening_range(30) == (100.0, 100.5, 99.5, 100.0)
    asyncio.run(md.refresh_bars(now=at(10, 6)))  # incremental: re-reads only the last few minutes
    assert len(st.bars) == 35


def test_premarket_vwap_uses_sip_bars(tmp_path):
    import test_scalp_pre as tp

    sleeve, md, _ = tp.make(tmp_path)
    md.alpaca = FakeAlpaca(at(4, 0), 200, px=50.0)
    tp.quote(md, 50.0)
    tp.tick(sleeve, tp.at(7, 29))
    tp.tick(sleeve, tp.at(7, 30))
    pre = sleeve.pre.st
    assert pre.source == "sip" and abs(pre.vwap - 50.0) < 1e-9 and pre.level < 50.0


def test_fill_cost_vs_nbbo(tmp_path):
    m = EdgeMonitor(tmp_path / "e.json", expectations={"scalp-orb": {"mean": 0.003, "std": 0.04, "dd_reduce": -0.5, "dd_off": -0.8, "slip_model": 0.0003}})
    m.record_fill("scalp-orb", "BUY", 100.05, 99.98, 100.02)  # paid 5 bps over the 100.00 mid
    m.record_fill("scalp-orb", "SELL", 99.97, 99.98, 100.02)  # sold 3 bps under the mid
    f = m.fill_stats("scalp-orb")
    assert f["n"] == 2 and abs(f["cost"] - 0.0004) < 1e-9 and abs(f["half_spread"] - 0.0002) < 1e-9
    assert edge_for_tag("scalp-pre-entry") == "scalp-pre" and edge_for_tag("scalp-stop") == "scalp-orb"
