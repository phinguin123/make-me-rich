"""Fastest realistic path from $12,400 to $100,000: chance of getting there vs chance of getting hurt.

Each candidate is a daily account return series; 5,000 ten-year paths are drawn by block bootstrap
(21-day blocks keep crashes and streaks intact). Monthly savings can be added. Reported per
candidate and savings level:
  P(hit $100k) by year 3 / 5 / 7 / 10, median years to $100k,
  P(ruin) = the account ever falls below half of everything put in (start + savings so far),
  median and 10th-percentile wealth after 5 years.

Candidates use two histories:
  2021-2026 (the bot's sleeves: scalp, swing, SOXL dip-buy, SOXL trend) - a strong period, so also
  shown with every edge halved;
  long histories (TQQQ/SOXL rules since 1986/1995 incl. 2000-02 and 2008; sector 3x since 2008).

    python -m trader.research.goal
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import lev_trend as LT
from . import sector_mom as SM
from . import soxl_soxs as X
from .allocation3 import sleeves

START, GOAL = 12400.0, 100000.0
YEARS, PATHS, BLOCK = 10, 5000, 21


def candidates() -> dict[str, pd.Series]:
    df, _ = sleeves()
    mix = lambda w: sum(df[k] * v for k, v in w.items())  # noqa: E731 — daily-rebalanced approximation
    halve = df - df.mean() / 2
    mixh = lambda w: sum(halve[k] * v for k, v in w.items())  # noqa: E731
    out = {
        "Live: 25% scalp / 60% swing / 15% cash": mix({"scalp": 0.25, "swing": 0.60}),
        "30% scalp / 70% swing": mix({"scalp": 0.30, "swing": 0.70}),
        "Swing only (100%)": mix({"swing": 1.0}),
        "Max growth: 100% scalp at 100%/trade": mix({"scalp": 1.0}),
        "Aggressive: 50 scalp / 30 dip / 20 trend": mix({"scalp": 0.5, "dip": 0.3, "trend": 0.2}),
        "  same live mix, every edge halved": mixh({"scalp": 0.25, "swing": 0.60}),
        "  max growth, every edge halved": mixh({"scalp": 1.0}),
        "  aggressive, every edge halved": mixh({"scalp": 0.5, "dip": 0.3, "trend": 0.2}),
    }
    # long histories
    lt = LT.load()
    rf_all = lt["tbill"].ffill() / 100 / 252
    ndx = lt["^NDX"].dropna()
    r_ndx = ndx.pct_change()
    rf = rf_all.reindex(ndx.index).fillna(0)
    r3 = LT.synth(r_ndx, rf, 3, LT.FEES["TQQQ"], -0.0075)
    rules = LT.rules(ndx, r_ndx)
    out["TQQQ 200d + vol target (1986-2026)"] = LT.run(r3, rf, rules["SMA200 + vol target 40%"]).iloc[260:]
    out["TQQQ buy & hold (1986-2026)"] = r3.iloc[260:].fillna(0)
    d = X.load()
    sox = d["^SOX"]["close"].dropna()
    rfs = (d["TBILL"]["close"].reindex(sox.index).ffill() / 100 / 252).fillna(0)
    out["SOXL buy & hold (1995-2026)"] = X.synth(sox.pct_change().fillna(0), rfs, 3, -0.0065).iloc[1:]
    sec = SM.results()
    out["Sector 3x momentum top5 (2008-2026)"] = sec["3x 12-1 top5 +trend +vol40"].loc["2008":]
    return {k: v.dropna() for k, v in out.items()}


def simulate(r: pd.Series, monthly: float, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    a = r.to_numpy()
    T = 252 * YEARS
    starts = rng.integers(0, len(a) - BLOCK, size=(PATHS, T // BLOCK + 1))
    R = a[(starts[:, :, None] + np.arange(BLOCK)).reshape(PATHS, -1)[:, :T]]
    W = np.full(PATHS, START)
    put_in = np.full(PATHS, START)
    hit = np.full(PATHS, np.inf)
    ruined = np.zeros(PATHS, bool)
    w5 = None
    for t in range(T):
        W = W * (1 + R[:, t])
        if monthly and t % 21 == 20:
            W = W + monthly
            put_in = put_in + monthly
        ruined |= W < 0.5 * put_in
        newly = (W >= GOAL) & ~np.isfinite(hit)
        hit[newly] = (t + 1) / 252
        if t == 252 * 5 - 1:
            w5 = W.copy()
    yrs = np.where(np.isfinite(hit), hit, np.nan)
    return {"p3": np.mean(hit <= 3), "p5": np.mean(hit <= 5), "p7": np.mean(hit <= 7), "p10": np.mean(hit <= 10),
            "median years": float(np.nanmedian(yrs)) if np.isfinite(yrs).mean() >= 0.5 else np.inf,
            "ruin": ruined.mean(), "w5 median": np.median(w5), "w5 p10": np.quantile(w5, 0.10)}


def main() -> None:
    cands = candidates()
    for monthly in (0, 500, 1000):
        print(f"\n=== start ${START:,.0f}, add ${monthly:,}/month, goal ${GOAL:,.0f} ===")
        print(f"{'strategy':46s} {'CAGR':>6s} {'P(3y)':>6s} {'P(5y)':>6s} {'P(7y)':>6s} {'P(10y)':>6s} {'median yrs':>10s} "
              f"{'P(ruin)':>7s} {'5y median':>10s} {'5y bad(10%)':>11s}")
        for name, r in cands.items():
            s = simulate(r, monthly)
            cagr = (1 + r).prod() ** (252 / len(r)) - 1
            my = f"{s['median years']:.1f}" if np.isfinite(s["median years"]) else ">10"
            print(f"{name:46s} {cagr:+6.0%} {s['p3']:6.0%} {s['p5']:6.0%} {s['p7']:6.0%} {s['p10']:6.0%} {my:>10s} "
                  f"{s['ruin']:7.0%} {s['w5 median']:10,.0f} {s['w5 p10']:11,.0f}")


if __name__ == "__main__":
    main()
