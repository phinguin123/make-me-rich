import asyncio
from datetime import datetime

from trader.broker import Order, PaperBroker
from trader.guard import OrderGuard
from trader.market import ET, MarketData


def guard_at(h, m, money=12500.0, quotes=None, open_orders=()):
    quotes = quotes or {"SOXL": (120.0, 120.1), "SOXS": (38.0, 38.02), "AAPL": (200.0, 200.1)}
    return OrderGuard(lambda: money, lambda s: quotes.get(s, (0.0, 0.0)), lambda: list(open_orders),
                      now=lambda: datetime(2026, 10, 14, h, m, tzinfo=ET))


def test_allowed_orders_pass():
    g = guard_at(8, 0)
    assert g.check("SOXL", "BUY", 50, 120.05, "scalp-pre-entry") == ""
    assert g.check("SOXL", "SELL", 50, 122.5, "scalp-pre-target") == ""  # selling above the market is fine
    assert guard_at(10, 30).check("SOXS", "BUY", 100, 38.06, "scalp-entry") == ""
    assert guard_at(10, 0).check("AAPL", "BUY", 0, None, "swing-buy", amount=900) == ""


def test_symbols_size_price_windows_doubles_counts():
    assert "only trade SOXL/SOXS" in guard_at(8, 0).check("TQQQ", "BUY", 10, 50, "scalp-entry")
    assert "leveraged" in guard_at(10, 0).check("SOXL", "BUY", 0, None, "swing-buy", amount=500)
    assert guard_at(10, 0).check("SOXS", "BUY", 0, None, "swing-buy", amount=500) == ""  # the SOXS hedge
    assert "exceeds 60%" in guard_at(8, 0).check("SOXL", "BUY", 70, 120.05, "scalp-pre-entry")  # $8.4k of $12.5k
    assert "above the market" in guard_at(8, 0).check("SOXL", "BUY", 10, 135.0, "scalp-pre-entry")
    assert guard_at(8, 0).check("SOXL", "BUY", 10, 126.0, "scalp-pre-entry") == ""  # 5% after a spike: fine
    assert "below the market" in guard_at(8, 0).check("SOXL", "SELL", 10, 105.0, "scalp-close")
    assert "only allowed" in guard_at(12, 0).check("SOXL", "BUY", 10, 120.05, "scalp-entry")
    assert guard_at(15, 58).check("SOXL", "SELL", 10, 119.8, "scalp-close") == ""  # exits are never time-blocked
    working = Order(id="x", client_id="", symbol="SOXL", side="BUY", qty=5, limit=119, tag="scalp-pre-entry")
    assert "already working" in guard_at(8, 0, open_orders=[working]).check("SOXL", "BUY", 5, 119, "scalp-pre-entry")
    g = guard_at(8, 0)
    for _ in range(30):
        assert g.check("SOXL", "BUY", 1, 120.05, "scalp-pre-entry") == ""
    assert "orders today" in g.check("SOXL", "BUY", 1, 120.05, "scalp-pre-entry")


def test_refused_order_never_reaches_the_broker():
    md = MarketData(client=None)
    b = PaperBroker(md, 0.001, 10000.0)
    b.guard = guard_at(12, 0)
    refused = []
    b.on_refuse = refused.append
    o = asyncio.run(b.submit("SOXL", "BUY", 10, 120.05, "scalp-entry"))
    assert o.status == "REJECTED" and o.error.startswith("guard:") and refused and b._cash == 10000.0
