"""Edges specific to the extended sessions for SOXL/SOXS.

Day market  = 20:00-04:00 ET (Toss/Blue Ocean, Korean daytime), Toss 1m bars since 2025-11.
Pre-market  = 04:00-09:30 ET, Alpaca SIP 1m bars 2021-2026 (Toss bars for recent checks).

  D1 Korea leads     - Samsung/Hynix move after the KRX open -> SOXL in the (thin) day market
  D2 Korea catch-up  - SOXL lagging the full Korean semis session -> last part of the day market
  D3 Limit reversion - limit buys at stretches below VWAP (no spread paid), exit at VWAP/end
  D4 Day -> pre      - day-market move continuing / reversing in the pre-market
  P1 Gap             - overnight gap into the 04:00 pre-market open, follow / fade to 09:29
  P2 8:30 releases   - SOXL move 08:30-08:45 ET (CPI/jobs) continuing into the open
  P3 Late drift      - pre-market trend 04:00-09:00 extending 09:00-09:29
  P4 Limit reversion - same as D3 in the pre-market
  P5 Live spec        - P4 refined: dip = 0.4 x SOXL 20-day vol below the pre-market VWAP, entries
                        07:00-09:00, take profit at +dip, else out at 09:25; three fill models

Result: P5 is the only robust edge (traded by trader/scalp_pre.py). Nothing in the day market
beat costs, including minute-level Korean-semis lead-lag and stale SOXL/SOXS quotes.

    python -m trader.research.scalp_ext
"""
from __future__ import annotations

from datetime import time, timedelta

import numpy as np
import pandas as pd

from ..config import DATA_DIR

D = DATA_DIR / "research" / "scalp"
ET = "America/New_York"
COMM = 0.001
SLIP = {"day": 0.001, "pre": 0.0005}


def net(gross, session, limit=False):
    slip = 0.0 if limit else SLIP[session]
    return np.asarray(gross) - 2 * COMM - 2 * slip - 0.0000206


def show(name, g, session, limit=False, dates=None):
    g = np.asarray([x for x in g if np.isfinite(x)])
    if len(g) < 15:
        print(f"{name:62s} n={len(g)} (too few)")
        return
    n = net(g, session, limit)
    t = n.mean() / n.std() * np.sqrt(len(n))
    half = ""
    if dates is not None and len(dates) == len(g):
        dts = pd.to_datetime(pd.Series(dates))
        cut = dts.quantile(0.5)
        a, b = n[(dts < cut).to_numpy()], n[(dts >= cut).to_numpy()]
        half = f" | 1st half {a.mean():+.3%} 2nd half {b.mean():+.3%}"
    print(f"{name:62s} n {len(n):4d} gross {g.mean():+.3%} net {n.mean():+.3%} (t {t:+.1f}) win {(n > 0).mean():.0%}{half}")


def toss_bars():
    t = pd.read_parquet(D / "toss_1m.parquet")
    t["et"] = t["ts"].dt.tz_convert(ET)
    return t


def day_sessions(t):
    """Group Toss bars into day-market sessions: 20:00 ET (day d) -> 04:00 ET (d+1)."""
    tt = t[(t["et"].dt.time >= time(20, 0)) | (t["et"].dt.time < time(4, 0))].copy()
    shifted = tt["et"] - pd.Timedelta(hours=4)  # 20:00-04:00 maps onto one calendar day
    tt["sess"] = shifted.dt.tz_localize(None).dt.normalize()
    return tt


def kr_semis():
    k = pd.read_parquet(D / "kr_semis_1m.parquet")
    k["et"] = k["ts"].dt.tz_convert(ET)
    k["kst"] = k["ts"].dt.tz_convert("Asia/Seoul")
    k = k[k["kst"].dt.time.between(time(9, 0), time(15, 30))]
    k["kdate"] = k["kst"].dt.tz_localize(None).dt.normalize()
    piv = k.pivot_table(index="et", columns="symbol", values="close").sort_index().ffill()
    return k, piv


