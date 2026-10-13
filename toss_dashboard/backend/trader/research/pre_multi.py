"""Does the pre-market dip edge exist beyond SOXL? The exact live rule (no re-tuning) on 18 liquid names.

Rule: dip = 0.4 x the name's 20-day daily volatility (1.5%..6%) below its pre-market VWAP, 0.3% through,
entries 07:00-09:00 ET, take profit at +dip, otherwise out at 09:25. Three fill models (scalp_ext._dip_trade).
Then: on how many days do several names signal together, and does a basket of signals beat SOXL alone?

    python -m trader.research.pre_multi
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .pre_multi_data import OUT as MULTI
from .scalp_ext import D, ET, _dip_trade, net, show

BASE = ["SOXL", "SOXS", "NVDA", "MU"]


def matrices() -> dict[str, dict]:
    a = pd.concat([pd.read_parquet(D / "alpaca_1m.parquet", columns=["symbol", "ts", "high", "low", "close", "volume"]),
                   pd.read_parquet(MULTI, columns=["symbol", "ts", "high", "low", "close", "volume"])])
    a = a[a["symbol"].isin(BASE) | ~a["symbol"].isin(["SOXX", "SMH", "QQQ"])]
    a["et"] = a["ts"].dt.tz_convert(ET)
    a["date"] = a["et"].dt.tz_localize(None).dt.normalize()
    a["m"] = (a["et"].dt.hour - 4) * 60 + a["et"].dt.minute
    a = a[(a["m"] >= 0) & (a["m"] < 720)]
    out = {}
    for s, g in a.groupby("symbol"):
        M = {f: g.pivot_table(index="date", columns="m", values=f, aggfunc="last").reindex(columns=range(720)) for f in ("high", "low", "close", "volume")}
        M["close"] = M["close"].ffill(axis=1)
        out[s] = M
    return out


def trades(M: dict, model: str) -> pd.DataFrame:
    C, L, H, V = M["close"], M["low"], M["high"], M["volume"]
    sig = np.log(C[719]).diff().rolling(20).std().shift(1)
    rows = []
    for d in C.index:
        s = sig.get(d)
        if not np.isfinite(s):
            continue
        c, lo, hi, v = C.loc[d].to_numpy(), L.loc[d].to_numpy(), H.loc[d].to_numpy(), V.loc[d].to_numpy()
        first = np.flatnonzero(np.isfinite(lo[:60]))
        if not len(first):
            continue
        r = _dip_trade(c, lo, hi, np.nan_to_num(v), first[0], min(0.06, max(0.015, 0.4 * s)), model)
        if r is not None:
            rows.append((d, float(net([r], "pre", limit=True)[0])))
    return pd.DataFrame(rows, columns=["date", "net"])


def main() -> None:
    mats = matrices()
    print("Live pre-market rule on each name (net of Toss fees; limit orders)")
    res = {}
    for sym in sorted(mats, key=lambda s: (s not in BASE, s)):
        out = {}
        for model in ("level", "reactive", "pessimistic"):
            t = trades(mats[sym], model)
            out[model] = t
        res[sym] = out
        lv, rc, ps = (out[m]["net"] for m in ("level", "reactive", "pessimistic"))
        tt = lambda x: x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() else 0.0  # noqa: E731
        pd_ = out["pessimistic"]
        half = pd_["date"].median() if len(pd_) else None
        h1 = pd_[pd_["date"] < half]["net"].mean() if half is not None else np.nan
        h2 = pd_[pd_["date"] >= half]["net"].mean() if half is not None else np.nan
        print(f"  {sym:5s} level n {len(lv):3d} {lv.mean():+6.2%} (t {tt(lv):+4.1f}) | reactive n {len(rc):3d} {rc.mean():+6.2%} (t {tt(rc):+4.1f}) | "
              f"pessimistic n {len(ps):3d} {ps.mean():+6.2%} (t {tt(ps):+4.1f}) halves {h1:+.2%} / {h2:+.2%}")
    # which names pass: reactive t >= 2 and pessimistic mean > 0 with both halves >= 0
    good = []
    for sym, out in res.items():
        rc, ps = out["reactive"]["net"], out["pessimistic"]
        if len(rc) < 20:
            continue
        t_rc = rc.mean() / rc.std() * np.sqrt(len(rc))
        half = ps["date"].median()
        h1, h2 = ps[ps["date"] < half]["net"].mean(), ps[ps["date"] >= half]["net"].mean()
        if t_rc >= 2 and ps["net"].mean() > 0 and h1 >= -0.001 and h2 >= -0.001:
            good.append(sym)
    print(f"\nNames passing (reactive t >= 2, pessimistic positive in both halves): {good}")
    if not good:
        return
    # overlap and a combined book: each signal gets an equal slice of one pot (max 1 trade per name per day)
    for model in ("reactive", "pessimistic"):
        frames = [res[s][model].assign(sym=s) for s in good]
        allt = pd.concat(frames)
        per_day = allt.groupby("date")["sym"].count()
        print(f"\n[{model}] {len(allt)} trades on {len(per_day)} days ({len(allt) / 5.75:.0f} trades/yr, {len(per_day) / 5.75:.0f} trade days/yr); "
              f"names per signal day: mean {per_day.mean():.2f}, 3+ names on {(per_day >= 3).mean():.0%} of days")
        days = pd.Series(1.0, index=pd.bdate_range("2021-01-04", "2026-10-09"))
        day_ret = allt.groupby("date")["net"].mean()  # the pot split equally across that day's signals
        soxl_ret = res["SOXL"][model].set_index("date")["net"] if "SOXL" in res else pd.Series(dtype=float)
        for name, r in (("SOXL only", soxl_ret), ("all passing names", day_ret)):
            r = r.reindex(days.index).fillna(0.0)
            eq = (1 + r).cumprod()
            yrs = len(r) / 252
            print(f"   {name:18s} pot growth {eq.iloc[-1] ** (1 / yrs) - 1:+6.1%}/yr, Sharpe {r.mean() / r.std() * np.sqrt(252):.2f}, "
                  f"max DD {(eq / eq.cummax() - 1).min():+6.1%}, days in a trade {(r != 0).mean():.0%}")
        same = allt.pivot_table(index="date", columns="sym", values="net")
        c = same.corr(min_periods=10).where(~np.eye(len(same.columns), dtype=bool)).stack()
        print(f"   same-day result correlation between names: median {c.median():+.2f}")


if __name__ == "__main__":
    main()
