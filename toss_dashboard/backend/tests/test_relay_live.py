"""The live intraday relay: stocks-in-play trading the scalp pot's free cash on semis-uptrend days."""
import json
from dataclasses import replace
from datetime import datetime

from trader.broker import Order
from trader.config import Settings
from trader.engine import Engine
from trader.guard import OrderGuard
from trader.market import ET
from trader.scalp import ScalpState
from trader.strategy import Position

TODAY = datetime.now(ET).date()


def make(tmp_path, relay=True):
    e = Engine(Settings(mode="paper", capital=0.0, swing_capital=0.0, scalp_capital=3720.0, relay=relay, relay_regime="risk_on",
                        scalp_force_day="", scalp_position_frac=1.0, pre_names=("SOXL",), pre_slots=1, pre_qqq_filter=False))
    e.state_path, e.trades_path = tmp_path / "state.json", tmp_path / "trades.jsonl"  # never touch the real data folder
    e.scalp.path, e.edges.path = tmp_path / "scalp.json", tmp_path / "edges.json"
    e.edges.edges = {}
    e.scalp.cash, e.scalp.realized, e.scalp.st = 3720.0, 0.0, ScalpState()
    for n in e.scalp.pre.names.values():
        n.st.qty, n.st.status = 0.0, "done"
    e.positions, e.realized_pnl = {}, 0.0
    return e


def regime(e, regime, status):
    e.scalp.st.day, e.scalp.st.regime, e.scalp.st.status = TODAY.isoformat(), regime, status


def test_relay_profile_and_the_uptrend_gate(tmp_path):
    e = make(tmp_path)
    assert e.relay and e.cfg.exit_mode == "eod" and e.cfg.min_atr_pct == 0.05 and e.cfg.regime == "risk_on"
    assert not e.relay_on(TODAY)  # the breakout's regime isn't loaded yet
    regime(e, "bull", "standing_aside")
    assert e.relay_on(TODAY)
    regime(e, "bear", "waiting")
    assert not e.relay_on(TODAY)  # the breakout owns the pot
    regime(e, "bull", "waiting")
    assert not e.relay_on(TODAY)  # a one-day override that trades the breakout in an uptrend
    e.cfg = replace(e.cfg, regime="none")  # past the QQQ/VIXY filter, to reach the relay's own checks
    assert "relay" in e._entry_block_reason()
    regime(e, "bull", "standing_aside")
    assert e._entry_block_reason() == ""
    e.edges._edge("intraday-orb")["status"] = "off"
    assert "edge monitor" in e._entry_block_reason()
    assert not make(tmp_path, relay=False).relay


def test_relay_sizes_from_the_pots_free_cash(tmp_path):
    e = make(tmp_path)
    regime(e, "bull", "standing_aside")
    assert e._relay_qty(50.0, 47.5) == 14  # 1% of the $3,720 pot at risk over a $2.50 stop (35% cap = 26 shares)
    e.scalp.cash = 500.0  # the pre-market book holds the rest of the pot until 10:30
    e.scalp.pre.value = lambda: 3220.0
    assert e._relay_qty(50.0, 47.5) == 9  # only the free $500
    e._relay_reserved["OTHER"] = 300.0  # another entry is sizing right now
    assert e._relay_qty(50.0, 47.5) == 3
    e._relay_reserved.clear()
    e.scalp.cash, e.scalp.pre.value = 3720.0, lambda: 0.0  # the book is flat again
    e.edges._edge("intraday-orb")["status"] = "reduced"
    assert e._relay_qty(50.0, 47.5) == 7  # the edge monitor halves it


def test_relay_fills_move_the_pots_cash_and_feed_the_edge_monitor(tmp_path):
    e = make(tmp_path)
    regime(e, "bull", "standing_aside")
    e.positions["X"] = Position("X", 0.0, 50.0, 47.5, 47.5, 1.0, datetime.now(ET).isoformat(), highest=50.0)
    e._on_fill(Order(id="1", client_id="", symbol="X", side="BUY", qty=10, limit=50.0, tag="relay-entry"), 10, 50.0)
    assert abs(e.scalp.cash - (3720.0 - 500.5)) < 1e-6
    e.market.get("X").last = 52.0
    assert abs(e.scalp.equity() - (3219.5 + 520.0)) < 1e-6  # the pot counts the open position
    assert e.scalp.relay_busy()
    e._on_fill(Order(id="2", client_id="", symbol="X", side="SELL", qty=10, limit=None, tag="relay-close"), 10, 52.0)
    assert "X" not in e.positions and not e.scalp.relay_busy()
    proceeds = 520.0 * (1 - 0.001 - 0.0000206)
    assert abs(e.scalp.cash - (3219.5 + proceeds)) < 1e-6 and abs(e.scalp.realized - (proceeds - 500.5)) < 1e-6
    live = e.edges.stats("intraday-orb")
    assert live["n"] == 1 and abs(live["mean"] - (proceeds / 500.5 - 1)) < 1e-6  # stored to 6 decimals


def test_allocator_waits_for_relay_positions(tmp_path):
    import test_allocator as ta

    alloc, sleeve, _ = ta.make(tmp_path)
    assert alloc.safe() == ""
    sleeve.relay_busy = lambda: True
    assert "relay" in alloc.safe()


def test_guard_caps_one_relay_position_at_40pct_of_the_pot():
    g = OrderGuard(lambda: 12400.0, lambda s: (49.99, 50.01), lambda: [], relay_pot=lambda: 3720.0)
    assert "scalp pot" in g.check("X", "BUY", 30, 50.0, "relay-entry")  # $1,500 > 40% of $3,720
    assert g.check("X", "BUY", 29, 50.0, "relay-entry") == ""
    assert g.check("X", "SELL", 500, 50.0, "relay-close") == ""  # exits are never capped


def test_no_daily_loss_halt_on_the_relays_tiny_base(tmp_path):
    e = make(tmp_path)
    e.realized_pnl = 100.0
    e.risk.new_day(e.equity())
    e.realized_pnl = 95.0  # -5% of a $100 "equity": meaningless for a sleeve without capital
    e._check_daily_loss()
    assert not e.risk.status.halted


def test_relay_snapshot_is_plain_json(tmp_path):
    e = make(tmp_path)
    regime(e, "bull", "standing_aside")
    snap = e.snapshot()
    assert snap["relay"]["enabled"] and snap["relay"]["today"]
    json.dumps(snap)
