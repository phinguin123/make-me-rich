"""Full scalp vs scalp + swing split, on the same days (2021-01 .. 2026-10).

Daily returns of each sleeve from its own backtest:
  scalp  bear-regime opening-range breakout (research/scalp.py) + pre-market SOXL dip (pessimistic
         fills, research/scalp_ext.py), per-trade net returns x the share of the pot each trade uses
  swing  monthly momentum with a 20% volatility target (research/portfolio.py, PP defaults)
Sleeves are separate pots, as in the bot (no rebalancing between them unless stated). Idle cash
earns nothing. After-tax: Korean 22% on each year's net gain above the 2.5M KRW allowance (~$1,800).

    python -m trader.research.allocation
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import portfolio
from .edge_stats import orb_trades, pre_trades

CAPITAL = 12400.0
ALLOWANCE = 1800.0
TAX = 0.22


def sleeve_returns() -> pd.DataFrame:
    orb, pre = orb_trades(), pre_trades()
    w = portfolio.load()
    eq, trades = portfolio.backtest(w, portfolio.PP())
    swing = eq.pct_change().fillna(eq.iloc[0] / portfolio.PP().capital - 1)
    swing.index = pd.to_datetime(swing.index)
    days = swing.index[(swing.index >= "2021-01-04") & (swing.index <= min(swing.index.max(), pd.Timestamp("2026-10-08")))]
    df = pd.DataFrame(index=days)
    df["swing"] = swing.reindex(days).fillna(0.0)
    df["orb"] = orb.groupby(orb["date"].dt.normalize())["net"].sum().reindex(days).fillna(0.0)
    df["pre"] = pre.groupby(pd.to_datetime(pre["date"]).dt.normalize())["net"].sum().reindex(days).fillna(0.0)
    df["orb_n"] = orb.groupby(orb["date"].dt.normalize())["net"].count().reindex(days).fillna(0)
    df["pre_n"] = pre.groupby(pd.to_datetime(pre["date"]).dt.normalize())["net"].count().reindex(days).fillna(0)
    return df


def simulate(df: pd.DataFrame, scalp_cap: float, frac: float, swing_cap: float, rebalance: bool = False) -> pd.Series:
    """Account value per day; scalp trades use `frac` of the scalp pot."""
    scalp, swing, out = scalp_cap, swing_cap, []
    year = df.index[0].year
    for t, row in zip(df.index, df.itertuples(index=False)):
        if rebalance and t.year != year and scalp_cap and swing_cap:
            total = scalp + swing
            scalp, swing = total * scalp_cap / (scalp_cap + swing_cap), total * swing_cap / (scalp_cap + swing_cap)
        year = t.year
        scalp *= 1 + frac * (row.orb + row.pre)
        swing *= 1 + row.swing
        out.append(scalp + swing)
    return pd.Series(out, index=df.index)


def after_tax(eq: pd.Series, start: float) -> pd.Series:
    """Pay 22% of each calendar year's gain above the allowance at year end."""
    vals, base, paid = [], start, 0.0
    for t, v in eq.items():
        vals.append(v - paid)
    out = pd.Series(vals, index=eq.index)
    adj = eq.copy()
    tax_total = 0.0
    prev_end = start
    for yr, grp in eq.groupby(eq.index.year):
        gain = grp.iloc[-1] - tax_total - prev_end
        tax = TAX * max(0.0, gain - ALLOWANCE)
        adj.loc[grp.index] = grp - tax_total
        tax_total += tax
        adj.loc[grp.index[-1]] = grp.iloc[-1] - tax_total
        prev_end = adj.loc[grp.index[-1]]
    return adj


def metrics(eq: pd.Series, start: float) -> dict:
    r = eq.pct_change().fillna(eq.iloc[0] / start - 1)
    yrs = len(eq) / 252
    yearly = eq.resample("YE").last()
    yearly = yearly / yearly.shift(1).fillna(start) - 1
    return {"CAGR": (eq.iloc[-1] / start) ** (1 / yrs) - 1, "vol": r.std() * np.sqrt(252),
            "Sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() else 0.0,
            "maxDD": (eq / eq.cummax() - 1).min(), "maxDD$": (eq - eq.cummax()).min(),
            "worst yr": yearly.min(), "end$": eq.iloc[-1], "yearly": yearly}


