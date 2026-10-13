"""SOXL looks overbought: is it a turn to buy SOXS? (Toss has no US shorting, so a fade = buying the inverse ETF.)

Part 1  intraday, 2021-2026 SIP minutes. Signals on SOXL's own 1/5/15-minute chart, the trade on SOXS's real
        prices (TQQQ -> SQQQ as a check that played no part in choosing):
          rsi_hi     RSI(14) crosses above 70 / 80 / 85 (fade the extreme straight away)
          rsi_roll   RSI was >= 70 / 80 in the last 6 bars and crosses back below it (wait for the turn)
          macd_top   MACD crosses below its signal while above 0
          boll_rej   close back under the upper Bollinger band after closing above it
          stretch    close >= session VWAP + 1 / 2 dips
          climax     volume >= 3 / 5 x normal on an up bar, then the next bar closes down
          combo      RSI >= 70 in the last 6 bars + MACD crosses down + volume >= 1.5 x
        Exits on SOXS: "signal" (SOXL's RSI back under 50 -> sell at the next bar's open; disaster stop
        -2 dips; out by 15:55), "dip" (+1 dip / -1 dip / 15:55), "run" (stop -1 dip, else 15:55).
        Filters: all days, semis downtrend (SOXX < 50-day), semis uptrend; and "SOXL already up >= 1 dip today".
        Plus a no-cost check on the 14 single names: does "overbought" predict a fall at all?
Part 2  multi-day, real SOXL/SOXS daily bars since 2010: SOXL daily RSI(14) > 70 / 80, RSI(2) > 90 / 95 / 98,
        close 15 / 25 / 35% above its 20-day average, 3-day gain > 15 / 25%. Buy SOXS at the next open, hold
        1 / 3 / 5 / 10 days or until SOXL's RSI(2) falls under 50 (max 10). All days / SOX above / below its
        200-day average. 2010-2017 vs 2018-2026.
Costs as research/open_reversal.py (Toss 0.1%/side, slippage 0.05-0.10%).

    python -m trader.research.fade_soxs
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind
from . import open_reversal as orv
from .soxl_soxs import CACHE as DAILY


# ── Part 1: intraday ───────────────────────────────────────────────────────────
def pair_map(d: dict, long: str, inv: str) -> tuple[np.ndarray, np.ndarray]:
    names = list(d["names"])
    lr = np.flatnonzero(d["sym"] == names.index(long))
    ir = np.flatnonzero(d["sym"] == names.index(inv))
    by_date = dict(zip(d["date"][ir], ir))
    pairs = [(a, by_date[d["date"][a]]) for a in lr if d["date"][a] in by_date]
    return np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs])


def fade_signals(b: dict, I: dict, kk: np.ndarray):
    C, O, vw, rv, rsi, macd, sg = b["C"], b["O"], b["vw"], I["rv"], I["rsi"], I["macd"], I["sig"]
    roll = lambda m: pd.DataFrame(m.astype(float)).T.rolling(6, min_periods=1).max().T.to_numpy() > 0  # noqa: E731
    exit_sig = rsi < 50
    for th in (70, 80, 85):
        yield "rsi_hi", f"RSI above {th}", ind.xup(rsi, th), exit_sig
    for th in (70, 80):
        yield "rsi_roll", f"RSI was >= {th}, back below", roll(rsi >= th) & ind.xdown(rsi, th), exit_sig
    yield "macd_top", "MACD crosses down above 0", ind.xdown(macd, sg) & (macd > 0), exit_sig
    prev_above = np.concatenate([np.zeros((C.shape[0], 1), bool), (C > I["up"])[:, :-1]], 1)
    yield "boll_rej", "back under the upper band", prev_above & (C <= I["up"]), exit_sig
    for k in (1, 2):
        yield "stretch", f"VWAP + {k} dip", C >= vw * (1 + k * kk[:, None]), exit_sig
    for r in (3, 5):
        up_climax = (rv >= r) & (C > O)
        prev = np.concatenate([np.zeros((C.shape[0], 1), bool), up_climax[:, :-1]], 1)
        yield "climax", f"volume {r}x up bar, then a down bar", prev & (C < O), exit_sig
    yield "combo", "RSI>=70 recently + MACD down + volume 1.5x", roll(rsi >= 70) & ind.xdown(macd, sg) & (rv >= 1.5), exit_sig


def trade_inverse(d, tf, ent_l, ex_l, lrows, irows, mode, mask_l):
    """Signals computed on the long ETF's rows, executed on the inverse ETF's rows (same day, same minute)."""
    nb = 390 // tf
    ent = np.zeros((len(d["C"]), nb), bool)
    ex = np.zeros((len(d["C"]), nb), bool)
    ent[irows] = ent_l[lrows] & mask_l[lrows][:, None]
    ex[irows] = ex_l[lrows]
    m = np.zeros(len(d["C"]), bool)
    m[irows] = True
    return ind.run_signal(d, tf, ent, ex, mode, m)


def part1(d: dict) -> None:
    print("=== Part 1. Intraday: SOXL overbought on its chart -> buy SOXS (real SOXS prices, net of costs) ===")
    sl, ss = pair_map(d, "SOXL", "SOXS")
    tl, ts = pair_map(d, "TQQQ", "SQQQ")
    rows = []
    for tf in (1, 5, 15):
        b = ind.bars(d, tf)
        I = ind.indicators(d, b)
        day_up = b["C"] >= d["pc"][:, None] * (1 + d["kk"][:, None])  # already up a dip on the day
        for fam, lab, ent, ex in fade_signals(b, I, d["kk"]):
            for mode in ("signal", "dip", "run"):
                for filt in ("all", "semis down", "semis up", "up a dip today"):
                    m = {"all": np.ones(len(d["C"]), bool), "semis down": d["bear"], "semis up": ~d["bear"]}.get(filt, np.ones(len(d["C"]), bool))
                    e = ent & day_up if filt == "up a dip today" else ent
                    t = trade_inverse(d, tf, e, ex, sl, ss, mode, m)
                    q = trade_inverse(d, tf, e, ex, tl, ts, mode, m)
                    if len(t) < 30:
                        continue
                    r = t["ret"].to_numpy()
                    isx = d["is"][t["row"].to_numpy()]
                    rows.append({"tf": tf, "family": fam, "signal": lab, "exit": mode, "filter": filt, "n": len(r), "mean": r.mean(),
                                 "t": r.mean() / r.std() * np.sqrt(len(r)), "win": (r > 0).mean(),
                                 "is_mean": r[isx].mean(), "is_n": int(isx.sum()), "oos_mean": r[~isx].mean(), "oos_n": int((~isx).sum()),
                                 "sqqq": q["ret"].mean() if len(q) >= 20 else np.nan, "sqqq_n": len(q)})
    v = pd.DataFrame(rows)
    v.to_parquet(orv.D / "fade_soxs_variants.parquet")
    print(f"  {len(v)} variants: positive after costs {(v['mean'] > 0).mean():.0%} (2021-23 {(v['is_mean'] > 0).mean():.0%}, 2024-26 {(v['oos_mean'] > 0).mean():.0%}); "
          f"same rule TQQQ -> SQQQ positive {(v['sqqq'] > 0).mean():.0%}")
    print(f"  rank correlation 2021-23 vs 2024-26: {v[['is_mean', 'oos_mean']].rank().corr().iloc[0, 1]:+.2f}")
    print("  By signal family (median over timeframes, exits, filters):")
    for fam, g in v.groupby("family"):
        print(f"    {fam:9s} {len(g):3d} variants  median {g['mean'].median():+.2%}  2021-23 {g['is_mean'].median():+.2%}  2024-26 {g['oos_mean'].median():+.2%}  "
              f"positive {(g['mean'] > 0).mean():3.0%}  trades/yr {g['n'].median() / 5.7:4.0f}  TQQQ->SQQQ {g['sqqq'].median():+.2%}")
    print("  By filter:")
    for f, g in v.groupby("filter"):
        print(f"    {f:15s} median {g['mean'].median():+.2%}, positive {(g['mean'] > 0).mean():.0%}")
    print("  Top 10 by 2021-23 mean (n >= 40 in each half), then 2024-26:")
    ok = v[(v["is_n"] >= 40) & (v["oos_n"] >= 40)]
    for _, s in ok.sort_values("is_mean", ascending=False).head(10).iterrows():
        print(f"    {s['tf']:2d}m {s['family']:9s} {s['signal']:40s} exit {s['exit']:6s} {s['filter']:14s} 2021-23 {s['is_mean']:+.2%} (n {s['is_n']}) "
              f"-> 2024-26 {s['oos_mean']:+.2%} (n {s['oos_n']}) | TQQQ->SQQQ {s['sqqq']:+.2%}")

    print("\n  Does 'overbought' predict a fall at all? 14 single names, 5-minute chart, no costs, the SHORT side's return "
          "from the next minute's open (positive = the price fell):")
    b = ind.bars(d, 5)
    I = ind.indicators(d, b)
    ends = (np.arange(78) + 1) * 5
    okw = (ends >= 15) & (ends <= 330)
    for fam, lab, ent, ex in fade_signals(b, I, d["kk"]):
        if fam not in ("rsi_hi", "rsi_roll", "stretch", "macd_top"):
            continue
        k = orv.first_true(ent & okw[None, :] & d["main"][:, None])
        rows_ = np.flatnonzero(k >= 0)
        e = (k[rows_] + 1) * 5
        e = np.minimum(e, 389)
        p0 = d["O"][rows_, e]
        out = []
        for h in (30, 60):
            p1 = d["C"][rows_, np.minimum(e + h, 385)]
            out.append(f"+{h} min {np.nanmean(1 - p1 / p0):+.3%}")
        p1 = d["C"][rows_, 385]
        out.append(f"to 15:55 {np.nanmean(1 - p1 / p0):+.3%}")
        print(f"    {lab:34s} n {len(rows_):5d}  " + "  ".join(out))


# ── Part 2: multi-day ──────────────────────────────────────────────────────────
def rsi(c: pd.Series, n: int) -> pd.Series:
    dlt = c.diff()
    up = dlt.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-dlt.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def part2() -> None:
    print("\n=== Part 2. Multi-day: SOXL overbought on the DAILY chart -> buy SOXS at the next open (real ETFs since 2010) ===")
    raw = pd.read_parquet(DAILY)
    px = {s: g.set_index("date").sort_index() for s, g in raw.groupby("symbol")}
    L, S, X = px["SOXL"], px["SOXS"], px["^SOX"]
    idx = L.index.intersection(S.index).intersection(X.index)
    L, S, X = L.loc[idx], S.loc[idx], X.loc[idx]
    c = L["close"]
    feats = {"rsi14": rsi(c, 14), "rsi2": rsi(c, 2), "ext20": c / c.rolling(20).mean() - 1, "r3": c.pct_change(3)}
    bull = (X["close"] > X["close"].rolling(200).mean())
    sigs = {f"RSI14 > {t}": feats["rsi14"] > t for t in (70, 80)}
    sigs |= {f"RSI2 > {t}": feats["rsi2"] > t for t in (90, 95, 98)}
    sigs |= {f"{int(t * 100)}% above 20-day avg": feats["ext20"] > t for t in (0.15, 0.25, 0.35)}
    sigs |= {f"3-day gain > {int(t * 100)}%": feats["r3"] > t for t in (0.15, 0.25)}
    so, sc = S["open"].to_numpy(), S["close"].to_numpy()
    r2 = feats["rsi2"].to_numpy()
    rows = []
    for name, sig in sigs.items():
        s = sig.to_numpy() & np.isfinite(feats["rsi14"].to_numpy())
        for hold in (1, 3, 5, 10, "rsi2<50"):
            for filt, fm in (("all", np.ones(len(idx), bool)), ("SOX above 200d", bull.to_numpy()), ("SOX below 200d", ~bull.to_numpy())):
                trades, i = [], 0
                while i < len(idx) - 2:
                    if s[i] and fm[i]:
                        e = i + 1  # buy SOXS at the next open
                        if hold == "rsi2<50":
                            x = e
                            while x < min(len(idx) - 1, e + 9) and not (r2[x] < 50):
                                x += 1
                        else:
                            x = min(len(idx) - 1, e + hold - 1)
                        ret = sc[x] * 0.9995 / (so[e] * 1.0005) - 1 - orv.COST
                        trades.append((idx[e], ret))
                        i = x + 1
                    else:
                        i += 1
                if len(trades) < 10:
                    continue
                t = pd.DataFrame(trades, columns=["date", "ret"])
                a, b_ = t[t["date"] < "2018-01-01"]["ret"], t[t["date"] >= "2018-01-01"]["ret"]
                rows.append({"signal": name, "hold": str(hold), "filter": filt, "n": len(t), "mean": t["ret"].mean(), "win": (t["ret"] > 0).mean(),
                             "median": t["ret"].median(), "worst": t["ret"].min(), "h1": a.mean(), "n1": len(a), "h2": b_.mean(), "n2": len(b_)})
    v = pd.DataFrame(rows)
    print(f"  {len(v)} variants: positive {(v['mean'] > 0).mean():.0%}; positive in both 2010-17 and 2018-26 {((v['h1'] > 0) & (v['h2'] > 0)).mean():.0%}; "
          f"median per trade {v['mean'].median():+.2%}, median win rate {v['win'].median():.0%}")
    print("  By signal (median over holds and filters):")
    for name, g in v.groupby("signal", sort=False):
        print(f"    {name:24s} median {g['mean'].median():+.2%}  win {g['win'].median():.0%}  2010-17 {g['h1'].median():+.2%}  2018-26 {g['h2'].median():+.2%}  "
              f"worst trade {g['worst'].min():+.0%}  signals {int(g['n'].median())}")
    print("  By filter:")
    for f, g in v.groupby("filter"):
        print(f"    {f:15s} median {g['mean'].median():+.2%}, positive {(g['mean'] > 0).mean():.0%}")
    print("  Best 8 by 2010-17 (n >= 10 each half), then 2018-26:")
    ok = v[(v["n1"] >= 10) & (v["n2"] >= 10)]
    for _, s in ok.sort_values("h1", ascending=False).head(8).iterrows():
        print(f"    {s['signal']:24s} hold {s['hold']:7s} {s['filter']:15s} 2010-17 {s['h1']:+.2%} (n {s['n1']}) -> 2018-26 {s['h2']:+.2%} (n {s['n2']}) | win {s['win']:.0%} worst {s['worst']:+.0%}")


def main() -> None:
    part1(orv.prepare(orv.load()))
    part2()


if __name__ == "__main__":
    main()
