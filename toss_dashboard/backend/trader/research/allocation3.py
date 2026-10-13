"""Allocation with survival first: scalp (intraday SOXL/SOXS) + SOXL dip-buy + SOXL trend + swing + cash.

Candidate sleeves (daily returns on their own pot, 2021-01 .. 2026-10, same days):
  scalp   bear-regime opening-range breakout + pre-market dip (research/scalp.py, scalp_ext.py)
  dip     SOXL dip-buy in uptrends: SOX RSI2 < 10 above its 200-day avg, out above the 5-day avg (soxl_soxs.py)
  trend   SOXL when SOX > 200-day avg, sized to a 40% vol target, else cash (soxl_soxs.py)
  swing   monthly momentum, 20% vol target (research/portfolio.py)
Weights are shares of the account; the rest is cash. Rebalanced each January.

Survival rules (a mix must pass all three):
  1. 2021-2026 history: max drawdown no worse than -20%
  2. bootstrap (21-day blocks, 4,000 one-year paths): the 5% worst year no worse than -10%
  3. crash replay: SOXL sleeves lose what they lost in their worst 12 months since 1995 (synthetic SOXL,
     incl. 2000-02 and 2008) while swing loses 30% and scalp 15% in the same year; account loss <= 25%
Among survivors: the highest median 3-year growth.

    python -m trader.research.allocation3
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from . import allocation_opt as AO
from . import soxl_soxs as X

SWING_CRASH, SCALP_CRASH = -0.30, -0.15


def sleeves() -> tuple[pd.DataFrame, dict]:
    base = AO.load()  # orb, pre, swing daily returns
    d = X.load()
    sox = d["^SOX"]["close"].dropna()
    rf = (d["TBILL"]["close"].reindex(sox.index).ffill() / 100 / 252).fillna(0)
    r = sox.pct_change().fillna(0)
    rs = X.rules(sox, r)
    real = d["SOXL"]["close"].pct_change()
    zero = rf * 0  # idle cash earns nothing in the allocation studies
    out = pd.DataFrame(index=base.index)
    out["scalp"] = base["orb"] + base["pre"]
    out["swing"] = base["swing"]
    for name, key in (("dip", "Dip-buy SOXL, uptrend only"), ("trend", "Trend + vol target: SOXL / cash")):
        wl, ws = rs[key]
        ret = X.backtest(wl, ws, real.reindex(sox.index).fillna(0), real.reindex(sox.index).fillna(0) * 0, zero, 1)
        out[name] = ret.reindex(out.index).fillna(0)
    # long-history crash behaviour of the SOXL sleeves (synthetic SOXL since 1995)
    L = X.synth(r, rf, 3, -0.0065)
    crash = {}
    for name, key in (("dip", "Dip-buy SOXL, uptrend only"), ("trend", "Trend + vol target: SOXL / cash")):
        wl, ws = rs[key]
        ret = X.backtest(wl, ws, L, L * 0, zero, 1).loc["1995-06-01":]
        roll = (1 + ret).rolling(252).apply(np.prod, raw=True) - 1
        eq = (1 + ret).cumprod()
        crash[name] = {"worst 12m": float(roll.min()), "maxDD": float((eq / eq.cummax() - 1).min()),
                       "2000-02": float((1 + ret.loc["2000-03-01":"2002-10-31"]).prod() - 1),
                       "2008": float((1 + ret.loc["2008-01-01":"2008-12-31"]).prod() - 1),
                       "2022": float((1 + ret.loc["2022-01-01":"2022-12-31"]).prod() - 1)}
    crash["swing"] = {"worst 12m": SWING_CRASH}
    crash["scalp"] = {"worst 12m": SCALP_CRASH}
    return out, crash


def evaluate(df: pd.DataFrame, crash: dict) -> pd.DataFrame:
    names = ["scalp", "dip", "trend", "swing"]
    R = df[names].to_numpy()
    years = df.index.year.to_numpy()
    rng = np.random.default_rng(3)
    T1, T3, n, block = 252, 756, 4000, 21
    def boot(T, seed):
        g = np.random.default_rng(seed)
        st = g.integers(0, len(R) - block, size=(n, T // block + 1))
        return (st[:, :, None] + np.arange(block)).reshape(n, -1)[:, :T]
    i1, i3 = boot(T1, 1), boot(T3, 2)
    grids = {"scalp": np.arange(0, 0.61, 0.05), "dip": np.arange(0, 0.31, 0.05), "trend": np.arange(0, 0.31, 0.1), "swing": np.arange(0, 1.01, 0.05)}
    rows = []
    for ws, wd, wt in itertools.product(grids["scalp"], grids["dip"], grids["trend"]):
        for wsw in grids["swing"]:
            if ws + wd + wt + wsw > 1.0001:
                continue
            w = np.array([ws, wd, wt, wsw])
            if w.sum() == 0:
                continue
            hist = account(R[None, :, :], w)[0]
            h = AO.hist_metrics(hist, years)
            b1 = account(R[i1], w)
            b3 = account(R[i3], w)
            stress = sum(w[k] * crash[nm]["worst 12m"] for k, nm in enumerate(names))
            rows.append({"scalp": ws, "dip": wd, "trend": wt, "swing": wsw, "cash": 1 - w.sum(), **h,
                         "1y p5": np.quantile(b1[:, -1] - 1, 0.05), "1y P(loss)": np.mean(b1[:, -1] < 1),
                         "3y med": np.median(b3[:, -1] - 1), "3y p5": np.quantile(b3[:, -1] - 1, 0.05),
                         "3y DD p5": np.quantile((b3 / np.maximum.accumulate(b3, axis=1) - 1).min(axis=1), 0.05),
                         "stress": stress})
    return pd.DataFrame(rows)


def account(R: np.ndarray, w: np.ndarray, year_len: int = 252) -> np.ndarray:
    """R (paths, days, sleeves) -> account value with yearly rebalance to weights w (rest cash)."""
    n, T, _ = R.shape
    out = np.empty((n, T))
    v = np.ones(n)
    for a in range(0, T, year_len):
        b = min(T, a + year_len)
        grow = np.cumprod(1 + R[:, a:b, :], axis=1)
        out[:, a:b] = v[:, None] * ((grow * w).sum(axis=2) + (1 - w.sum()))
        v = out[:, b - 1]
    return out


def show(t: pd.DataFrame, title: str) -> None:
    cols = ["scalp", "dip", "trend", "swing", "cash", "CAGR", "after tax", "Sharpe", "maxDD", "worst yr", "1y p5", "1y P(loss)", "3y med", "3y p5", "stress"]
    f = t[cols].copy()
    for c in ("scalp", "dip", "trend", "swing", "cash", "1y P(loss)"):
        f[c] = f[c].map("{:.0%}".format)
    for c in ("CAGR", "after tax", "maxDD", "worst yr", "1y p5", "3y med", "3y p5", "stress"):
        f[c] = f[c].map("{:+.1%}".format)
    f["Sharpe"] = f["Sharpe"].map("{:.2f}".format)
    print(f"\n{title}")
    print(f.to_string(index=False))


def main() -> None:
    df, crash = sleeves()
    print(f"{df.index[0]:%Y-%m} .. {df.index[-1]:%Y-%m}")
    for c in df:
        x = df[c]
        print(f"  {c:6s} mean {x.mean() * 252:+6.1%}/yr vol {x.std() * np.sqrt(252):5.1%} Sharpe {x.mean() / x.std() * np.sqrt(252):5.2f} "
              f"in market {(x != 0).mean():4.0%}")
    print("  correlations:\n" + df.corr().round(2).to_string())
    print("  SOXL sleeves since 1995 (synthetic):", {k: {kk: f"{vv:+.0%}" for kk, vv in v.items()} for k, v in crash.items() if k in ("dip", "trend")})
    t = evaluate(df, crash)
    t.to_parquet(AO.DATA_DIR / "research" / "allocation3.parquet")
    ok = t[(t["maxDD"] >= -0.20) & (t["1y p5"] >= -0.10) & (t["stress"] >= -0.25)]
    print(f"\n{len(t)} mixes tested, {len(ok)} pass all three survival rules")
    ref = t[((t["scalp"] - 0.3).abs() < 1e-9) & (t["dip"] == 0) & (t["trend"] == 0) & ((t["swing"] - 0.7).abs() < 1e-9)]
    show(ref, "Current 30/70 for reference:")
    show(ok.sort_values("3y med", ascending=False).head(12), "Survivors with the highest median 3-year growth:")
    show(ok.sort_values("Sharpe", ascending=False).head(5), "Survivors with the best Sharpe:")
    show(t.sort_values("3y med", ascending=False).head(5), "Highest growth regardless of survival rules (for contrast):")
    best = ok.sort_values("3y med", ascending=False).iloc[0]
    # how much does each piece of the winner matter? remove one at a time
    print("\nWinner without each piece (money moved to swing):")
    for k in ("scalp", "dip", "trend"):
        if best[k] > 0:
            alt = t[(np.isclose(t[k], 0)) & np.isclose(t["swing"], best["swing"] + best[k]) &
                    np.all([np.isclose(t[o], best[o]) for o in ("scalp", "dip", "trend") if o != k], axis=0)]
            if len(alt):
                a = alt.iloc[0]
                print(f"  no {k:5s}: 3y med {a['3y med']:+.1%} (vs {best['3y med']:+.1%}), maxDD {a['maxDD']:+.1%}, 1y p5 {a['1y p5']:+.1%}, stress {a['stress']:+.1%}")


if __name__ == "__main__":
    main()