def day_market_edges():
    t = toss_bars()
    dm = day_sessions(t)
    L = dm[dm["symbol"] == "SOXL"].set_index("et").sort_index()
    S = dm[dm["symbol"] == "SOXS"].set_index("et").sort_index()
    k, kp = kr_semis()
    kr_dates = sorted(k["kdate"].unique())
    d1 = {th: ([], []) for th in (0.005, 0.01, 0.02)}
    d1b = {th: ([], []) for th in (0.005, 0.01, 0.02)}
    d2 = {th: ([], []) for th in (0.01, 0.02)}
    d3 = {kk: ([], []) for kk in (0.01, 0.02, 0.03)}
    d4 = {"follow": ([], []), "fade": ([], [])}
    pre_toss = t[(t["et"].dt.time >= time(4, 0)) & (t["et"].dt.time < time(9, 30))]
    preL = pre_toss[pre_toss["symbol"] == "SOXL"].set_index("et").sort_index()
    preS = pre_toss[pre_toss["symbol"] == "SOXS"].set_index("et").sort_index()
    for sess, g in L.groupby(dm.loc[dm["symbol"] == "SOXL"].set_index("et").sort_index()["sess"]):
        if len(g) < 240:
            continue
        s = S.loc[g.index[0] : g.index[-1], "close"].reindex(g.index).ffill()
        c = g["close"]
        start, end = g.index[0], g.index[-1]
        # matching KRX session: the Korean date of the day-market window
        kd = (start.tz_convert("Asia/Seoul") + pd.Timedelta(hours=1)).tz_localize(None).normalize()
        if kd not in kr_dates:
            kr = None
        else:
            kr = kp.loc[(kp.index >= start - pd.Timedelta(hours=1)) & (kp.index <= end)]
        if kr is not None and len(kr) > 60:
            kopen = kr.iloc[0]
            k30 = kr.index[0] + pd.Timedelta(minutes=30)
            entry_t = max(k30, start + pd.Timedelta(minutes=5))
            kret = (kr.loc[:entry_t].iloc[-1] / kopen - 1).mean()
            ci = c.loc[entry_t:]
            si = s.loc[entry_t:]
            kclose_t = kr.index[-1]
            for th in d1:
                if abs(kret) >= th and len(ci) > 10:
                    up = kret > 0
                    px = ci if up else si
                    d1[th][0].append(px.iloc[-1] / px.iloc[0] - 1)
                    d1[th][1].append(sess)
                    pk = px.loc[:kclose_t]
                    if len(pk) > 5:
                        d1b[th][0].append(pk.iloc[-1] / pk.iloc[0] - 1)
                        d1b[th][1].append(sess)
            # D2: after the KRX close, does SOXL catch up to the Korean semis session move?
            kfull = (kr.iloc[-1] / kopen - 1).mean()
            soxl_so_far = c.loc[:kclose_t].iloc[-1] / c.iloc[0] - 1 if len(c.loc[:kclose_t]) else np.nan
            gap = 3 * kfull - soxl_so_far
            after = c.loc[kclose_t:]
            for th in d2:
                if np.isfinite(gap) and abs(gap) >= th and len(after) > 10:
                    px = after if gap > 0 else s.loc[kclose_t:]
                    d2[th][0].append(px.iloc[-1] / px.iloc[0] - 1)
                    d2[th][1].append(sess)
        # D3: limit-order VWAP reversion inside the day market
        vol = g["volume"].fillna(0)
        vwap = ((g["high"] + g["low"] + g["close"]) / 3 * vol).cumsum() / vol.cumsum().replace(0, np.nan)
        lo, hi = g["low"].to_numpy(), g["high"].to_numpy()
        vw = vwap.to_numpy()
        for kk in d3:
            for i in range(30, len(g) - 30):
                if not np.isfinite(vw[i - 1]):
                    continue
                buy_px = vw[i - 1] * (1 - kk)
                sell_px = vw[i - 1] * (1 + kk)
                if lo[i] < buy_px * 0.9995:  # filled only if traded through the limit
                    exit_px = c.iloc[-1]
                    for j in range(i + 1, len(g)):
                        if hi[j] >= vw[j - 1]:
                            exit_px = vw[j - 1]
                            break
                    d3[kk][0].append(exit_px / buy_px - 1)
                    d3[kk][1].append(sess)
                    break
                if hi[i] > sell_px * 1.0005:  # SOXL stretched up: buy SOXS at its own stretch (mirror)
                    s_now = s.iloc[i]
                    exit_s = s.iloc[-1]
                    for j in range(i + 1, len(g)):
                        if lo[j] <= vw[j - 1]:
                            exit_s = s.iloc[j]
                            break
                    d3[kk][0].append(exit_s / s_now - 1)
                    d3[kk][1].append(sess)
                    break
        # D4: day-market move -> pre-market
        r_day = c.iloc[-1] / c.iloc[0] - 1
        pl = preL.loc[end : end + pd.Timedelta(hours=6), "close"]
        ps = preS.loc[end : end + pd.Timedelta(hours=6), "close"]
        if len(pl) > 60 and len(ps) > 60 and abs(r_day) >= 0.01:
            fol = (pl if r_day > 0 else ps)
            fad = (ps if r_day > 0 else pl)
            d4["follow"][0].append(fol.iloc[-1] / fol.iloc[0] - 1)
            d4["follow"][1].append(sess)
            d4["fade"][0].append(fad.iloc[-1] / fad.iloc[0] - 1)
            d4["fade"][1].append(sess)
    print("=== DAY MARKET (since Nov 2025) — market orders at 0.10%/side slippage unless 'limit' ===")
    for th, (g_, d_) in d1.items():
        show(f"D1 KR semis 30m after KRX open >= {th:.1%} -> SOXL/SOXS to session end", g_, "day", dates=d_)
    for th, (g_, d_) in d1b.items():
        show(f"D1b same, exit at the KRX close (15:30 KST)", g_, "day", dates=d_) if th == 0.01 else None
    for th, (g_, d_) in d2.items():
        show(f"D2 SOXL lag vs 3x KR semis >= {th:.0%} -> catch-up after KRX close", g_, "day", dates=d_)
    for kk, (g_, d_) in d3.items():
        show(f"D3 limit buy {kk:.0%} below VWAP, exit at VWAP / end (limit)", g_, "day", limit=True, dates=d_)
    for mode, (g_, d_) in d4.items():
        show(f"D4 day-market move >= 1% -> {mode} in the pre-market (04:00-09:29)", g_, "pre", dates=d_)


