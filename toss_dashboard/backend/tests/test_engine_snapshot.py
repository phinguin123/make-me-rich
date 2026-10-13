import json

from trader.config import Settings
from trader.engine import Engine


def test_engine_snapshot_is_plain_json():
    """/ws sends the engine snapshot with json.dumps every second: any non-JSON value breaks the dashboard."""
    e = Engine(Settings(mode="paper", capital=0.0, swing_capital=0.0, scalp_capital=1000.0))
    json.dumps(e.snapshot())
