"""The live relay over five years (2022-01 .. 2026-10), now that the stocks-in-play data includes the 2022 bear market
and the 2023 recovery (research/backfill.py; delisted symbols included). Research only.

  1. the live relay (bigger-swing, regime "none", closed by 16:00, only on SOXX-uptrend days) year by year:
     its trades, their per-trade net, its P&L inside the 30/70 account, and the Deflated Sharpe ratio of five years
     of taken trades
  2. the same with a QQQ > 200-day filter on the stocks-in-play leg (the pre-market book's filter): does it protect
     2022's bear-market rallies without cutting 2024-2026?

    python -m trader.research.relay_5y
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from . import backtest as BT
from . import intraday_relay as IR
from . import scalp as scalp_research
from .edge_stats import deflated_sharpe, orb_trades
from .pre_slots import qqq_up
from .relay_quality import COMM, relay_account, to_day_dict

TRIALS = 304  # 250 before the relay + the 54-variant filter grid
LIVE = replace(IR.BIGGER, regime="none")


def main() -> None:
    print("loading five years of stocks-in-play minutes ...", flush=True)
    data = BT.load_all()
    m, _ = scalp_research.load_matrices()
    bear = m["regime"] == "bear"
    del m
    up = qqq_up()
    book = IR.book_trades(bear)
    orb = orb_trades().groupby("date")["net"].sum()
    orb.index = pd.to_datetime(orb.index)
    swing = IR.swing_returns(0.20)
    days = swing.index[(swing.index >= "2022-01-03") & (swing.index <= pd.Timestamp("2026-10-09"))]

    variants = {"live (regime none)": LIVE, "risk-on filter (for reference)": replace(LIVE, regime="risk_on")}
    trades = {}
    for name, p in variants.items():
        t = BT.generate_trades(p, *data[:3])
        t["d"] = pd.to_datetime(t["date"])
        t["net"] = t["exit"] * (1 - COMM - BT.SEC_FEE) / (t["entry"] * (1 + COMM)) - 1
        t["semis_up"] = ~t["d"].map(bear).fillna(True).astype(bool)
        t["qqq_up"] = t["d"].map(lambda x: up.get(x) is True or up.get(x) == 1)
        trades[name] = t
    base = trades["live (regime none)"]
    trades["live + QQQ > 200-day"] = base[base["qqq_up"]].copy()

    print("\n=== 1. Stocks-in-play trades the relay can take (semis-uptrend days), per year, net of fees ===")
    print(f"{'':32s} " + "  ".join(f"{y:>16d}" for y in range(2022, 2027)))
    for name, t in trades.items():
        r = t[t["semis_up"]]
        cells = []
        for y in range(2022, 2027):
            x = r[r["d"].dt.year == y]["net"]
            cells.append(f"{x.mean():+6.2%} (n {len(x):3d})" if len(x) else f"{'-':>16s}")
        print(f"  {name:30s} " + "  ".join(f"{c:>16s}" for c in cells))
    print("  QQQ above / below its 200-day on those days (live rule):")
    r = base[base["semis_up"]]
    for lab, g in (("QQQ above 200-day", r[r["qqq_up"]]), ("QQQ below 200-day", r[~r["qqq_up"]])):
        print(f"    {lab:28s} n {len(g):4d} avg {g['net'].mean():+.2%}  " + "  ".join(
            f"{y}: {g[g['d'].dt.year == y]['net'].mean():+.2%} ({(g['d'].dt.year == y).sum()})" for y in range(2022, 2027)))

    print("\n=== 2. The 30/70 account, 2022-01 .. 2026-10 (start $12,400, yearly rebalance) ===")
    plans = [("now: book + bear breakout, no stocks-in-play", None, LIVE)] + [(f"relay: {k}", k, variants.get(k, LIVE)) for k in trades]
    print(f"{'plan':54s} {'CAGR':>7s} {'Sharpe':>6s} {'maxDD':>7s} | " + " ".join(f"{y:>7d}" for y in range(2022, 2027)) + " | stocks-in-play P&L by year")
    for label, key, p in plans:
        sip = to_day_dict(trades[key]) if key else None
        eq, info = relay_account(days, 0.30, swing, book, bear, orb, sip, p)
        mm = IR.metrics(eq)
        yearly = eq.resample("YE").last()
        yearly = yearly / yearly.shift(1).fillna(IR.START) - 1
        pnl = " ".join(f"{y}: ${v:+,.0f}" for y, v in sorted(info.get("by_year", {}).items())) if key else ""
        print(f"{label:54s} {mm['CAGR']:+7.1%} {mm['Sharpe']:6.2f} {mm['maxDD']:+7.1%} | " + " ".join(f"{v:+7.1%}" for v in yearly.values) + f" | {pnl}")

    print(f"\n=== 3. Deflated Sharpe ratio of the stocks-in-play trades as taken (live sizing on its own pot, semis-uptrend days; {TRIALS} trials) ===")
    for name, t in trades.items():
        p = variants.get(name, LIVE)
        t = t[t["semis_up"]]
        for lab, lo in (("2022-2026", "2022"), ("2024-2026", "2024")):
            x = t[t["d"] >= lo]
            taken, _ = BT.run_portfolio(replace(p, capital=3720.0), x.drop(columns=["d", "net", "semis_up", "qqq_up"]))
            r = taken["ret"].to_numpy()
            ds = deflated_sharpe(r, TRIALS)
            print(f"  {name:30s} {lab}: n {len(r):4d} avg {r.mean():+.2%} per-trade Sharpe {ds['sharpe_trade']:.3f} (luck level {ds['sr0']:.3f}) DSR {ds['dsr']:.2f}")


if __name__ == "__main__":
    main()
