"""Strategy research: sweep the three families with Toss costs and report IS/OOS.

    python -m trader.research.experiments [orb|tc|overnight|all]
"""
from __future__ import annotations

import sys
import time as clock
from datetime import time

import pandas as pd

from .backtest import RES, Params, load_all, sweep

GRIDS = {
    "orb": (
        Params(strategy="orb"),
        {
            "or_minutes": [5, 15, 30],
            "stop_atr": [0.1, 0.25, 0.5],
            "trail_atr": [0.0, 0.75],
            "exit_mode": ["eod", "hold_strong"],
            "regime": ["none", "risk_on"],
        },
    ),
    "orb_orlow": (
        Params(strategy="orb", stop_mode="or_low"),
        {"or_minutes": [5, 15, 30], "trail_atr": [0.0, 0.75], "exit_mode": ["eod", "hold_strong"], "regime": ["none", "risk_on"]},
    ),
    "tc": (
        Params(strategy="tc"),
        {
            "tc_time": [time(10, 0), time(10, 30), time(11, 0)],
            "tc_min_move_atr": [0.2, 0.5],
            "stop_atr": [0.5, 1.0],
            "trail_atr": [0.0, 1.0],
            "exit_mode": ["eod", "hold_strong"],
            "regime": ["none", "risk_on"],
        },
    ),
    "overnight": (
        Params(strategy="overnight"),
        {"top_n": [5, 10, 20, 30], "regime": ["none"]},
    ),
}


def main(which: list[str]) -> None:
    t0 = clock.time()
    inplay, macro, cache, days = load_all()
    print(f"loaded {len(days)} days, {len(inplay)} in-play rows in {clock.time() - t0:.0f}s", flush=True)
    for name in which:
        base, grid = GRIDS[name]
        print(f"\n===== {name} =====", flush=True)
        res = sweep(grid, base, inplay, macro, cache, days)
        res.to_csv(RES / f"sweep_{name}.csv", index=False)
        cols = [c for c in res.columns if c not in ("pf",)]
        print(res.sort_values("sharpe", ascending=False)[cols].head(15).to_string(), flush=True)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    main(list(GRIDS) if arg == "all" else arg.split(","))