def premarket_edges():
    a = pd.read_parquet(D / "alpaca_1m.parquet")
    a["et"] = a["ts"].dt.tz_convert(ET)
    a["date"] = a["et"].dt.tz_localize(None).dt.normalize()
    a["m"] = (a["et"].dt.hour - 4) * 60 + a["et"].dt.minute
    a = a[(a["m"] >= 0) & (a["m"] < 720)]
    mats = {(s, f): a[a["symbol"] == s].pivot_table(index="date", columns="m", values=f, aggfunc="last").reindex(columns=range(720))
            for s in ("SOXL", "SOXS") for f in ("open", "high", "low", "close", "volume")}
    for s in ("SOXL", "SOXS"):
        mats[(s, "close")] = mats[(s, "close")].ffill(axis=1)
    days = mats[("SOXL", "close")].index.intersection(mats[("SOXS", "close")].index)
    C, SC = mats[("SOXL", "close")].loc[days], mats[("SOXS", "close")].loc[days]
    O, H, L, V = (mats[("SOXL", f)].loc[days] for f in ("open", "high", "low", "volume"))
    prev = C[719].shift(1)
    res = {k: ([], []) for k in ("p1_follow_3", "p1_fade_3", "p1_follow_6", "p1_fade_6", "p2_1", "p2_2", "p3_2", "p3_4", "p4_2", "p4_3")}
    for d in days[1:]:
        c, sc, lo, hi, vol = C.loc[d].to_numpy(), SC.loc[d].to_numpy(), L.loc[d].to_numpy(), H.loc[d].to_numpy(), V.loc[d].fillna(0).to_numpy()
        pc = prev.loc[d]
        first = np.flatnonzero(np.isfinite(O.loc[d].to_numpy()[:60]))
        if not np.isfinite(pc) or not len(first):
            continue
        i0 = first[0]
        gap = c[i0] / pc - 1
        e = min(i0 + 5, 329)
        for thr, tag in ((0.03, "3"), (0.06, "6")):
            if abs(gap) >= thr:
                fol = c if gap > 0 else sc
                fad = sc if gap > 0 else c
                res[f"p1_follow_{tag}"][0].append(fol[329] / fol[e] - 1); res[f"p1_follow_{tag}"][1].append(d)
                res[f"p1_fade_{tag}"][0].append(fad[329] / fad[e] - 1); res[f"p1_fade_{tag}"][1].append(d)
        r830 = c[285] / c[269] - 1  # 08:29 -> 08:45
        for thr, tag in ((0.01, "1"), (0.02, "2")):
            if np.isfinite(r830) and abs(r830) >= thr:
                px = c if r830 > 0 else sc
                res[f"p2_{tag}"][0].append(px[329] / px[285] - 1); res[f"p2_{tag}"][1].append(d)
        r = c[300] / c[i0] - 1  # 04:00 -> 09:00
        for thr, tag in ((0.02, "2"), (0.04, "4")):
            if np.isfinite(r) and abs(r) >= thr:
                px = c if r > 0 else sc
                res[f"p3_{tag}"][0].append(px[329] / px[300] - 1); res[f"p3_{tag}"][1].append(d)
        pv = np.cumsum(((hi + lo + c) / 3)[i0:330] * vol[i0:330])
        vv = np.cumsum(vol[i0:330])
        vwap = np.where(vv > 0, pv / np.maximum(vv, 1), np.nan)
        for kk, tag in ((0.02, "2"), (0.03, "3")):
            for j in range(i0 + 30, 300):
                w = vwap[j - 1 - i0]
                if np.isfinite(w) and np.isfinite(lo[j]) and lo[j] < w * (1 - kk) * 0.9995:
                    buy = w * (1 - kk)
                    ex = c[329]
                    for q in range(j + 1, 330):
                        wq = vwap[q - 1 - i0]
                        if np.isfinite(hi[q]) and hi[q] >= wq:
                            ex = wq
                            break
                    res[f"p4_{tag}"][0].append(ex / buy - 1); res[f"p4_{tag}"][1].append(d)
                    break
    print("\n=== PRE-MARKET (2021-2026, Alpaca SIP) — market orders at 0.05%/side unless 'limit' ===")
    labels = {
        "p1_follow_3": "P1 gap >= 3% at 04:00, follow to 09:29", "p1_fade_3": "P1 gap >= 3% at 04:00, fade to 09:29",
        "p1_follow_6": "P1 gap >= 6% at 04:00, follow to 09:29", "p1_fade_6": "P1 gap >= 6% at 04:00, fade to 09:29",
        "p2_1": "P2 08:30-08:45 move >= 1% -> continue to 09:29", "p2_2": "P2 08:30-08:45 move >= 2% -> continue to 09:29",
        "p3_2": "P3 pre-market trend >= 2% by 09:00 -> 09:00-09:29", "p3_4": "P3 pre-market trend >= 4% by 09:00 -> 09:00-09:29",
        "p4_2": "P4 limit buy 2% below pre-market VWAP (limit)", "p4_3": "P4 limit buy 3% below pre-market VWAP (limit)",
    }
    for k, (g_, d_) in res.items():
        show(labels[k], g_, "pre", limit=k.startswith("p4"), dates=d_)


