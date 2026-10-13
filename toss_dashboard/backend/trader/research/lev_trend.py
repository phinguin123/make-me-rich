"""Leveraged-ETF trend rules: hold TQQQ / SOXL (3x) only in uptrends, T-bills otherwise.

History is extended before the ETFs existed with synthetic daily 3x returns of the index
(Nasdaq-100 ^NDX since 1985, PHLX Semiconductor ^SOX since 1994):
    r_3x = 3 r_index - 2 r_tbill - fees/252 - spread/252
with the swap-financing spread calibrated so the synthetic series matches the real ETF since 2010.
Signals use the index close; trades happen at the NEXT day's close (no look-ahead), paying Toss's
0.1% commission + 0.05% slippage per side. Cash earns the 3-month T-bill rate (FRED DTB3).

    python -m trader.research.lev_trend
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

from ..config import DATA_DIR
from ..macro import UA, fred

CACHE = DATA_DIR / "research" / "lev_trend.parquet"
COST = 0.0015  # per side: commission + slippage
FEES = {"TQQQ": 0.0084, "SOXL": 0.0075}  # current expense ratios
PAIRS = {"TQQQ": "^NDX", "SOXL": "^SOX"}


def yahoo_adj(symbol: str, start: str = "1985-01-01") -> pd.Series:
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.now(timezone.utc).timestamp())
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"period1": p1, "period2": p2, "interval": "1d", "events": "div,split"}, headers=UA, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    adj = res["indicators"].get("adjclose", [{}])[0].get("adjclose") or res["indicators"]["quote"][0]["close"]
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("America/New_York").tz_localize(None).normalize()
    s = pd.Series(adj, index=idx, name=symbol, dtype=float)
    return s[~s.index.duplicated(keep="last")].dropna()


def load() -> pd.DataFrame:
    if CACHE.exists():
        return pd.read_parquet(CACHE)
    cols = {s: yahoo_adj(s) for s in ("^NDX", "^SOX", "TQQQ", "SOXL", "QQQ", "SOXX")}
    df = pd.DataFrame(cols).sort_index()
    tb = fred("DTB3", "1985-01-01").reindex(df.index).ffill()
    df["tbill"] = tb
    df = df[df["^NDX"].notna()]
    df.to_parquet(CACHE)
    return df


def synth(idx_ret: pd.Series, rf: pd.Series, lev: float, fee: float, spread: float) -> pd.Series:
    return lev * idx_ret - (lev - 1) * rf - (fee + (lev - 1) * spread) / 252


def calibrate(df: pd.DataFrame, etf: str) -> float:
    """Financing spread (per unit of borrowed exposure) that matches the real ETF's growth."""
    idx = PAIRS[etf]
    d = df[[idx, etf, "tbill"]].dropna()
    r_i, r_e, rf = d[idx].pct_change(), d[etf].pct_change(), d["tbill"] / 100 / 252
    real = np.log1p(r_e.dropna()).sum()
    best, err = 0.0, 1e9
    for spread in np.arange(-0.02, 0.04, 0.0005):
        s = np.log1p(synth(r_i, rf, 3, FEES[etf], spread).dropna()).sum()
        if abs(s - real) < err:
            best, err = spread, abs(s - real)
    corr = synth(r_i, rf, 3, FEES[etf], best).corr(r_e)
    print(f"{etf}: synthetic matches the real ETF since {d.index[0]:%Y-%m} with spread {best:+.2%}/yr (daily corr {corr:.3f})")
    return float(best)


def run(r_lev: pd.Series, rf: pd.Series, weight: pd.Series) -> pd.Series:
    """Daily strategy returns: weight decided at close t-1 -> executed at close t -> earns from t+1."""
    w = weight.shift(2).fillna(0.0)  # signal at t-2 close, traded at t-1 close, earning day t
    turnover = w.diff().abs().fillna(w.abs())
    return w * r_lev + (1 - w) * rf - turnover * COST


def metrics(ret: pd.Series, rf: pd.Series, w: pd.Series | None = None) -> dict:
    ret = ret.dropna()
    eq = (1 + ret).cumprod()
    yrs = len(ret) / 252
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    vol = ret.std() * np.sqrt(252)
    ex = ret - rf.reindex(ret.index).fillna(0)
    out = {"CAGR": cagr, "vol": vol, "Sharpe": ex.mean() / ex.std() * np.sqrt(252), "maxDD": (eq / eq.cummax() - 1).min(),
           "worst yr": (1 + ret).groupby(ret.index.year).prod().min() - 1}
    if w is not None:
        ww = w.reindex(ret.index).shift(2).fillna(0)
        out["in mkt"] = (ww > 0).mean()
        out["trades/yr"] = (ww.diff().abs() > 0.05).sum() / yrs
    return out


