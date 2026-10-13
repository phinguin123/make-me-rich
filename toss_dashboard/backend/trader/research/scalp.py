"""SOXL/SOXS intraday edge research (2021-2026, 1-minute, all sessions).

Edges tested (each a long-only trade in SOXL or SOXS, flat by the session end):
  E1 close momentum   - leveraged-ETF rebalancing / hedging flows push late-day moves further
                        (Cheng & Madhavan 2009; Baltussen, Da, Lammers & Martens 2021)
  E2 opening range    - breakout of the first N minutes, SOXL up / SOXS down
  E3 gap              - fade vs follow large opening gaps
  E4 premarket drift  - pre-market momentum into the open
  E5 VWAP reversion   - fade large intraday stretches from VWAP
  E6 day market       - Korean-hours session (Toss data since 2025-11), drift and reversal
Costs: Toss 0.1%/side + SEC fee + slippage (regular 0.03%, pre/after 0.10%, day market 0.25% per side).

    python -m trader.research.scalp
"""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

from ..config import DATA_DIR

D = DATA_DIR / "research" / "scalp"
ET = "America/New_York"
COMM = 0.001
SLIP = {"regular": 0.0003, "ext": 0.0010, "day": 0.0025}
OPEN_M, CLOSE_M = 330, 719  # minute index from 04:00 ET: 09:30 and 15:59
SPLIT = pd.Timestamp("2024-01-01")


def cost(session: str) -> float:
    return 2 * COMM + 2 * SLIP[session] + 0.0000206


def load_matrices():
    d = pd.read_parquet(D / "alpaca_1m.parquet")
    d["et"] = d["ts"].dt.tz_convert(ET)
    d["date"] = d["et"].dt.tz_localize(None).dt.normalize()
    d["m"] = (d["et"].dt.hour - 4) * 60 + d["et"].dt.minute
    d = d[(d["m"] >= 0) & (d["m"] < 960)]
    mats = {}
    for sym in ("SOXL", "SOXS", "SOXX"):
        x = d[d["symbol"] == sym]
        for f in ("open", "high", "low", "close", "volume"):
            p = x.pivot_table(index="date", columns="m", values=f, aggfunc="last").reindex(columns=range(960))
            if f == "close":
                p = p.ffill(axis=1)
            mats[(sym, f)] = p
    days = mats[("SOXL", "close")].index.intersection(mats[("SOXS", "close")].index).intersection(mats[("SOXX", "close")].index)
    mats = {k: v.loc[days] for k, v in mats.items()}
    soxx_close = mats[("SOXX", "close")][CLOSE_M]
    regime = pd.Series(np.where(soxx_close > soxx_close.rolling(50, min_periods=30).mean(), "bull", "bear"), index=days)
    mats["regime"] = regime.shift(1).fillna("bull")  # known before the day starts
    return mats, days


def report(name: str, trades: pd.DataFrame) -> None:
    if trades.empty:
        print(f"{name:58s} no trades")
        return
    t = trades
    yrs = (t["date"].max() - t["date"].min()).days / 365.25 or 1
    isr, oos = t[t["date"] < SPLIT]["net"], t[t["date"] >= SPLIT]["net"]
    bear, bull = t[t["regime"] == "bear"]["net"], t[t["regime"] == "bull"]["net"]
    tstat = t["net"].mean() / t["net"].std() * np.sqrt(len(t)) if t["net"].std() > 0 else 0
    eq = (1 + t["net"]).cumprod()
    print(f"{name:58s} n {len(t):4d} ({len(t) / yrs:4.0f}/yr) net/trade {t['net'].mean():+.3%} (t {tstat:+.1f}) win {(t['net'] > 0).mean():.0%} | "
          f"IS {isr.mean():+.3%} OOS {oos.mean():+.3%} | bear {bear.mean():+.3%} (n{len(bear)}) bull {bull.mean():+.3%} | "
          f"compounded {eq.iloc[-1] - 1:+.0%} MDD {(eq / eq.cummax() - 1).min():.0%}")


def trade(day, sym, entry, exit_, session, regime, **kw):
    gross = exit_ / entry - 1
    return {"date": day, "sym": sym, "gross": gross, "net": gross - cost(session), "regime": regime, **kw}


# ── E1: late-day momentum ───────────────────────────────────────────────────
def e1_close_momentum(m, days, sig_m=690, theta=0.01, exit_m=CLOSE_M, use="day"):
    sx, sl, ss = m[("SOXX", "close")], m[("SOXL", "close")], m[("SOXS", "close")]
    prev = sx[CLOSE_M].shift(1)
    out = []
    for d in days[1:]:
        ref = prev.loc[d] if use == "day" else sx.loc[d, OPEN_M]
        r = sx.loc[d, sig_m] / ref - 1
        if not np.isfinite(r) or abs(r) < theta:
            continue
        sym, px = ("SOXL", sl) if r > 0 else ("SOXS", ss)
        e, x = px.loc[d, sig_m], px.loc[d, exit_m]
        if np.isfinite(e) and np.isfinite(x):
            out.append(trade(d, sym, e, x, "regular", m["regime"].loc[d], signal=r))
    return pd.DataFrame(out)


