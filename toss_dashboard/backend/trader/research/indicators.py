"""Do RSI, MACD, volume and the other chart indicators give an intraday edge after Toss costs?

Prompted by SOXL on 2026-10-13 (117.8 -> 126 after 12:00 ET). Long-only signals on 1-, 5- and 15-minute
regular-session bars of the book's 14 names (selection) plus SOXL alone and TQQQ/SPXL/TNA/SQQQ (an
out-of-universe check). Indicators run on the continuous chart (one series across days, like a chart with
extended hours off).

  rsi_rev     RSI(14) crosses back above 20 / 25 / 30 (oversold bounce)       exit: RSI >= 70
  rsi_mom     RSI crosses above 50 / 60 / 70 (momentum)                       exit: RSI < 45
  macd        MACD(12,26,9) crosses above its signal: any / below 0 / above 0 exit: crosses back below
  macd_zero   MACD crosses above 0                                            exit: MACD below its signal
  vol_surge   volume >= 2 / 3 / 5 x normal for that time of day, an up bar above the session VWAP
                                                                              exit: close below VWAP
  combo_mom   MACD cross up + RSI > 50 + volume >= 1.5 / 2 / 3 x + above VWAP ("everything lines up")
                                                                              exit: MACD crosses down
  combo_rev   RSI under 30 in the last 6 bars + MACD cross up + volume >= 1.5 / 2 / 3 x
                                                                              exit: RSI >= 70 or MACD down
  ema         EMA 9 crosses above EMA 21 (with / without volume >= 1.5 x)     exit: crosses back below
  boll_rev    close back above the lower Bollinger band (20, 2)               exit: close >= middle band
  boll_break  close above the upper band (with / without volume >= 2 x)       exit: close < middle band
Exits: "signal" (the indicator's own exit at the next bar's open, a disaster stop at -2 dips, out by 15:55),
"dip" (take profit +1 dip, stop -1 dip, 15:55), "run" (stop -1 dip, else hold to 15:55).
dip = 0.4 x 20-day daily volatility (1.5%-6%), the book's unit. First signal per name per day, 09:45-15:00 ET.
Fills and costs as research/open_reversal.py: next bar's open + 0.10% slippage before 09:45 else 0.05%,
Toss 0.1%/side, 0.05% on market exits. Chosen on 2021-23, judged on 2024-26.

    python -m trader.research.indicators
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import open_reversal as orv
from .scalp_ext import D

TFS = (1, 5, 15)
WIN = (15, 330)  # signal bars must END between 09:45 and 15:00 (minutes after 09:30)


def bars(d: dict, tf: int) -> dict:
    R = len(d["C"])
    nb = 390 // tf
    sh = (R, nb, tf)
    Hr = d["Hr"].reshape(sh).max(2)
    Lr = d["Lr"].reshape(sh).min(2)
    return {"O": d["O"][:, ::tf], "H": np.where(np.isfinite(Hr), Hr, np.nan), "L": np.where(np.isfinite(Lr), Lr, np.nan),
            "C": d["C"][:, tf - 1::tf], "V": d["V"].reshape(sh).sum(2), "vw": d["svw"][:, tf - 1::tf], "nb": nb, "tf": tf}


def chained(d: dict, X: np.ndarray, fn) -> np.ndarray:
    """Apply fn to each symbol's continuous series (its days in order, flattened) and reshape back."""
    out = np.full(X.shape, np.nan)
    for s in np.unique(d["sym"]):
        rows = np.flatnonzero(d["sym"] == s)  # contiguous, dates ascending (see open_reversal.load)
        out[rows] = np.asarray(fn(pd.Series(X[rows].ravel()))).reshape(len(rows), -1)
    return out