def _dip_trade(c, lo, hi, vol, start, kk, model, e0=180, e1=300, exit_i=325, thru=0.003, rest=10):
    """One pre-market dip trade. model: level (filled at the level on a 0.3% trade-through),
    reactive (bar close if still below, else rest 10 min needing a close below), pessimistic
    (filled only when a bar closes below the level, at the level)."""
    pv = np.cumsum(((hi + lo + c) / 3)[start:exit_i + 1] * vol[start:exit_i + 1])
    vv = np.cumsum(vol[start:exit_i + 1])
    vwap = np.where(vv > 0, pv / np.maximum(vv, 1), np.nan)
    j = max(start + 30, e0)
    while j < e1:
        w = vwap[j - 1 - start]
        if not (np.isfinite(w) and np.isfinite(lo[j])) or lo[j] >= w * (1 - kk) * (1 - thru):
            j += 1
            continue
        level, buy = w * (1 - kk), None
        if model == "level":
            buy = level
        elif model == "pessimistic":
            buy = level if c[j] < level else None
        elif c[j] * 1.0005 <= level:
            buy = c[j] * 1.0005
        else:
            for q in range(j + 1, min(j + 1 + rest, e1)):
                if np.isfinite(c[q]) and c[q] < level:
                    buy, j = level, q
                    break
        if buy is None:
            j += 1
            continue
        ex = c[exit_i]
        for q in range(j + 1, exit_i + 1):
            if np.isfinite(hi[q]) and hi[q] >= buy * (1 + kk) * 1.0005:
                ex = buy * (1 + kk)
                break
        return ex / buy - 1
    return None


def premarket_dip():
    a = pd.read_parquet(D / "alpaca_1m.parquet")
    a = a[a["symbol"] == "SOXL"]
    a["et"] = a["ts"].dt.tz_convert(ET)
    a["date"] = a["et"].dt.tz_localize(None).dt.normalize()
    a["m"] = (a["et"].dt.hour - 4) * 60 + a["et"].dt.minute
    a = a[(a["m"] >= 0) & (a["m"] < 720)]
    M = {f: a.pivot_table(index="date", columns="m", values=f, aggfunc="last").reindex(columns=range(720)) for f in ("high", "low", "close", "volume")}
    M["close"] = M["close"].ffill(axis=1)
    sig = np.log(M["close"][719]).diff().rolling(20).std().shift(1)
    print("\n=== P5 pre-market dip, live spec (Alpaca 2021-2026, limit orders, no slippage) ===")
    for model in ("level", "reactive", "pessimistic"):
        rs, ds = [], []
        for d in M["close"].index:
            s = sig.get(d)
            if not np.isfinite(s):
                continue
            c, lo, hi, v = (M[f].loc[d].to_numpy() for f in ("close", "low", "high", "volume"))
            first = np.flatnonzero(np.isfinite(lo[:60]))
            if len(first):
                r = _dip_trade(c, lo, hi, np.nan_to_num(v), first[0], min(0.06, max(0.015, 0.4 * s)), model)
                if r is not None:
                    rs.append(r)
                    ds.append(d)
        show(f"P5 fill model: {model}", rs, "pre", limit=True, dates=ds)


if __name__ == "__main__":
    day_market_edges()
    premarket_edges()
    premarket_dip()
