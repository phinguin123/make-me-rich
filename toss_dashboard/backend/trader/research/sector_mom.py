"""Sector / industry momentum with ETFs (1x and 3x), 2008-2026 incl. the 2008 crash.

Universe: 9 SPDR sectors + 16 industry ETFs with history since 2006 or earlier. Each month-end, rank
by momentum and hold the top K equally (trade at the next close, Toss costs 0.15%/side), optionally
only ETFs above their 200-day average (else T-bills), optionally a volatility target. The 3x version
holds a synthetic 3x daily-reset fund of the same ETFs (like TECL/SOXL/LABU/FAS ...).

Parameters were kept to a small grid and judged on both halves (2008-2016 / 2017-2026).

    python -m trader.research.sector_mom
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from ..config import DATA_DIR
from ..macro import fred
from .soxl_soxs import yahoo

CACHE = DATA_DIR / "research" / "sectors.parquet"
SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB"]
INDUSTRIES = ["SMH", "IBB", "IGV", "XBI", "KRE", "XME", "XHB", "ITB", "GDX", "OIH", "XOP", "ITA", "IYT", "XRT", "KBE", "TAN"]
COST = 0.0015


def load() -> tuple[pd.DataFrame, pd.Series]:
    if CACHE.exists():
        d = pd.read_parquet(CACHE)
    else:
        cols = {}
        for s in SECTORS + INDUSTRIES + ["SPY", "QQQ"]:
            try:
                cols[s] = yahoo(s, "2000-01-01")["close"]
            except Exception as exc:  # noqa: BLE001
                print("skip", s, exc)
        d = pd.DataFrame(cols).sort_index()
        d["TBILL"] = fred("DTB3", "2000-01-01").reindex(d.index).ffill()
        d.to_parquet(CACHE)
    rf = (d.pop("TBILL").ffill() / 100 / 252).fillna(0)
    return d, rf


def momentum(px: pd.DataFrame, kind: str) -> pd.DataFrame:
    if kind == "12-1":
        return px.shift(21) / px.shift(252) - 1
    if kind == "6m":
        return px / px.shift(126) - 1
    return 0.4 * (px / px.shift(63) - 1) + 0.2 * (px / px.shift(126) - 1) + 0.2 * (px / px.shift(189) - 1) + 0.2 * (px / px.shift(252) - 1)


def weights(px: pd.DataFrame, kind: str, k: int, trend: bool) -> pd.DataFrame:
    mom = momentum(px, kind)
    sma = px.rolling(200).mean()
    month_end = px.index.to_series().groupby(px.index.to_period("M")).transform("max") == px.index.to_series()
    w = pd.DataFrame(np.nan, index=px.index, columns=px.columns)
    for t in px.index[month_end.to_numpy()]:
        m = mom.loc[t].dropna()
        if len(m) < k + 3:
            continue
        top = m.sort_values(ascending=False).index[:k]
        row = pd.Series(0.0, index=px.columns)
        for s in top:
            if not trend or px.at[t, s] > sma.at[t, s]:
                row[s] = 1.0 / k
        w.loc[t] = row
    return w.ffill().fillna(0.0)


def run(w: pd.DataFrame, rets: pd.DataFrame, rf: pd.Series, vol_target: float = 0.0) -> pd.Series:
    w = w.shift(1).fillna(0.0)  # signal at the month-end close, traded at the next close
    if vol_target:
        port = (w.shift(1) * rets).sum(axis=1)
        realized = port.rolling(63, min_periods=20).std() * np.sqrt(252)
        scale = (vol_target / realized).clip(0.25, 1.0).shift(1).fillna(1.0)
        scale = (scale * 4).round() / 4  # quarter steps to limit trading
        w = w.mul(scale, axis=0)
    turn = w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))
    held = w.sum(axis=1)
    return (w * rets).sum(axis=1) + (1 - held).clip(lower=0) * rf - turn * COST


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    yearly = (1 + r).groupby(r.index.year).prod() - 1
    return {"CAGR": eq.iloc[-1] ** (1 / yrs) - 1, "Sharpe": r.mean() / r.std() * np.sqrt(252), "maxDD": (eq / eq.cummax() - 1).min(),
            "worst yr": yearly.min(), "best yr": yearly.max()}


def line(name: str, r: pd.Series) -> str:
    a, b, f = stats(r.loc["2008":"2016"]), stats(r.loc["2017":]), stats(r.loc["2008":])
    return (f"{name:44s} CAGR {f['CAGR']:+6.1%} Sharpe {f['Sharpe']:.2f} maxDD {f['maxDD']:+6.1%} worst yr {f['worst yr']:+6.1%} best {f['best yr']:+6.0%}"
            f" | 2008-16 {a['CAGR']:+6.1%} / 2017-26 {b['CAGR']:+6.1%}")


def lever(rets: pd.DataFrame, rf: pd.Series, lev: int = 3, cost_yr: float = 0.01) -> pd.DataFrame:
    return lev * rets - (lev - 1) * rf.values[:, None] - cost_yr / 252


def results() -> dict[str, pd.Series]:
    px, rf = load()
    px = px.loc["2006-01-01":]
    universe = [c for c in SECTORS + INDUSTRIES if c in px and px[c].loc[:"2007-06-30"].notna().sum() > 200]
    p = px[universe]
    rets = p.pct_change().fillna(0.0)
    r3 = lever(rets, rf.reindex(rets.index).fillna(0))
    rf = rf.reindex(rets.index).fillna(0)
    out = {"SPY buy & hold": px["SPY"].pct_change().fillna(0), "QQQ buy & hold": px["QQQ"].pct_change().fillna(0)}
    for kind, k, trend in itertools.product(("blend", "12-1", "6m"), (1, 2, 3, 5), (False, True)):
        w = weights(p, kind, k, trend)
        out[f"1x {kind} top{k}{' +trend' if trend else ''}"] = run(w, rets, rf)
        if trend:
            out[f"3x {kind} top{k} +trend"] = run(w, r3, rf)
            out[f"3x {kind} top{k} +trend +vol40"] = run(w, r3, rf, 0.40)
    return out


def main() -> None:
    out = results()
    print("2008-01 .. 2026-10, Toss costs, monthly rebalance")
    for name, r in out.items():
        print(line(name, r.loc["2008":]))


if __name__ == "__main__":
    main()