# ── E2: opening range breakout (SOXL up / SOXS down) ──────────────────────────
def e2_orb(m, days, n=15, stop="opposite", trail=0.0, cutoff_m=OPEN_M + 120):
    out = []
    H, L, C = m[("SOXL", "high")], m[("SOXL", "low")], m[("SOXL", "close")]
    SC = m[("SOXS", "close")]
    for d in days:
        h, l, c, sc = H.loc[d].to_numpy(), L.loc[d].to_numpy(), C.loc[d].to_numpy(), SC.loc[d].to_numpy()
        orh, orl = np.nanmax(h[OPEN_M : OPEN_M + n]), np.nanmin(l[OPEN_M : OPEN_M + n])
        if not (np.isfinite(orh) and np.isfinite(orl)) or orh <= orl:
            continue
        for i in range(OPEN_M + n, cutoff_m):
            if np.isfinite(h[i]) and h[i] > orh:
                side, ei = "SOXL", i
                break
            if np.isfinite(l[i]) and l[i] < orl:
                side, ei = "SOXS", i
                break
        else:
            continue
        if side == "SOXL":
            entry, stop_px, best = max(orh, c[ei - 1]), (orl if stop == "opposite" else orh * (1 - stop)), orh
            for j in range(ei + 1, CLOSE_M + 1):
                if np.isfinite(l[j]) and l[j] <= stop_px:
                    exit_ = stop_px
                    break
                if np.isfinite(h[j]):
                    best = max(best, h[j])
                    if trail:
                        stop_px = max(stop_px, best * (1 - trail))
            else:
                exit_ = c[CLOSE_M]
            out.append(trade(d, "SOXL", entry, exit_, "regular", m["regime"].loc[d]))
        else:
            # SOXS mirrors SOXL: stop when SOXL trades back above the stop level
            entry_s = sc[ei]
            stop_l, worst = (orh if stop == "opposite" else orl * (1 + stop)), orl
            for j in range(ei + 1, CLOSE_M + 1):
                if np.isfinite(h[j]) and h[j] >= stop_l:
                    exit_s = sc[j]
                    break
                if np.isfinite(l[j]):
                    worst = min(worst, l[j])
                    if trail:
                        stop_l = min(stop_l, worst * (1 + trail))
            else:
                exit_s = sc[CLOSE_M]
            if np.isfinite(entry_s) and np.isfinite(exit_s):
                out.append(trade(d, "SOXS", entry_s, exit_s, "regular", m["regime"].loc[d]))
    return pd.DataFrame(out)


# ── E3: opening gap fade / follow ────────────────────────────────────────────
def e3_gap(m, days, g=0.03, mode="fade", hold_m=60):
    C, O = m[("SOXL", "close")], m[("SOXL", "open")]
    SC = m[("SOXS", "close")]
    prev = C[CLOSE_M].shift(1)
    out = []
    for d in days[1:]:
        op = O.loc[d, OPEN_M]
        gap = op / prev.loc[d] - 1 if np.isfinite(op) else np.nan
        if not np.isfinite(gap) or abs(gap) < g:
            continue
        up = gap > 0
        buy_soxl = (not up) if mode == "fade" else up
        px = C if buy_soxl else SC
        e, x = px.loc[d, OPEN_M + 1], px.loc[d, min(OPEN_M + hold_m, CLOSE_M)]
        if np.isfinite(e) and np.isfinite(x):
            out.append(trade(d, "SOXL" if buy_soxl else "SOXS", e, x, "regular", m["regime"].loc[d], gap=gap))
    return pd.DataFrame(out)


# ── E4: premarket momentum into the open ─────────────────────────────────────
def e4_premarket(m, days, sig_m=300, theta=0.02, exit_m=OPEN_M - 1):
    C, SC = m[("SOXL", "close")], m[("SOXS", "close")]
    prev = C[CLOSE_M].shift(1)
    out = []
    for d in days[1:]:
        r = C.loc[d, sig_m] / prev.loc[d] - 1
        if not np.isfinite(r) or abs(r) < theta:
            continue
        px, sym = (C, "SOXL") if r > 0 else (SC, "SOXS")
        e, x = px.loc[d, sig_m], px.loc[d, exit_m]
        if np.isfinite(e) and np.isfinite(x):
            out.append(trade(d, sym, e, x, "ext", m["regime"].loc[d], signal=r))
    return pd.DataFrame(out)


