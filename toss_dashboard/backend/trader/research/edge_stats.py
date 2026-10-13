"""Backtest expectations for the edge health monitor (writes trader/edges.json).

Per-trade net returns (on the position, after Toss fees and modelled slippage) for each live edge:
  scalp-orb  bear-regime 30-minute opening-range breakout   (research/scalp.py, e2_orb)
  scalp-pre  pre-market dip book: 14 names, 3 slots, QQQ filter, pessimistic fills (research/pre_slots.py)
For each: mean, standard deviation, trades per year, and two drawdown limits from 20,000 bootstrap
resamples of one year of trades: the worst peak-to-trough run that only 10% (dd_reduce) and 1%
(dd_off) of resampled years reach.

    python -m trader.research.edge_stats
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from . import scalp, scalp_ext

OUT = Path(__file__).resolve().parents[1] / "edges.json"


def orb_trades() -> pd.DataFrame:
    m, days = scalp.load_matrices()
    t = scalp.e2_orb(m, days, 30, "opposite", 0.0)
    t = t[t["regime"] == "bear"]
    return pd.DataFrame({"date": pd.to_datetime(t["date"]), "net": t["net"].to_numpy()})


def pre_trades() -> pd.DataFrame:
    """Accepted trades of the live book (slots taken in time order, QQQ filter), pessimistic fills."""
    from .pre_multi import matrices
    from .pre_slots import all_trades, qqq_up
    from ..replay import SLOTS
    from ..scalp_pre import NAMES
    t = all_trades(matrices(), "pessimistic")
    up = qqq_up()
    order = {s: i for i, s in enumerate(NAMES)}
    t["o"] = t["sym"].map(order)
    rows = []
    for d, g in t.groupby("date"):
        if up.get(d) is not True and up.get(d) != 1:
            continue
        busy: list[int] = []
        for row in g.sort_values(["j", "o"]).itertuples():
            busy = [q for q in busy if q > row.j]
            if len(busy) >= SLOTS:
                continue
            busy.append(row.q)
            rows.append((d, row.net))
    return pd.DataFrame(rows, columns=["date", "net"])


def pre_trades_soxl_only() -> pd.DataFrame:
    a = pd.read_parquet(scalp_ext.D / "alpaca_1m.parquet")
    a = a[a["symbol"] == "SOXL"]
    a["et"] = a["ts"].dt.tz_convert(scalp_ext.ET)
    a["date"] = a["et"].dt.tz_localize(None).dt.normalize()
    a["m"] = (a["et"].dt.hour - 4) * 60 + a["et"].dt.minute
    a = a[(a["m"] >= 0) & (a["m"] < 720)]
    M = {f: a.pivot_table(index="date", columns="m", values=f, aggfunc="last").reindex(columns=range(720)) for f in ("high", "low", "close", "volume")}
    M["close"] = M["close"].ffill(axis=1)
    sig = np.log(M["close"][719]).diff().rolling(20).std().shift(1)
    rows = []
    for d in M["close"].index:
        s = sig.get(d)
        if not np.isfinite(s):
            continue
        c, lo, hi, v = (M[f].loc[d].to_numpy() for f in ("close", "low", "high", "volume"))
        first = np.flatnonzero(np.isfinite(lo[:60]))
        if len(first):
            r = scalp_ext._dip_trade(c, lo, hi, np.nan_to_num(v), first[0], min(0.06, max(0.015, 0.4 * s)), "pessimistic")
            if r is not None:
                rows.append((d, float(scalp_ext.net([r], "pre", limit=True)[0])))
    return pd.DataFrame(rows, columns=["date", "net"])


def stats(t: pd.DataFrame, label: str, source: str, rng: np.random.Generator, slip_model: float) -> dict:
    r = t["net"].to_numpy()
    years = (t["date"].max() - t["date"].min()).days / 365.25
    per_year = len(r) / max(years, 0.5)
    k = max(20, int(round(per_year)))
    sims = rng.choice(r, size=(20000, k), replace=True).cumsum(axis=1)
    dd = (sims - np.maximum.accumulate(np.maximum(sims, 0), axis=1)).min(axis=1)
    return {"label": label, "source": source, "mean": round(float(r.mean()), 5), "std": round(float(r.std(ddof=1)), 5),
            "n": int(len(r)), "per_year": round(per_year, 1),
            "dd_reduce": round(float(np.percentile(dd, 10)), 4), "dd_off": round(float(np.percentile(dd, 1)), 4),
            "period": f"{t['date'].min():%Y-%m} .. {t['date'].max():%Y-%m}", "slip_model": slip_model}


def main() -> None:
    rng = np.random.default_rng(7)
    edges = {
        # slip_model: per-side slippage the backtest assumed (compared with live fills vs the NBBO)
        "scalp-orb": stats(orb_trades(), "SOXL/SOXS opening-range breakout (bear regime)", "research/scalp.py e2_orb 30m", rng, 0.0003),
        "scalp-pre": stats(pre_trades(), "Pre-market dip book (14 names, 3 slots, QQQ filter)", "research/pre_slots.py pessimistic", rng, 0.0),
    }
    OUT.write_text(json.dumps({"generated": date.today().isoformat(), "edges": edges}, indent=2) + "\n")
    for k, v in edges.items():
        print(k, v)


if __name__ == "__main__":
    main()
