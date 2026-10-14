"""Backtest expectations for the edge health monitor (writes trader/edges.json).

Per-trade net returns (on the position, after Toss fees and modelled slippage) for each live edge:
  scalp-orb  bear-regime 30-minute opening-range breakout   (research/scalp.py, e2_orb)
  scalp-pre  pre-market dip book: 14 names, 3 slots, QQQ filter, pessimistic fills (research/pre_slots.py)
For each: mean, standard deviation, trades per year, and two drawdown limits from 20,000 bootstrap
resamples of one year of trades: the worst peak-to-trough run that only 10% (dd_reduce) and 1%
(dd_off) of resampled years reach.
Multiple-testing check: the Deflated Sharpe Ratio (Bailey & Lopez de Prado 2014) of the per-trade returns,
given roughly how many configurations the research tried before picking each edge (TRIALS) and the
returns' skew and fat tails: the probability that the true per-trade Sharpe ratio is above zero after
discounting the best-of-N luck. Under 0.95 means the backtest alone is weak evidence.

    python -m trader.research.edge_stats
"""
from __future__ import annotations

import json
import math
from datetime import date
from statistics import NormalDist
from pathlib import Path

import numpy as np
import pandas as pd

from . import scalp, scalp_ext

OUT = Path(__file__).resolve().parents[1] / "edges.json"
# Rough count of the configurations tried before each edge was picked (families x parameters x filters in
# research/scalp.py; dip size, start and exit times, slots, names, filters and fill models for the book).
TRIALS = {"scalp-orb": 60, "scalp-pre": 200}


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
            rows.append((d, row.net, row.time_exit))
    return pd.DataFrame(rows, columns=["date", "net", "time_exit"])


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


def deflated_sharpe(r: np.ndarray, trials: int) -> dict:
    """Per-trade Sharpe ratio, the best-of-`trials` level pure luck would reach (sr0), and the probability
    that the true Sharpe ratio is above zero after that haircut (Bailey & Lopez de Prado 2014, eq. 9-10)."""
    nd = NormalDist()
    n = len(r)
    sr = r.mean() / r.std(ddof=1)
    z = (r - r.mean()) / r.std(ddof=0)
    skew, kurt = float((z ** 3).mean()), float((z ** 4).mean())
    sd = math.sqrt((1 - skew * sr + (kurt - 1) / 4 * sr ** 2) / (n - 1))  # standard error of the Sharpe estimate
    gamma = 0.5772156649  # Euler-Mascheroni
    sr0 = sd * ((1 - gamma) * nd.inv_cdf(1 - 1 / trials) + gamma * nd.inv_cdf(1 - 1 / (trials * math.e)))
    return {"sharpe_trade": round(float(sr), 4), "sr0": round(sr0, 4), "dsr": round(nd.cdf((sr - sr0) / sd), 4), "trials": trials}


def stats(t: pd.DataFrame, label: str, source: str, rng: np.random.Generator, slip_model: float, trials: int) -> dict:
    r = t["net"].to_numpy()
    years = (t["date"].max() - t["date"].min()).days / 365.25
    per_year = len(r) / max(years, 0.5)
    k = max(20, int(round(per_year)))
    sims = rng.choice(r, size=(20000, k), replace=True).cumsum(axis=1)
    dd = (sims - np.maximum.accumulate(np.maximum(sims, 0), axis=1)).min(axis=1)
    return {"label": label, "source": source, "mean": round(float(r.mean()), 5), "std": round(float(r.std(ddof=1)), 5),
            "n": int(len(r)), "per_year": round(per_year, 1),
            "dd_reduce": round(float(np.percentile(dd, 10)), 4), "dd_off": round(float(np.percentile(dd, 1)), 4),
            "period": f"{t['date'].min():%Y-%m} .. {t['date'].max():%Y-%m}", "slip_model": round(slip_model, 6),
            **deflated_sharpe(r, trials)}


def main() -> None:
    rng = np.random.default_rng(7)
    pre = pre_trades()
    edges = {
        # slip_model: per-side slippage the backtest assumed (compared with live fills vs the NBBO). The book's
        # entries and take-profits are limit orders; only its time exits (market orders) pay SLIP["pre"]: per
        # fill that averages SLIP["pre"] x the share of trades that end at the time exit / 2 fills.
        "scalp-orb": stats(orb_trades(), "SOXL/SOXS opening-range breakout (bear regime)", "research/scalp.py e2_orb 30m", rng,
                           0.0003, TRIALS["scalp-orb"]),
        "scalp-pre": stats(pre, "Pre-market dip book (14 names, 3 slots, QQQ filter)", "research/pre_slots.py pessimistic", rng,
                           scalp_ext.SLIP["pre"] * float(pre["time_exit"].mean()) / 2, TRIALS["scalp-pre"]),
    }
    OUT.write_text(json.dumps({"generated": date.today().isoformat(), "edges": edges}, indent=2) + "\n")
    for k, v in edges.items():
        print(k, v)


if __name__ == "__main__":
    main()
