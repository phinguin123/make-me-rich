"""Can the bot catch moves like SOXL on 2026-10-13? It opened at 118.40, flushed to 115.84 at 09:32 ET,
then rose to 123.90 by 10:15.

  Part 0  hindsight: how often a 6% low-then-high swing exists, and what follows an opening flush
  Part 1  rules that buy around the open (the 14 book names, 2021 - 2026-10, 1-minute SIP):
    R1  the book's dip rule after the open: 0.4 sigma below the pre-market VWAP (frozen at 09:29),
        entries 09:30-10:00 ET
    R2  flush below the open: the same distance below the 09:30 opening price, entries 09:30-10:00
    R3  the live book (entries 04:30-09:00) but held through the open instead of sold at 09:25
  Exits: take profit at +dip (resting limit), else at the time exit; optional stop at -dip or -2 x dip.
  Fills: level (limit at the level, 0.3% trade-through), reactive (bar close +0.05% if still below,
  else rest 10 min for a close below), pessimistic (only if a bar closes below the level).
  Costs: Toss 0.1%/side + SEC fee; 0.05% slippage on stop and time exits (market orders).

    python -m trader.research.open_flush
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from .pre_multi_data import OUT as MULTI
from .pre_slots import NAMES, qqq_up
from .scalp_ext import D, ET

OPEN = 330  # minute index of 09:30 ET (minutes from 04:00)
COST = 0.002 + 0.0000206
SLIP = 0.0005
EXITS = {"R1": (360, 390, 450, 715), "R2": (360, 390, 450, 715), "R3": (325, 345, 360, 390, 450)}
LABEL = {325: "09:25", 345: "09:45", 360: "10:00", 390: "10:30", 450: "11:30", 715: "15:55"}
STOPS = (None, 1.0, 2.0)  # x dip
MODELS = ("level", "reactive", "pessimistic")


def load(sym: str) -> dict:
    src = D / "alpaca_1m.parquet" if sym in ("SOXL", "SOXS", "NVDA", "MU") else MULTI
    a = pd.read_parquet(src, columns=["symbol", "ts", "open", "high", "low", "close", "volume"], filters=[("symbol", "==", sym)])
    a["et"] = a["ts"].dt.tz_convert(ET)
    a["date"] = a["et"].dt.tz_localize(None).dt.normalize()
    a["m"] = (a["et"].dt.hour - 4) * 60 + a["et"].dt.minute
    a = a[(a["m"] >= 0) & (a["m"] < 720)]
    M = {f: a.pivot_table(index="date", columns="m", values=f, aggfunc="last").reindex(columns=range(720)).to_numpy()
         for f in ("open", "high", "low", "close", "volume")}
    dates = np.array(sorted(a["date"].unique()))
    C = pd.DataFrame(M["close"]).ffill(axis=1).to_numpy()
    sig = pd.Series(np.log(C[:, 719])).diff().rolling(20).std().shift(1).to_numpy()
    return {"dates": dates, "sig": sig, **M}


def entry(lo, c, ref, kk, e0, e1, model, thru=0.003, rest=10):
    j = e0
    while j < e1:
        r = ref[j]
        if not (np.isfinite(r) and np.isfinite(lo[j])) or lo[j] >= r * (1 - kk) * (1 - thru):
            j += 1
            continue
        level = r * (1 - kk)
        if model == "level":
            return j, level
        if model == "pessimistic":
            if c[j] < level:
                return j, level
        elif c[j] * 1.0005 <= level:
            return j, c[j] * 1.0005
        else:
            for q in range(j + 1, min(j + 1 + rest, e1)):
                if np.isfinite(c[q]) and c[q] < level:
                    return q, level
        j += 1
    return None


def outcome(o, hi, lo, cf, j, buy, kk, exit_i, stop):
    tgt = buy * (1 + kk) * 1.0005
    h, l = hi[j + 1:exit_i + 1], lo[j + 1:exit_i + 1]
    t_hit = np.flatnonzero(h >= tgt)
    s_hit = np.flatnonzero(l <= buy * (1 - stop * kk)) if stop else np.array([], int)
    t = t_hit[0] if len(t_hit) else 10 ** 6
    s = s_hit[0] if len(s_hit) else 10 ** 6
    if s <= t and s < 10 ** 6:  # a stop and a target in the same minute count as the stop
        px = buy * (1 - stop * kk)
        op = o[j + 1 + s]
        if np.isfinite(op) and op < px:
            px = op  # gapped through the stop
        return px * (1 - SLIP) / buy - 1, "stop"
    if t < 10 ** 6:
        return kk, "target"
    return cf[exit_i] * (1 - SLIP) / buy - 1, "time"


def run_name(sym: str) -> pd.DataFrame:
    M = load(sym)
    rows = []
    for i, d in enumerate(M["dates"]):
        s = M["sig"][i]
        if not np.isfinite(s):
            continue
        o, hi, lo, c, v = (M[f][i] for f in ("open", "high", "low", "close", "volume"))
        cf = pd.Series(c).ffill().to_numpy()
        first = np.flatnonzero(np.isfinite(lo[:60]))
        if not len(first) or not np.isfinite(o[OPEN]) or not np.isfinite(cf[715]):
            continue
        st = first[0]
        kk = min(0.06, max(0.015, 0.4 * s))
        typ = (hi + lo + c) / 3
        pv = np.where(np.isfinite(typ) & np.isfinite(v), typ * v, 0.0)
        vv = np.nan_to_num(v)
        run = np.full(720, np.nan)  # running VWAP known at minute j (through j-1), from the first bar
        cpv, cvv = np.cumsum(pv[st:]), np.cumsum(vv[st:])
        w = np.where(cvv > 0, cpv / np.maximum(cvv, 1), np.nan)
        run[st + 1:] = w[:-1]
        run[:st + 30] = np.nan  # needs 30 minutes of pre-market first, as live
        pre = np.full(720, np.nan)
        pre[OPEN:] = run[OPEN]  # pre-market VWAP frozen at 09:29
        op = np.full(720, np.nan)
        op[OPEN:] = o[OPEN]
        # hindsight: best low-then-high rise inside the first hour and the whole regular session
        rl = np.fmin.accumulate(np.where(np.isfinite(lo[OPEN:]), lo[OPEN:], np.inf))
        rise = np.where(np.isfinite(hi[OPEN:]), hi[OPEN:] / rl - 1, 0)
        rows.append({"sym": sym, "date": d, "kind": "hindsight", "rise60": np.nanmax(rise[:60]), "rise_day": np.nanmax(rise[:386]),
                     "open_drop15": np.nanmin(lo[OPEN:OPEN + 15]) / o[OPEN] - 1, "kk": kk,
                     "pre_vwap": run[OPEN], "open": o[OPEN], "low15": np.nanmin(lo[OPEN:OPEN + 15]),
                     "after15_low": np.nanmin(lo[OPEN + 15:OPEN + 120]), "after15_high": np.nanmax(hi[OPEN + 15:OPEN + 120]),
                     "c1030": cf[390], "c1555": cf[715]})
        for rule, ref, e0, e1 in (("R1", pre, OPEN, 360), ("R2", op, OPEN, 360), ("R3", run, 30, 300)):
            for model in MODELS:
                e = entry(lo, c, ref, kk, e0, e1, model)
                if e is None:
                    continue
                j, buy = e
                for ex in EXITS[rule]:
                    if j >= ex:
                        continue
                    for stop in STOPS:
                        g, why = outcome(o, hi, lo, cf, j, buy, kk, ex, stop)
                        rows.append({"sym": sym, "date": d, "kind": rule, "model": model, "exit": ex, "stop": stop or 0.0,
                                     "net": g - COST, "why": why, "entry_min": j, "kk": kk})
    return pd.DataFrame(rows)


def stats(x: pd.Series, dates: pd.Series) -> str:
    n = len(x)
    if n < 15:
        return f"n {n} (too few)"
    t = x.mean() / x.std() * np.sqrt(n)
    cut = dates.quantile(0.5)
    h1, h2 = x[dates < cut].mean(), x[dates >= cut].mean()
    worst = x.nsmallest(max(1, n // 20)).mean()
    return f"n {n:5d} ({n / 5.75:4.0f}/yr) net {x.mean():+6.2%} t {t:+5.1f} win {(x > 0).mean():3.0%} halves {h1:+6.2%} / {h2:+6.2%} worst-5% avg {worst:+6.1%}"


def main() -> None:
    with ProcessPoolExecutor(14) as ex:
        df = pd.concat(ex.map(run_name, NAMES))
    up = qqq_up()
    hs = df[df["kind"] == "hindsight"].copy()
    tr = df[df["kind"] != "hindsight"].copy()
    tr["qqq_up"] = tr["date"].map(up).fillna(False).astype(bool)

    print("=== Part 0. Hindsight ===")
    for sym in ("SOXL", "NVDA", "TSLA"):
        h = hs[hs["sym"] == sym]
        print(f"  {sym}: a low-then-high rise of 6%+ existed in the first hour on {(h['rise60'] >= 0.06).mean():.0%} of days, "
              f"during the day on {(h['rise_day'] >= 0.06).mean():.0%} (median best rise: first hour {h['rise60'].median():.1%}, day {h['rise_day'].median():.1%})")
    h = hs[hs["sym"] == "SOXL"].copy()
    flush = h[h["low15"] <= h["pre_vwap"] * (1 - h["kk"])]
    print(f"  SOXL fell 0.4 sigma below its pre-market VWAP in the first 15 minutes on {len(flush)} of {len(h)} days. From that low, by 11:30:")
    up_ = flush["after15_high"] / flush["low15"] - 1
    dn_ = flush["after15_low"] / flush["low15"] - 1
    print(f"    rose another {up_.median():+.1%} (median); fell further {dn_.median():+.1%} (median), 10%+ further on {(dn_ <= -0.10).mean():.0%} "
          f"of them, 5%+ on {(dn_ <= -0.05).mean():.0%}; closed above the low on {(flush['c1555'] > flush['low15']).mean():.0%}")
    print("    (you only know it was 'the low' afterwards: the rise and the further fall are both measured from it)")

    print("\n=== Part 1. Rules (all 14 names pooled; SOXL alone below each) ===")
    names = {"R1": "R1 book rule after the open (pre-mkt VWAP - dip, 09:30-10:00)",
             "R2": "R2 flush below the open (open - dip, 09:30-10:00)",
             "R3": "R3 live book entries 04:30-09:00, held past 09:25"}
    for rule in ("R1", "R2", "R3"):
        print(f"\n{names[rule]}")
        for model in MODELS:
            for ex_ in EXITS[rule]:
                for stop in (0.0, 1.0, 2.0):
                    x = tr[(tr["kind"] == rule) & (tr["model"] == model) & (tr["exit"] == ex_) & (tr["stop"] == stop)]
                    sx = x[x["sym"] == "SOXL"]
                    tag = f"  {model:11s} exit {LABEL[ex_]} stop {'none' if not stop else f'-{stop:g}x dip':9s}"
                    print(f"{tag} all: {stats(x['net'], x['date'])}")
                    if model == "reactive" and stop == 0.0:
                        print(f"{'':42s} SOXL: {stats(sx['net'], sx['date'])}")
    print("\nBy QQQ regime (reactive fills, no stop, all names):")
    for rule in ("R1", "R2", "R3"):
        for ex_ in EXITS[rule]:
            x = tr[(tr["kind"] == rule) & (tr["model"] == "reactive") & (tr["exit"] == ex_) & (tr["stop"] == 0.0)]
            for flag in (True, False):
                y = x[x["qqq_up"] == flag]
                print(f"  {rule} exit {LABEL[ex_]} QQQ {'above' if flag else 'below'} 200d: {stats(y['net'], y['date'])}")
    print("\nPer name (R1, reactive, exit 10:30, no stop):")
    x = tr[(tr["kind"] == "R1") & (tr["model"] == "reactive") & (tr["exit"] == 390) & (tr["stop"] == 0.0)]
    for sym, y in x.groupby("sym"):
        print(f"  {sym:5s} {stats(y['net'], y['date'])}")
    tr.to_parquet(D / "open_flush_trades.parquet")


if __name__ == "__main__":
    main()