# ── E5: VWAP stretch reversion (regular session) ─────────────────────────────
def e5_vwap(m, days, k=0.03, start_m=OPEN_M + 30, end_m=OPEN_M + 330):
    C, V = m[("SOXL", "close")], m[("SOXL", "volume")].fillna(0)
    SC = m[("SOXS", "close")]
    out = []
    for d in days:
        c, v, sc = C.loc[d].to_numpy(), V.loc[d].to_numpy(), SC.loc[d].to_numpy()
        pv = np.nancumsum(c[OPEN_M:] * v[OPEN_M:])
        vv = np.cumsum(v[OPEN_M:])
        vwap = np.where(vv > 0, pv / np.maximum(vv, 1), np.nan)
        for i in range(start_m, end_m):
            dev = c[i] / vwap[i - OPEN_M] - 1
            if not np.isfinite(dev) or abs(dev) < k:
                continue
            buy_soxl = dev < 0
            px = c if buy_soxl else sc
            e = px[i]
            x = px[CLOSE_M]
            for j in range(i + 1, CLOSE_M + 1):  # exit when SOXL touches VWAP again
                if np.isfinite(c[j]) and (c[j] >= vwap[j - OPEN_M] if buy_soxl else c[j] <= vwap[j - OPEN_M]):
                    x = px[j]
                    break
            if np.isfinite(e) and np.isfinite(x):
                out.append(trade(d, "SOXL" if buy_soxl else "SOXS", e, x, "regular", m["regime"].loc[d], dev=dev))
            break
    return pd.DataFrame(out)


# ── E6: Korean-hours day market (Toss data) ──────────────────────────────────
def e6_day_market(theta=0.01):
    t = pd.read_parquet(D / "toss_1m.parquet")
    t["kst"] = t["ts"].dt.tz_convert("Asia/Seoul")
    t = t[t["kst"].dt.time.between(time(9, 0), time(17, 59))]
    t["date"] = t["kst"].dt.tz_localize(None).dt.normalize()
    soxl = t[t["symbol"] == "SOXL"].sort_values("kst")
    soxs = t[t["symbol"] == "SOXS"].sort_values("kst").set_index("kst")["close"]
    out, stats = [], []
    for d, g in soxl.groupby("date"):
        if len(g) < 120:
            continue
        g = g.set_index("kst")
        first, last = g["open"].iloc[0], g["close"].iloc[-1]
        h1 = g["close"].iloc[min(59, len(g) - 1)]
        stats.append({"date": d, "first_hour": h1 / first - 1, "rest": last / h1 - 1, "vol": g["volume"].sum()})
        r = h1 / first - 1
        if abs(r) >= theta:
            if r > 0:
                e, x, sym = h1, last, "SOXL"
            else:
                s = soxs.loc[g.index[0] : g.index[-1]]
                if len(s) < 60:
                    continue
                e, x, sym = s.iloc[59], s.iloc[-1], "SOXS"
            out.append(trade(d, sym, e, x, "day", "n/a"))
    st = pd.DataFrame(stats)
    if not st.empty:
        print(f"  day market sessions {len(st)}; median SOXL volume/session {st['vol'].median():,.0f} sh; "
              f"corr(first hour, rest) {st[['first_hour', 'rest']].corr().iloc[0, 1]:+.2f}")
    return pd.DataFrame(out)


def main():
    m, days = load_matrices()
    print(f"{len(days)} sessions {days[0].date()} .. {days[-1].date()}; bear days {(m['regime'] == 'bear').mean():.0%}\n")
    print("== E1 late-day momentum (SOXX move since prior close, trade SOXL/SOXS to the close) ==")
    for sig_m, label in ((660, "15:00"), (690, "15:30"), (705, "15:45")):
        for theta in (0.005, 0.01, 0.02, 0.03):
            report(f"E1 signal {label} |SOXX day| >= {theta:.1%}", e1_close_momentum(m, days, sig_m, theta))
    report("E1 signal 15:30, move since OPEN >= 1%", e1_close_momentum(m, days, 690, 0.01, use="open"))
    print("\n== E2 opening range breakout ==")
    for n in (5, 15, 30):
        for stop, trail in (("opposite", 0.0), ("opposite", 0.03), (0.02, 0.0), (0.02, 0.03)):
            report(f"E2 ORB {n}m stop {stop} trail {trail:.0%}", e2_orb(m, days, n, stop, trail))
    print("\n== E3 opening gap ==")
    for g in (0.02, 0.04, 0.06):
        for mode in ("fade", "follow"):
            for hold in (30, 120, CLOSE_M - OPEN_M):
                report(f"E3 gap >= {g:.0%} {mode} hold {hold}m", e3_gap(m, days, g, mode, hold))
    print("\n== E4 premarket momentum (signal at 09:00 ET vs prior close, exit 09:29) ==")
    for theta in (0.02, 0.04, 0.06):
        report(f"E4 premarket |move| >= {theta:.0%}", e4_premarket(m, days, 300, theta))
        report(f"E4 premarket |move| >= {theta:.0%}, hold to 10:00", e4_premarket(m, days, 300, theta, OPEN_M + 30))
    print("\n== E5 VWAP stretch reversion ==")
    for k in (0.02, 0.03, 0.05):
        report(f"E5 fade SOXL stretch >= {k:.0%} from VWAP", e5_vwap(m, days, k))
    print("\n== E6 day market (Korean hours, since 2025-11) ==")
    for theta in (0.005, 0.01, 0.02):
        report(f"E6 day-market first-hour momentum >= {theta:.1%}", e6_day_market(theta))


if __name__ == "__main__":
    main()