def fmt(name: str, m: dict) -> str:
    s = f"{name:44s} CAGR {m['CAGR']:+7.1%}  vol {m['vol']:5.0%}  Sharpe {m['Sharpe']:5.2f}  maxDD {m['maxDD']:6.1%}  worst yr {m['worst yr']:+6.1%}"
    if "in mkt" in m:
        s += f"  in {m['in mkt']:4.0%}  trades/yr {m['trades/yr']:4.1f}"
    return s


def rules(px: pd.Series, r_idx: pd.Series) -> dict[str, pd.Series]:
    sma = {n: px.rolling(n).mean() for n in (100, 150, 200)}
    vol20 = r_idx.rolling(20).std() * np.sqrt(252)
    out = {f"SMA{n}": (px > s).astype(float) for n, s in sma.items()}
    band = pd.Series(0.0, index=px.index)
    state = 0.0
    for t, (p, s) in enumerate(zip(px.to_numpy(), sma[200].to_numpy())):
        if np.isfinite(s):
            state = 1.0 if p > s * 1.02 else 0.0 if p < s * 0.98 else state
        band.iloc[t] = state
    out["SMA200 +-2% band"] = band
    for target in (0.40, 0.60):  # leveraged position's volatility target, only in uptrends
        w = (target / (3 * vol20)).clip(upper=1.0)
        w = (w * 4).round() / 4  # quarter steps so it doesn't trade every day
        out[f"SMA200 + vol target {target:.0%}"] = out["SMA200"] * w
    return out


def main() -> None:
    df = load()
    rf_all = df["tbill"].ffill() / 100 / 252
    for etf, idx in PAIRS.items():
        spread = calibrate(df, etf)
        px = df[idx].dropna()
        r_idx = px.pct_change()
        rf = rf_all.reindex(px.index).fillna(0)
        r3 = synth(r_idx, rf, 3, FEES[etf], spread)
        r2 = synth(r_idx, rf, 2, FEES[etf] * 0.95, spread)
        rs = rules(px, r_idx)
        periods = [("full", px.index[260], px.index[-1])]
        periods += [(f"{a}-{b}", pd.Timestamp(f"{a}-01-01"), pd.Timestamp(f"{b}-12-31")) for a, b in ((1995 if etf == "SOXL" else 1990, 2009), (2010, 2026))]
        for label, a, b in periods:
            sl = slice(max(a, px.index[260]), b)
            print(f"\n=== {etf} ({idx}) {label}: {px.loc[sl].index[0]:%Y-%m} .. {px.loc[sl].index[-1]:%Y-%m} ===")
            print(fmt("index 1x buy & hold", metrics(r_idx.loc[sl], rf)))
            print(fmt("2x buy & hold (synthetic)", metrics(r2.loc[sl], rf)))
            print(fmt("3x buy & hold (synthetic)", metrics(r3.loc[sl], rf)))
            for name, w in rs.items():
                print(fmt(f"3x {name}", metrics(run(r3, rf, w).loc[sl], rf, w)))
            for name in ("SMA200", "SMA200 +-2% band"):
                print(fmt(f"2x {name}", metrics(run(r2, rf, rs[name]).loc[sl], rf, rs[name])))
        real = df[etf].dropna().pct_change()
        rfr = rf_all.reindex(real.index).fillna(0)
        w = rs["SMA200"].reindex(real.index).ffill()
        print(f"\n--- real {etf} since {real.index[0]:%Y-%m} (no synthetic) ---")
        print(fmt(f"{etf} buy & hold", metrics(real.iloc[1:], rfr)))
        print(fmt(f"{etf} SMA200", metrics(run(real, rfr, w).iloc[1:], rfr, w)))
        b = rs["SMA200 +-2% band"].reindex(real.index).ffill()
        print(fmt(f"{etf} SMA200 +-2% band", metrics(run(real, rfr, b).iloc[1:], rfr, b)))
        print("crashes (3x SMA200 vs 3x buy & hold):")
        for a, b_ in (("2000-03-01", "2002-10-31"), ("2007-10-01", "2009-03-31"), ("2020-02-15", "2020-04-30"), ("2022-01-01", "2022-12-31")):
            sl = slice(a, b_)
            if px.loc[sl].empty:
                continue
            s1 = (1 + run(r3, rf, rs["SMA200"]).loc[sl]).prod() - 1
            s2 = (1 + r3.loc[sl]).prod() - 1
            print(f"   {a[:7]}..{b_[:7]}: SMA200 {s1:+6.1%}  vs  buy & hold {s2:+6.1%}")


if __name__ == "__main__":
    main()