def indicators(d: dict, b: dict) -> dict:
    C = b["C"]
    def rsi(s):
        dlt = s.diff()
        up = dlt.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        dn = (-dlt.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        return 100 - 100 / (1 + up / dn.replace(0, np.nan))
    ema = lambda n: chained(d, C, lambda s: s.ewm(span=n, adjust=False).mean())  # noqa: E731
    macd = ema(12) - ema(26)
    sig = chained(d, macd, lambda s: s.ewm(span=9, adjust=False).mean())
    mid = chained(d, C, lambda s: s.rolling(20).mean())
    sd = chained(d, C, lambda s: s.rolling(20).std())
    rv = np.full(C.shape, np.nan)  # volume vs the same bar of the day over the prior 20 days
    for s in np.unique(d["sym"]):
        rows = np.flatnonzero(d["sym"] == s)
        avg = pd.DataFrame(b["V"][rows]).rolling(20, min_periods=10).mean().shift(1).to_numpy()
        rv[rows] = b["V"][rows] / np.where(avg > 0, avg, np.nan)
    return {"rsi": chained(d, C, rsi), "macd": macd, "sig": sig, "e9": ema(9), "e21": ema(21),
            "up": mid + 2 * sd, "lo": mid - 2 * sd, "mid": mid, "rv": rv}


def xup(a, b_):
    prev_a = np.concatenate([np.full((a.shape[0], 1), np.nan), a[:, :-1]], 1)
    prev_b = b_ if np.isscalar(b_) else np.concatenate([np.full((a.shape[0], 1), np.nan), b_[:, :-1]], 1)
    return (prev_a <= prev_b) & (a > b_)


def xdown(a, b_):
    return xup(-a, -b_)


def signals(b: dict, I: dict):
    C, O, vw, rv = b["C"], b["O"], b["vw"], I["rv"]
    up_bar = C > O
    rsi, macd, sg = I["rsi"], I["macd"], I["sig"]
    was_os = pd.DataFrame((rsi < 30).astype(float)).T.rolling(6, min_periods=1).max().T.to_numpy() > 0
    macd_up, macd_dn = xup(macd, sg), xdown(macd, sg)
    for th in (20, 25, 30):
        yield "rsi_rev", f"RSI back above {th}", xup(rsi, th), rsi >= 70
    for th in (50, 60, 70):
        yield "rsi_mom", f"RSI above {th}", xup(rsi, th), rsi < 45
    yield "macd", "cross up, any", macd_up, macd_dn
    yield "macd", "cross up below 0", macd_up & (macd < 0), macd_dn
    yield "macd", "cross up above 0", macd_up & (macd > 0), macd_dn
    yield "macd_zero", "MACD above 0", xup(macd, 0.0), macd < sg
    for r in (2, 3, 5):
        yield "vol_surge", f"volume {r}x, up bar above VWAP", (rv >= r) & up_bar & (C > vw), C < vw
    for r in (1.5, 2, 3):
        yield "combo_mom", f"MACD up + RSI>50 + volume {r}x", macd_up & (rsi > 50) & (rv >= r) & (C > vw), macd_dn
        yield "combo_rev", f"RSI<30 recently + MACD up + volume {r}x", was_os & macd_up & (rv >= r), (rsi >= 70) | macd_dn
    yield "ema", "EMA 9 over 21", xup(I["e9"], I["e21"]), xdown(I["e9"], I["e21"])
    yield "ema", "EMA 9 over 21 + volume 1.5x", xup(I["e9"], I["e21"]) & (rv >= 1.5), xdown(I["e9"], I["e21"])
    lo_prev = np.concatenate([np.full((C.shape[0], 1), np.nan), (C - I["lo"])[:, :-1]], 1)
    yield "boll_rev", "back above the lower band", (lo_prev < 0) & (C >= I["lo"]), C >= I["mid"]
    yield "boll_break", "close above the upper band", xup(C, I["up"]), C < I["mid"]
    yield "boll_break", "above the upper band + volume 2x", xup(C, I["up"]) & (rv >= 2), C < I["mid"]


def run_signal(d: dict, tf: int, entry: np.ndarray, exit_sig: np.ndarray, mode: str, mask: np.ndarray) -> pd.DataFrame:
    nb = 390 // tf
    ends = (np.arange(nb) + 1) * tf  # bar end, minutes after 09:30
    ok = (ends >= WIN[0]) & (ends <= WIN[1])
    k = orv.first_true(entry & ok[None, :] & mask[:, None])
    j = np.where(k >= 0, (k + 1) * tf - 1, -1)  # the bar's last minute: simulate buys at the next minute's open
    C1 = d["C"]
    if mode == "dip":
        return orv.simulate(d, j, orv.at(C1, j) * (1 - d["kk"]), orv.at(C1, j) * (1 + d["kk"]), 385)
    if mode == "run":
        return orv.simulate(d, j, orv.at(C1, j) * (1 - d["kk"]), np.full(len(j), np.inf), 385)
    # "signal": first exit-signal bar after entry -> sell at the next bar's open, disaster stop -2 dips, 15:55 cap
    after = exit_sig & (np.arange(nb)[None, :] > k[:, None])
    x = orv.first_true(after)
    xmin = np.where(x >= 0, np.minimum((x + 1) * tf, 385), 385)
    t = orv.simulate(d, j, orv.at(C1, j) * (1 - 2 * d["kk"]), np.full(len(j), np.inf), 385)
    if not len(t):
        return t
    rows = t["row"].to_numpy()
    e = t["e"].to_numpy()
    xm = xmin[rows]
    slip = np.where(e < 15, 0.001, 0.0005)
    buy = d["O"][rows, e] * (1 + slip)
    # the disaster stop counts only if it came before the indicator exit
    cols = np.arange(390)
    stop = orv.at(C1, j)[rows] * (1 - 2 * d["kk"][rows])
    hs = (cols[None, :] >= e[:, None]) & (cols[None, :] < xm[:, None]) & (d["Lr"][rows] <= stop[:, None])
    stopped = hs.any(1)
    exit_px = np.where(xm >= 385, d["C"][rows, 385], d["O"][rows, np.minimum(xm, 389)]) * (1 - 0.0005)
    ret = np.where(stopped, t["ret"].to_numpy(), exit_px / buy - 1 - orv.COST)
    return pd.DataFrame({"row": rows, "ret": ret, "e": e})


def main() -> None:
    d = orv.prepare(orv.load())
    names = list(d["names"])
    soxl = d["sym"] == names.index("SOXL")
    extra = np.isin(d["sym"], [names.index(x) for x in orv.EXTRA])
    rows = []
    for tf in TFS:
        b = bars(d, tf)
        I = indicators(d, b)
        for fam, lab, ent, ex in signals(b, I):
            for mode in ("signal", "dip", "run"):
                for filt in ("all", "qqq"):
                    fm = d["qqq"] if filt == "qqq" else np.ones(len(d["qqq"]), bool)
                    t = run_signal(d, tf, ent, ex, mode, d["main"] & fm)
                    if len(t) < 60:
                        continue
                    r = t["ret"].to_numpy()
                    isx = d["is"][t["row"].to_numpy()]
                    ts = run_signal(d, tf, ent, ex, mode, soxl & fm)
                    te = run_signal(d, tf, ent, ex, mode, extra & fm)
                    tt = lambda x: x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() > 0 else 0.0  # noqa: E731
                    rows.append({"tf": tf, "family": fam, "signal": lab, "exit": mode, "filter": filt, "n": len(r), "mean": r.mean(), "t": tt(r),
                                 "win": (r > 0).mean(), "is_n": int(isx.sum()), "is_mean": r[isx].mean(), "is_t": tt(r[isx]),
                                 "oos_n": int((~isx).sum()), "oos_mean": r[~isx].mean(), "oos_t": tt(r[~isx]),
                                 "soxl_n": len(ts), "soxl": ts["ret"].mean() if len(ts) else np.nan,
                                 "etf_n": len(te), "etf": te["ret"].mean() if len(te) else np.nan,
                                 "gross": r.mean() + orv.COST + 0.001})
        print(f"  {tf}-minute bars done", flush=True)
    v = pd.DataFrame(rows)
    v.to_parquet(D / "indicator_variants.parquet")
    print(f"\n{len(v)} indicator strategies on the 14 names ({v['family'].nunique()} families x 3 chart timeframes x 3 exits x 2 filters)")
    print(f"  positive after costs: all years {(v['mean'] > 0).mean():.0%}, 2021-23 {(v['is_mean'] > 0).mean():.0%}, 2024-26 {(v['oos_mean'] > 0).mean():.0%}")
    print(f"  before costs (adding back ~0.3% fees + slippage): median {v['gross'].median():+.2%}, positive {(v['gross'] > 0).mean():.0%}")
    print(f"  does 2021-23 rank predict 2024-26? rank correlation {v[['is_mean', 'oos_mean']].rank().corr().iloc[0, 1]:+.2f}")
    print("\nBy family (all timeframes / exits): median net per trade, share of variants positive")
    for fam, g in v.groupby("family"):
        print(f"  {fam:10s} {len(g):3d} variants  median {g['mean'].median():+.2%}  2021-23 {g['is_mean'].median():+.2%}  2024-26 {g['oos_mean'].median():+.2%}  "
              f"positive {(g['mean'] > 0).mean():3.0%}  trades/yr {g['n'].median() / 5.7:5.0f}  SOXL alone {g['soxl'].median():+.2%}")
    print("\nBy chart timeframe:")
    for tf, g in v.groupby("tf"):
        print(f"  {tf:2d}-minute  median {g['mean'].median():+.2%}, positive {(g['mean'] > 0).mean():.0%}")
    print("\nTop 12 by 2021-23 t-stat, then their 2024-26 result:")
    for _, s in v.sort_values("is_t", ascending=False).head(12).iterrows():
        print(f"  {s['tf']:2d}m {s['family']:10s} {s['signal']:38s} exit {s['exit']:6s} {s['filter']:3s} | 2021-23 {s['is_mean']:+.2%} (t {s['is_t']:+.1f}, n {s['is_n']}) "
              f"-> 2024-26 {s['oos_mean']:+.2%} (t {s['oos_t']:+.1f}, n {s['oos_n']}) | SOXL {s['soxl']:+.2%} (n {s['soxl_n']}) | ETFs {s['etf']:+.2%}")
    print("\nSOXL alone, best 8 variants over all years (hindsight):")
    for _, s in v[v["soxl_n"] >= 40].sort_values("soxl", ascending=False).head(8).iterrows():
        print(f"  {s['tf']:2d}m {s['family']:10s} {s['signal']:38s} exit {s['exit']:6s} {s['filter']:3s} SOXL {s['soxl']:+.2%} (n {s['soxl_n']}) | same rule on 14 names {s['mean']:+.2%}")


if __name__ == "__main__":
    main()
