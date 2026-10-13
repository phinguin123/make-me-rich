"""Pre-market SOXL dips: SOXL's own VWAP vs fair value from the underlying semis.

Within one day a 3x daily-reset fund is worth prev_close * (1 + 3 * underlying return since the prior
close). So intraday, SOXL's chart is the underlying's chart stretched 3x; across days it is not
(compounding decay), which is why "don't read leveraged charts" applies to multi-day charts.

Questions:
  1. How closely does SOXL follow 3x its underlying in the pre-market, and which proxy tracks best
     (SOXX, SMH, a basket of SMH/NVDA/MU)? How much do SOXX and SOXL trade pre-market?
  2. When the live rule buys (SOXL 0.4 sigma below its pre-market VWAP, 0.3% through, 07:00-09:00 ET),
     was the dip SOXL-only (SOXL below its fair value) or the whole sector? Which kind rebounds?
  3. Alternatives: buy SOXL when it trades x% below fair value; or when the underlying dips below
     its own VWAP.
Exits as live: take profit at +dip, else 09:25 ET. Toss costs; limit entries (no slippage).

    python -m trader.research.pre_fairvalue
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .scalp_ext import D, ET, net, show

SYMS = ("SOXL", "SOXX", "SMH", "NVDA", "MU", "QQQ")


def load():
    a = pd.read_parquet(D / "alpaca_1m.parquet")
    a = a[a["symbol"].isin(SYMS)]
    a["et"] = a["ts"].dt.tz_convert(ET)
    a["date"] = a["et"].dt.tz_localize(None).dt.normalize()
    a["m"] = (a["et"].dt.hour - 4) * 60 + a["et"].dt.minute
    a = a[(a["m"] >= 0) & (a["m"] < 720)]
    M = {}
    for s in SYMS:
        x = a[a["symbol"] == s]
        for f in ("high", "low", "close", "volume"):
            p = x.pivot_table(index="date", columns="m", values=f, aggfunc="last").reindex(columns=range(720))
            M[(s, f)] = p.ffill(axis=1) if f == "close" else p
    days = M[("SOXL", "close")].index
    for k in list(M):
        M[k] = M[k].reindex(days)
    return M, days


def main() -> None:
    M, days = load()
    C = {s: M[(s, "close")] for s in SYMS}
    V = {s: M[(s, "volume")].fillna(0) for s in SYMS}
    prev = {s: C[s][719].shift(1) for s in SYMS}
    pre = slice(180, 330)  # 07:00-09:30 ET
    print("=== 1. Pre-market activity and tracking (07:00-09:30 ET, 2021-2026) ===")
    for s in ("SOXL", "SOXX", "SMH", "NVDA", "MU"):
        vol = V[s].iloc[:, pre].sum(axis=1)
        traded = (V[s].iloc[:, pre] > 0).mean(axis=1)
        print(f"  {s:5s} median pre-market volume {vol.median():>10,.0f} shares, minutes with trades {traded.median():.0%}")
    ret = {s: C[s].div(prev[s], axis=0) - 1 for s in SYMS}
    proxies = {"3x SOXX": 3 * ret["SOXX"], "3x SMH": 3 * ret["SMH"],
               "3x basket (SMH, NVDA, MU)": 3 * (ret["SMH"] + ret["NVDA"] + ret["MU"]) / 3}
    for name, fv in proxies.items():
        dis = (ret["SOXL"] - fv).iloc[:, pre]
        d1 = ret["SOXL"].iloc[:, pre].diff(axis=1).stack()
        f1 = fv.iloc[:, pre].diff(axis=1).stack()
        ok = d1.notna() & f1.notna() & (d1 != 0)
        print(f"  SOXL vs {name:28s} minute-return corr {np.corrcoef(d1[ok], f1[ok])[0, 1]:.2f}; "
              f"gap SOXL - fair value: median |gap| {dis.abs().stack().median():.2%}, 95th pct {dis.abs().stack().quantile(0.95):.2%}")
    fv = proxies["3x basket (SMH, NVDA, MU)"]
    print("\n=== 2. The live rule's trades: SOXL-only dip or the whole sector? ===")
    H, L, Vs, Cs = M[("SOXL", "high")], M[("SOXL", "low")], V["SOXL"], C["SOXL"]
    sig = np.log(Cs[719]).diff().rolling(20).std().shift(1)
    rows = []
    for d in days:
        s = sig.get(d)
        if not np.isfinite(s) or not np.isfinite(prev["SOXL"].get(d, np.nan)):
            continue
        kk = min(0.06, max(0.015, 0.4 * s))
        c, lo, hi, v = Cs.loc[d].to_numpy(), L.loc[d].to_numpy(), H.loc[d].to_numpy(), Vs.loc[d].to_numpy()
        first = np.flatnonzero(np.isfinite(lo[:60]))
        if not len(first):
            continue
        st = first[0]
        pv = np.cumsum(((hi + lo + c) / 3)[st:330] * np.nan_to_num(v[st:330]))
        vv = np.cumsum(np.nan_to_num(v[st:330]))
        vwap = np.where(vv > 0, pv / np.maximum(vv, 1), np.nan)
        fvd = fv.loc[d].to_numpy()
        soxl_r = ret["SOXL"].loc[d].to_numpy()
        for j in range(max(st + 30, 180), 300):
            w = vwap[j - 1 - st]
            if not (np.isfinite(w) and np.isfinite(lo[j])) or lo[j] >= w * (1 - kk) * 0.997:
                continue
            buy = w * (1 - kk)
            ex = c[325]
            for q in range(j + 1, 326):
                if np.isfinite(hi[q]) and hi[q] >= buy * (1 + kk) * 1.0005:
                    ex = buy * (1 + kk)
                    break
            # dislocation at the trigger: SOXL's move since the prior close minus 3x the basket's
            low_r = lo[j] / prev["SOXL"][d] - 1
            gap = low_r - fvd[j] if np.isfinite(fvd[j]) else np.nan
            sector_dip = fvd[j] - fvd[max(st, j - 30)] if np.isfinite(fvd[j]) else np.nan  # basket's own 30-min move (x3)
            rows.append({"d": d, "r": ex / buy - 1, "gap": gap, "sector": sector_dip, "kk": kk})
            break
    t = pd.DataFrame(rows).dropna()
    t["net"] = net(t["r"], "pre", limit=True)
    print(f"  {len(t)} trades, avg {t['net'].mean():+.2%}")
    t["g3"] = pd.qcut(t["gap"], 3, labels=["SOXL far BELOW fair value", "middle", "SOXL near/above fair value"])
    for g, x in t.groupby("g3", observed=True):
        print(f"    {g:28s} n {len(x):3d} avg {x['net'].mean():+.2%} win {(x['net'] > 0).mean():.0%}  (gap {x['gap'].median():+.1%})")
    t["s3"] = pd.qcut(t["sector"], 3, labels=["sector also fell hard", "sector fell a bit", "sector flat/up"])
    for g, x in t.groupby("s3", observed=True):
        print(f"    {g:28s} n {len(x):3d} avg {x['net'].mean():+.2%} win {(x['net'] > 0).mean():.0%}  (basket x3, last 30 min {x['sector'].median():+.1%})")

    print("\n=== 3. Alternative signals (07:00-09:00 entries, exit +target or 09:25) ===")
    for name, fvs in (("basket", fv), ("SOXX", proxies["3x SOXX"]), ("SMH", proxies["3x SMH"])):
        for x in (0.01, 0.015, 0.02, 0.03):
            res, ds = [], []
            for d in days:
                p0 = prev["SOXL"].get(d)
                if not np.isfinite(p0):
                    continue
                c, lo, hi = Cs.loc[d].to_numpy(), L.loc[d].to_numpy(), H.loc[d].to_numpy()
                f = fvs.loc[d].to_numpy()
                for j in range(180, 300):
                    if not (np.isfinite(lo[j]) and np.isfinite(f[j - 1])):
                        continue
                    fair = p0 * (1 + f[j - 1])  # fair value from the underlying, last completed minute
                    buy = fair * (1 - x)
                    if lo[j] >= buy * 0.997:
                        continue
                    ex = c[325]
                    for q in range(j + 1, 326):
                        if np.isfinite(hi[q]) and hi[q] >= buy * (1 + x) * 1.0005:
                            ex = buy * (1 + x)
                            break
                    res.append(ex / buy - 1)
                    ds.append(d)
                    break
            show(f"SOXL {x:.1%} below fair value ({name}), tgt +{x:.1%}", res, "pre", limit=True, dates=ds)
    # the underlying's own VWAP dip -> buy SOXL
    for und in ("SMH", "SOXX"):
        Hu, Lu, Cu, Vu = M[(und, "high")], M[(und, "low")], C[und], V[und]
        for z in (0.4,):
            res, ds = [], []
            for d in days:
                s = sig.get(d)
                if not np.isfinite(s):
                    continue
                k3 = min(0.06, max(0.015, z * s)) / 3  # the same dip in underlying terms
                cu, lu, hu, vu = Cu.loc[d].to_numpy(), Lu.loc[d].to_numpy(), Hu.loc[d].to_numpy(), Vu.loc[d].to_numpy()
                c, hi = Cs.loc[d].to_numpy(), H.loc[d].to_numpy()
                pv = np.nancumsum(((hu + lu + cu) / 3)[:330] * vu[:330])
                vv = np.cumsum(vu[:330])
                vw = np.where(vv > 0, pv / np.maximum(vv, 1), np.nan)
                for j in range(180, 300):
                    if not (np.isfinite(vw[j - 1]) and np.isfinite(lu[j]) and np.isfinite(c[j])) or lu[j] >= vw[j - 1] * (1 - k3):
                        continue
                    buy = c[j] * 1.0005
                    kk = k3 * 3
                    ex = c[325]
                    for q in range(j + 1, 326):
                        if np.isfinite(hi[q]) and hi[q] >= buy * (1 + kk) * 1.0005:
                            ex = buy * (1 + kk)
                            break
                    res.append(ex / buy - 1)
                    ds.append(d)
                    break
            show(f"{und} {z} sigma/3 below ITS VWAP -> buy SOXL at the bar close", res, "pre", limit=True, dates=ds)


if __name__ == "__main__":
    main()
