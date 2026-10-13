"""SOXL vs SOXL + SOXS on daily-to-weekly horizons, and what follows semiconductor crashes.

History: PHLX Semiconductor index (^SOX) since 1994 with synthetic +3x / -3x daily returns
(calibrated to the real SOXL / SOXS since 2010), plus the real ETFs since 2010.
Costs: Toss 0.1% + 0.05% slippage per side; cash earns T-bills.
Execution: signal on the close; "MOC" = traded at that close (live: computed at 15:55 ET from
live prices), "next" = traded at the next close (conservative).

  Part 1  event study: forward SOXL / SOXS returns after crashes, sharp drops, dead-cat bounces
  Part 2  strategies: buy & hold, trend (SOXL/cash vs SOXL/SOXS), dip-buying, rally-fading
          with SOXS, crash-bounce and dead-cat-fade trades, combinations
  Part 3  today's situation matched against history

    python -m trader.research.soxl_soxs
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

from ..config import DATA_DIR
from ..macro import UA, fred

CACHE = DATA_DIR / "research" / "soxl_soxs.parquet"
COST = 0.0015
FEE = {3: 0.0075, -3: 0.0102}  # SOXL / SOXS expense ratios


def yahoo(symbol: str, start: str = "1994-01-01") -> pd.DataFrame:
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.now(timezone.utc).timestamp())
    r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                     params={"period1": p1, "period2": p2, "interval": "1d", "events": "div,split"}, headers=UA, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("America/New_York").tz_localize(None).normalize()
    df = pd.DataFrame({"open": q["open"], "high": q["high"], "low": q["low"], "close": q["close"], "adj": adj}, index=idx)
    df = df[~df.index.duplicated(keep="last")].dropna(subset=["close"])
    f = df["adj"] / df["close"]
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] * f
    return df.drop(columns="adj")


def load() -> dict[str, pd.DataFrame]:
    if CACHE.exists():
        d = pd.read_parquet(CACHE)
        return {s: g.drop(columns="symbol").set_index("date") for s, g in d.groupby("symbol")}
    out = {s: yahoo(s) for s in ("^SOX", "SOXX", "SOXL", "SOXS")}
    tb = fred("DTB3", "1994-01-01")
    out["TBILL"] = pd.DataFrame({"close": tb})
    rows = [g.assign(symbol=s, date=g.index) for s, g in out.items()]
    pd.concat(rows, ignore_index=True).to_parquet(CACHE)
    return out


def synth(r: pd.Series, rf: pd.Series, lev: int, spread: float) -> pd.Series:
    """Daily return of a lev x daily-reset fund: lev*r - (lev-1)*rf - fees - |lev|*spread."""
    return lev * r - (lev - 1) * rf - (FEE[lev] + abs(lev) * spread) / 252


def calibrate(r: pd.Series, rf: pd.Series, real: pd.Series, lev: int) -> float:
    d = pd.concat([r, rf, real.pct_change()], axis=1, keys=["r", "rf", "e"]).dropna()
    target = np.log1p(d["e"]).sum()
    grid = np.arange(-0.03, 0.05, 0.0005)
    errs = [abs(np.log1p(synth(d["r"], d["rf"], lev, s)).sum() - target) for s in grid]
    s = float(grid[int(np.argmin(errs))])
    corr = synth(d["r"], d["rf"], lev, s).corr(d["e"])
    print(f"  {'SOXL' if lev > 0 else 'SOXS'} synthetic: spread {s:+.2%}/yr, daily corr {corr:.3f} vs real since {d.index[0]:%Y-%m}")
    return s


def rsi(close: pd.Series, n: int = 2) -> pd.Series:
    d = close.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    au, ad = up.ewm(alpha=1 / n, adjust=False).mean(), dn.ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + au / ad.replace(0, np.nan))


# ── Part 1: events ────────────────────────────────────────────────────────
def fwd(ret: pd.Series, i: int, k: int) -> float:
    seg = ret.iloc[i + 1 : i + 1 + k]
    return float(np.prod(1 + seg.to_numpy()) - 1) if len(seg) == k else np.nan


def events(mask: pd.Series, gap: int = 20) -> list[int]:
    idx, last = [], -10**9
    for i, m in enumerate(mask.to_numpy()):
        if m and i - last >= gap:
            idx.append(i)
            last = i
    return idx


def event_table(name: str, ev: list[int], L: pd.Series, S: pd.Series, X: pd.Series) -> None:
    if len(ev) < 5:
        print(f"{name:70s} n={len(ev)} (too few)")
        return
    out = []
    for k in (5, 10, 20, 60):
        l = np.array([fwd(L, i, k) for i in ev])
        s = np.array([fwd(S, i, k) for i in ev])
        x = np.array([fwd(X, i, k) for i in ev])
        ok = np.isfinite(l)
        out.append(f"{k:>2}d SOX {np.nanmedian(x):+5.1%} | SOXL med {np.nanmedian(l):+6.1%} up {np.mean(l[ok] > 0):3.0%} | SOXS med {np.nanmedian(s):+6.1%} up {np.mean(s[ok] > 0):3.0%}")
    print(f"{name}  (n={len(ev)})")
    for line in out:
        print("      " + line)


def part1(px: pd.Series, L: pd.Series, S: pd.Series, X: pd.Series) -> None:
    print("\n=== PART 1: what followed semiconductor crashes (^SOX since 1994; synthetic SOXL/SOXS) ===")
    hi = px.rolling(252, min_periods=60).max()
    dd = px / hi - 1
    r5 = px.pct_change(5)
    sma200 = px.rolling(200).mean()
    up = px > sma200
    for th in (-0.20, -0.30, -0.40):
        event_table(f"SOX first closes {th:.0%} below its 1-year high", events((dd <= th) & (dd.shift(1) > th), 60), L, S, X)
    for th in (-0.08, -0.12):
        event_table(f"SOX 5-day drop <= {th:.0%}, ABOVE 200-day avg (correction in an uptrend)", events((r5 <= th) & up), L, S, X)
        event_table(f"SOX 5-day drop <= {th:.0%}, BELOW 200-day avg (bear market)", events((r5 <= th) & ~up), L, S, X)
    # dead-cat bounce: after a 30% crash, a +15% bounce within 20 days -> does a new low follow?
    ev = events((dd <= -0.30) & (dd.shift(1) > -0.30), 60)
    bounced = newlow = 0
    for i in ev:
        low = px.iloc[i]
        seg = px.iloc[i : i + 21]
        lo_i = seg.idxmin()
        after = px.loc[lo_i:].iloc[:21]
        if (after.max() / seg.min() - 1) >= 0.15:
            bounced += 1
            peak_i = after.idxmax()
            nxt = px.loc[peak_i:].iloc[1:121]
            if len(nxt) and nxt.min() < seg.min():
                newlow += 1
    print(f"Dead-cat check: of {len(ev)} 30% crashes, {bounced} bounced >= +15% within ~20 days of the low; "
          f"{newlow} of those then made a NEW low within 6 months")


# ── Part 2: strategies ────────────────────────────────────────────────────
def backtest(wl: pd.Series, ws: pd.Series, L: pd.Series, S: pd.Series, rf: pd.Series, lag: int) -> pd.Series:
    wl, ws = wl.shift(lag).fillna(0.0), ws.shift(lag).fillna(0.0)
    turn = wl.diff().abs().fillna(wl.abs()) + ws.diff().abs().fillna(ws.abs())
    return wl * L + ws * S + (1 - wl - ws).clip(lower=0) * rf - turn * COST


def stats(r: pd.Series, rf: pd.Series, wl=None, ws=None, lag=1) -> dict:
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    ex = r - rf.reindex(r.index).fillna(0)
    out = {"CAGR": eq.iloc[-1] ** (1 / yrs) - 1, "Sharpe": ex.mean() / ex.std() * np.sqrt(252) if ex.std() else 0,
           "maxDD": (eq / eq.cummax() - 1).min(), "worst yr": (1 + r).groupby(r.index.year).prod().min() - 1}
    if wl is not None:
        w = (wl.shift(lag).fillna(0) + ws.shift(lag).fillna(0)).reindex(r.index).fillna(0)
        out["in mkt"] = (w > 0).mean()
        out["entries/yr"] = ((w > 0) & (w.shift(1).fillna(0) == 0)).sum() / yrs
    return out


def fmt(name: str, m: dict) -> str:
    s = f"{name:58s} CAGR {m['CAGR']:+7.1%} Sharpe {m['Sharpe']:5.2f} maxDD {m['maxDD']:6.1%} worst yr {m['worst yr']:+6.1%}"
    if "in mkt" in m:
        s += f" in {m['in mkt']:4.0%} entries/yr {m['entries/yr']:4.1f}"
    return s


def hold_until(entry: pd.Series, exit_: pd.Series, max_days: int) -> pd.Series:
    """1 from an entry day until the exit condition (or max_days), else 0."""
    e, x = entry.to_numpy(), exit_.to_numpy()
    w = np.zeros(len(e))
    i = 0
    while i < len(e):
        if e[i]:
            j = i
            while j < len(e) and j - i < max_days:
                w[j] = 1
                if j > i and x[j]:
                    break
                j += 1
            i = j + 1
        else:
            i += 1
    return pd.Series(w, index=entry.index)


def rules(px: pd.Series, r: pd.Series) -> dict[str, tuple[pd.Series, pd.Series]]:
    z = pd.Series(0.0, index=px.index)
    sma200, sma5 = px.rolling(200).mean(), px.rolling(5).mean()
    up = (px > sma200).astype(float)
    vol20 = r.rolling(20).std() * np.sqrt(252)
    vt = ((0.40 / (3 * vol20)).clip(upper=1.0) * 4).round() / 4
    r2 = rsi(px, 2)
    hi = px.rolling(252, min_periods=60).max()
    dd = px / hi - 1
    r5, r10 = px.pct_change(5), px.pct_change(10)
    low20 = px.rolling(20).min()
    out = {
        "SOXL buy & hold": (z + 1, z),
        "SOXS buy & hold": (z, z + 1),
        "Trend: SOXL above 200d, else cash": (up, z),
        "Trend: SOXL above 200d, else SOXS": (up, 1 - up),
        "Trend + vol target: SOXL / cash": (up * vt, z),
        "Trend + vol target: SOXL / SOXS": (up * vt, (1 - up) * vt),
    }
    dip_any = hold_until(r2 < 10, px > sma5, 10)
    dip_up = hold_until((r2 < 10) & (up > 0), px > sma5, 10)
    fade_dn = hold_until((r2 > 90) & (up == 0), px < sma5, 10)
    fade_any = hold_until(r2 > 90, px < sma5, 10)
    out["Dip-buy SOXL (RSI2<10, exit above 5d avg), any regime"] = (dip_any, z)
    out["Dip-buy SOXL, uptrend only"] = (dip_up, z)
    out["Rally-fade SOXS (RSI2>90, exit below 5d avg), downtrend only"] = (z, fade_dn)
    out["Rally-fade SOXS, any regime"] = (z, fade_any)
    out["Dip-buy SOXL in uptrend + rally-fade SOXS in downtrend"] = (dip_up, fade_dn)
    out["Dip-buy SOXL any regime + rally-fade SOXS in downtrend"] = (dip_any, fade_dn * (dip_any == 0))
    for k in (5, 10, 20):
        cap = (dd <= -0.25) & (r5 <= -0.10)
        out[f"Crash bounce: SOXL {k}d after a 25% crash + 10% 5-day drop"] = (hold_until(cap, z.astype(bool), k), z)
    for k in (10, 20):
        dc = (dd <= -0.25) & (up == 0) & (px / low20 - 1 >= 0.15)
        out[f"Dead-cat fade: SOXS {k}d after a +15% bounce in a bear market"] = (z, hold_until(dc, z.astype(bool), k))
    trend_l = up * vt
    out["Trend+vol SOXL + dip-buys in uptrend (max 100%)"] = ((trend_l + dip_up * (1 - trend_l)).clip(upper=1), z)
    out["Trend+vol SOXL/SOXS + dip/fade overlays (max 100%)"] = (
        (trend_l + dip_up * (1 - trend_l)).clip(upper=1),
        ((1 - up) * vt + fade_dn * (1 - (1 - up) * vt)).clip(upper=1))
    return out


def part2(px, r, L, S, rf, realL, realS) -> dict:
    print("\n=== PART 2: strategies (synthetic 3x, 0.15%/side) ===")
    rs = rules(px, r)
    periods = [("1995-2026", "1995-06-01", None), ("1995-2012 (in-sample)", "1995-06-01", "2012-12-31"),
               ("2013-2026 (out-of-sample)", "2013-01-01", None)]
    for lag, lag_name in ((1, "MOC: traded at the signal close"), (2, "next close")):
        for label, a, b in periods:
            if lag == 2 and label != "1995-2026":
                continue
            print(f"\n--- {label}, {lag_name} ---")
            for name, (wl, ws) in rs.items():
                ret = backtest(wl, ws, L, S, rf, lag).loc[a:b]
                print(fmt(name, stats(ret, rf, wl.loc[a:b], ws.loc[a:b], lag)))
    print("\n--- real SOXL/SOXS since 2010, MOC ---")
    idx = realL.index.intersection(realS.index)
    rl, rsx = realL.reindex(idx).pct_change().fillna(0), realS.reindex(idx).pct_change().fillna(0)
    for name in ("SOXL buy & hold", "Trend + vol target: SOXL / cash", "Trend + vol target: SOXL / SOXS",
                 "Dip-buy SOXL, uptrend only", "Dip-buy SOXL in uptrend + rally-fade SOXS in downtrend",
                 "Trend+vol SOXL + dip-buys in uptrend (max 100%)", "Trend+vol SOXL/SOXS + dip/fade overlays (max 100%)"):
        wl, ws = rs[name]
        wl, ws = wl.reindex(idx).fillna(0), ws.reindex(idx).fillna(0)
        print(fmt(name, stats(backtest(wl, ws, rl, rsx, rf.reindex(idx).fillna(0), 1).iloc[1:], rf, wl, ws)))
    return rs


def trade_stats(px, L, S, rs) -> None:
    print("\n--- per-trade view of the short-horizon rules (MOC, net of costs) ---")
    for name in ("Dip-buy SOXL, uptrend only", "Dip-buy SOXL (RSI2<10, exit above 5d avg), any regime",
                 "Rally-fade SOXS (RSI2>90, exit below 5d avg), downtrend only", "Crash bounce: SOXL 10d after a 25% crash + 10% 5-day drop",
                 "Dead-cat fade: SOXS 20d after a +15% bounce in a bear market"):
        wl, ws = rs[name]
        w = (wl + ws).shift(1).fillna(0)
        leg = L if wl.sum() > 0 else S
        trades, cur = [], None
        for t, v in w.items():
            if v > 0 and cur is None:
                cur = [t, 1.0]
            if v > 0:
                cur[1] *= 1 + leg.get(t, 0.0)
            elif cur is not None:
                trades.append((cur[0], cur[1] * (1 - COST) ** 2 - 1))
                cur = None
        t = pd.DataFrame(trades, columns=["d", "r"])
        if t.empty:
            continue
        a, b = t[t["d"] < "2013-01-01"]["r"], t[t["d"] >= "2013-01-01"]["r"]
        print(f"{name:62s} n {len(t):3d} avg {t['r'].mean():+6.2%} median {t['r'].median():+6.2%} win {(t['r'] > 0).mean():3.0%} "
              f"worst {t['r'].min():+6.1%} | pre-2013 {a.mean():+6.2%} (n{len(a)}) post {b.mean():+6.2%} (n{len(b)})")


def part3(px, r, L, S, rs) -> None:
    print("\n=== PART 3: today, matched against history ===")
    sma200, sma50 = px.rolling(200).mean(), px.rolling(50).mean()
    hi = px.rolling(252, min_periods=60).max()
    dd = px / hi - 1
    r5, r20 = px.pct_change(5), px.pct_change(20)
    r2 = rsi(px, 2)
    t = px.index[-1]
    print(f"SOX {px.iloc[-1]:.0f} on {t:%Y-%m-%d}: {dd.iloc[-1]:+.1%} from 1-year high, 5d {r5.iloc[-1]:+.1%}, 20d {r20.iloc[-1]:+.1%}, "
          f"vs 200d {px.iloc[-1] / sma200.iloc[-1] - 1:+.1%}, vs 50d {px.iloc[-1] / sma50.iloc[-1] - 1:+.1%}, RSI2 {r2.iloc[-1]:.0f}")
    like = (r5 <= -0.05) & (px > sma200) & (dd <= -0.10) & (dd >= -0.30)
    event_table("Similar setups: 5d <= -5%, above 200d avg, 10-30% off the high", events(like, 10), L, S, px.pct_change())
    like2 = like & (r20 > 0)
    event_table("  ...and after a bounce (20-day return still positive)", events(like2, 10), L, S, px.pct_change())
    print("What each rule holds today:")
    for name, (wl, ws) in rs.items():
        if wl.iloc[-1] or ws.iloc[-1]:
            print(f"   {name:58s} SOXL {wl.iloc[-1]:.0%}  SOXS {ws.iloc[-1]:.0%}")


def orb_split() -> None:
    from . import scalp
    m, days = scalp.load_matrices()
    t = scalp.e2_orb(m, days, 30, "opposite", 0.0)
    print("\n=== Intraday breakout (live scalp sleeve): SOXL legs vs SOXS legs, per trade net ===")
    for reg in ("bear", "bull"):
        for sym in ("SOXL", "SOXS"):
            x = t[(t["regime"] == reg) & (t["sym"] == sym)]["net"]
            print(f"   {reg:4s} regime {sym}: n {len(x):3d} avg {x.mean():+.3%} t {x.mean() / x.std() * np.sqrt(len(x)):+.1f}")


def main() -> None:
    d = load()
    sox = d["^SOX"]["close"].dropna()
    rf = (d["TBILL"]["close"].reindex(sox.index).ffill() / 100 / 252).fillna(0)
    r = sox.pct_change().fillna(0)
    print("Calibration:")
    sL = calibrate(r, rf, d["SOXL"]["close"], 3)
    sS = calibrate(r, rf, d["SOXS"]["close"], -3)
    L, S = synth(r, rf, 3, sL), synth(r, rf, -3, sS)
    part1(sox, L, S, r)
    rs = part2(sox, r, L, S, rf, d["SOXL"]["close"], d["SOXS"]["close"])
    trade_stats(sox, L, S, rs)
    part3(sox, r, L, S, rs)
    orb_split()


if __name__ == "__main__":
    main()