def bootstrap(df: pd.DataFrame, plans: dict, years: int, n: int = 4000, block: int = 21, seed: int = 11) -> pd.DataFrame:
    """Block-bootstrap joint daily returns (keeps the sleeves' same-day relationship and streaks)."""
    rng = np.random.default_rng(seed)
    days = 252 * years
    arr = df[["orb", "pre", "swing"]].to_numpy()
    starts = rng.integers(0, len(arr) - block, size=(n, days // block + 1))
    rows = []
    for k in range(n):
        idx = (starts[k][:, None] + np.arange(block)).ravel()[:days]
        sample = pd.DataFrame(arr[idx], columns=["orb", "pre", "swing"], index=pd.RangeIndex(days))
        sample.index = pd.bdate_range("2000-01-03", periods=days)
        row = {}
        for name, (sc, fr, sw) in plans.items():
            eq = simulate(sample, sc, fr, sw)
            row[name] = eq.iloc[-1] / CAPITAL - 1
            row[name + " dd"] = (eq / eq.cummax() - 1).min()
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    df = sleeve_returns()
    print(f"{df.index[0]:%Y-%m-%d} .. {df.index[-1]:%Y-%m-%d}: {int(df['orb_n'].sum())} breakout trades, "
          f"{int(df['pre_n'].sum())} pre-market trades, scalp invested on {((df['orb_n'] + df['pre_n']) > 0).mean():.0%} of days")
    traded = (df["orb"] != 0) | (df["pre"] != 0)
    print(f"daily correlation scalp vs swing: all days {(df['orb'] + df['pre']).corr(df['swing']):+.2f}, "
          f"scalp-trading days {(df['orb'] + df['pre'])[traded].corr(df['swing'][traded]):+.2f}")
    half = CAPITAL / 2
    plans = {
        "A  full scalp (12.4k, 50%/trade) = now": (CAPITAL, 0.5, 0.0),
        "B  split: scalp 6.2k @100% + swing 6.2k": (half, 1.0, half),
        "B' split, rebalanced each January": (half, 1.0, half),
        "C  full scalp (12.4k, 100%/trade)": (CAPITAL, 1.0, 0.0),
        "D  full swing (12.4k)": (0.0, 0.0, CAPITAL),
    }
    print(f"\n{'plan':44s} {'CAGR':>7s} {'after tax':>9s} {'vol':>5s} {'Sharpe':>6s} {'maxDD':>7s} {'maxDD $':>8s} {'worst yr':>8s} {'end $':>8s}")
    years = {}
    for name, (sc, fr, sw) in plans.items():
        eq = simulate(df, sc, fr, sw, rebalance=name.startswith("B'"))
        m, mt = metrics(eq, CAPITAL), metrics(after_tax(eq, CAPITAL), CAPITAL)
        years[name[:2].strip()] = m["yearly"]
        print(f"{name:44s} {m['CAGR']:+7.1%} {mt['CAGR']:+9.1%} {m['vol']:5.0%} {m['Sharpe']:6.2f} {m['maxDD']:7.1%} "
              f"{m['maxDD$']:8,.0f} {m['worst yr']:+8.1%} {m['end$']:8,.0f}")
    print("\nby year:")
    print(pd.DataFrame(years).map(lambda v: f"{v:+.1%}").rename(index=lambda t: t.year).to_string())
    print("\nsleeves by year (on their own pots):")
    by = pd.DataFrame({"breakout (per $ at 100%)": df["orb"], "pre-market (at 100%)": df["pre"], "swing": df["swing"]})
    print(((1 + by).groupby(by.index.year).prod() - 1).map(lambda v: f"{v:+.1%}").to_string())

    print("\nbootstrap (21-day blocks of the joint history, 4,000 paths):")
    boot_plans = {k: v for k, v in plans.items() if not k.startswith("B'")}
    for yrs in (1, 3):
        b = bootstrap(df, boot_plans, yrs)
        print(f"  {yrs} year{'s' if yrs > 1 else ''}:")
        for name in boot_plans:
            r, dd = b[name], b[name + " dd"]
            print(f"    {name:44s} median {r.median():+7.1%}  5th pct {r.quantile(0.05):+7.1%}  P(loss) {(r < 0).mean():4.0%}  "
                  f"median maxDD {dd.median():6.1%}  5th pct maxDD {dd.quantile(0.05):6.1%}")
        a, bb = b["A  full scalp (12.4k, 50%/trade) = now"], b["B  split: scalp 6.2k @100% + swing 6.2k"]
        print(f"    P(split ends ahead of full scalp) {(bb > a).mean():.0%}")


if __name__ == "__main__":
    main()
