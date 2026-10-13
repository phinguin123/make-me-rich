import asyncio
from datetime import datetime, timedelta

from trader.edges import EdgeMonitor, load_expectations

EXP = {"demo": {"label": "demo", "mean": 0.01, "std": 0.02, "n": 50, "per_year": 13, "dd_reduce": -0.08, "dd_off": -0.13}}


def mon(tmp_path):
    return EdgeMonitor(tmp_path / "edges.json", expectations=EXP)


def test_healthy_until_evidence(tmp_path):
    m = mon(tmp_path)
    assert m.multiplier("demo") == 1.0
    for r in (0.02, -0.01, 0.015, -0.02, 0.01):
        m.record("demo", r, r * 1000)
    assert m.multiplier("demo") == 1.0 and m.stats("demo")["n"] == 5


def test_drawdown_reduces_then_switches_off_and_stays_off(tmp_path):
    m = mon(tmp_path)
    for _ in range(3):
        m.record("demo", -0.03, -30)  # dd -9%: beyond the 1-in-10 year
    assert m.multiplier("demo") == 0.5
    m.record("demo", -0.05, -50)  # dd -14%: beyond the 1-in-100 year
    assert m.multiplier("demo") == 0.0
    for _ in range(10):
        m.record("demo", 0.03, 30)  # winners don't turn an off edge back on by themselves
    assert m.multiplier("demo") == 0.0
    m.reenable("demo")
    assert m.multiplier("demo") == 1.0 and m.stats("demo")["n"] == 0


def test_poor_mean_reduces_and_hysteresis(tmp_path):
    m = mon(tmp_path)
    for r in (0.0, -0.01, 0.0, -0.01, -0.005, 0.0, -0.01, -0.005):  # mean -0.5% vs +1% expected, z ~ -2.1
        m.record("demo", r, r * 1000)
    assert m.edges["demo"]["status"] == "reduced"
    m.record("demo", 0.02, 20)  # one winner is not enough to call it recovered
    assert m.edges["demo"]["status"] == "reduced"
    for _ in range(6):
        m.record("demo", 0.02, 20)
    assert m.edges["demo"]["status"] == "healthy"


def test_state_persists(tmp_path):
    m = mon(tmp_path)
    for _ in range(5):
        m.record("demo", -0.03, -30)
    again = EdgeMonitor(tmp_path / "edges.json", expectations=EXP)
    assert again.multiplier("demo") == 0.0 and again.stats("demo")["n"] == 5


def test_shipped_expectations_load():
    exp = load_expectations()
    assert {"scalp-orb", "scalp-pre"} <= set(exp)
    assert all(e["dd_off"] < e["dd_reduce"] < 0 < e["std"] for e in exp.values())


def test_sleeve_reports_trades_and_obeys_switch_off(tmp_path):
    import test_scalp_pre as tp

    sleeve, md, broker = tp.make(tmp_path)
    sleeve.monitor = EdgeMonitor(tmp_path / "edges.json")
    pre = tp.ready(sleeve, md)
    tp.quote(md, round(pre.level * 0.995, 2))
    tp.tick(sleeve, tp.at(7, 31, 10))
    tp.quote(md, pre.target + 0.05)
    broker.on_quote()
    live = sleeve.monitor.stats("scalp-pre")
    assert live["n"] == 1 and 0.01 < live["mean"] < pre.dip  # one winning trade, net of fees

    sleeve.monitor.edges["scalp-pre"]["status"] = "off"
    sleeve.pre.st.day = ""
    sleeve.pre._prep_day = ""
    tp.tick(sleeve, tp.at(7, 40))
    assert sleeve.pre.st.status == "standing_aside"
