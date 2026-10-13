"""Can the bot catch post-open reversals like SOXL on 2026-10-13 (open 118.40, low 115.84 at 09:32,
123.90 by 10:15) by WAITING for confirmation instead of buying the flush? A broad, honest search.

Universe: the book's 14 names (selection) + TQQQ, SPXL, TNA, SQQQ (never used for selection: an
out-of-universe check). SIP 1-minute bars 2021-01 .. 2026-10-12.

Entry families (a signal is judged on a bar's close; the buy fills at the NEXT minute's open + slippage;
one trade per name per day; signals 09:30-10:29 ET):
  bounce    after a flush (running low <= ref - f x dip), buy once the price closes b x dip above the low
  retrace   after a flush, buy once the price has won back a fraction of the drop (ref - low)
  reclaim   after a flush, buy when the price closes back above the open / the session VWAP
  orr       opening-range reversal: the first N minutes flush down, then a close above that range's high
  delay     no confirmation, just wait: at 09:35 / 09:40 / 09:45 / 10:00 buy if still f x dip below ref
  drive     the other direction of patience: after a strong opening rally, buy the first pullback to the
            session VWAP while still above the open
  ref = pre-market VWAP at 09:29, the 09:30 open, or the prior close. dip = 0.4 x 20-day daily vol (1.5-6%),
  the book's unit.
Exits: stop just below the running low at entry (0.2% under it) unless stated; target = ref, +dip, 2R or
none; time exit 10:30 / 11:30 / 15:55.
Costs: Toss 0.1%/side + SEC fee; slippage 0.10% on entries before 09:45, else 0.05%; 0.05% on stop and time
exits (market orders); targets are resting limits (no slippage, need a 0.05% trade-through).
Selection: every variant is ranked on 2021-2023 only; the top ones are then judged on 2024-2026, which played
no part in choosing them. Also: how many variants are positive out of sample, and whether in-sample rank
predicts out-of-sample rank at all.
Part C: overnight hold (buy the close, sell the next pre-market / open) as a different way to catch moves.

    python -m trader.research.open_reversal
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from .pre_multi_data import OUT as MULTI
from .pre_slots import NAMES, qqq_up, soxx_bear
from .scalp_ext import D, ET

EXTRA = ["TQQQ", "SPXL", "TNA", "SQQQ"]
ALL = list(NAMES) + EXTRA
IS_END = pd.Timestamp("2023-12-31")
COST = 0.002 + 0.0000206
CACHE = D / "open_rev_mats.npz"
W = 60  # signal bars: 09:30 .. 10:29
TEND = {"10:30": 60, "11:30": 120, "15:55": 385}


# ── data ─────────────────────────────────────────────────────────────────────
def load() -> dict:
    if CACHE.exists():
        z = np.load(CACHE, allow_pickle=True)
        return {k: z[k] for k in z.files}
    parts = []
    base = [s for s in ALL if s in ("SOXL", "SOXS", "NVDA", "MU")]
    cols = ["symbol", "ts", "open", "high", "low", "close", "volume"]
    parts.append(pd.read_parquet(D / "alpaca_1m.parquet", columns=cols, filters=[("symbol", "in", base)]))
    parts.append(pd.read_parquet(MULTI, columns=cols, filters=[("symbol", "in", [s for s in ALL if s not in base])]))
    a = pd.concat(parts)
    a["et"] = a["ts"].dt.tz_convert(ET)
    a["date"] = a["et"].dt.tz_localize(None).dt.normalize()
    a["m"] = (a["et"].dt.hour - 4) * 60 + a["et"].dt.minute
    a = a[(a["m"] >= 0) & (a["m"] < 720)]
    out = {k: [] for k in ("O", "H", "L", "C", "V", "pv", "sym", "date", "pc", "sig", "trend", "next_pre", "next_open")}
    for si, sym in enumerate(ALL):
        g = a[a["symbol"] == sym].drop_duplicates(["date", "m"]).set_index(["date", "m"])
        M = {f: g[f].unstack("m").reindex(columns=range(720)).to_numpy(dtype=np.float64) for f in ("open", "high", "low", "close", "volume")}
        dates = np.array(g.index.get_level_values(0).unique().sort_values())
        Cf = pd.DataFrame(M["close"]).ffill(axis=1).to_numpy()
        dc = pd.Series(Cf[:, 719])
        typ = (M["high"] + M["low"] + M["close"]) / 3
        pvol = np.where(np.isfinite(typ) & np.isfinite(M["volume"]), typ * M["volume"], 0.0)
        vv = np.nan_to_num(M["volume"][:, :330]).sum(1)
        out["pv"].append(np.where(vv > 0, pvol[:, :330].sum(1) / np.maximum(vv, 1), np.nan))
        out["pc"].append(dc.shift(1).to_numpy())
        out["sig"].append(np.log(dc).diff().rolling(20).std().shift(1).to_numpy())
        out["trend"].append((dc.shift(1) > dc.rolling(50).mean().shift(1)).to_numpy())
        out["next_pre"].append(pd.Series(Cf[:, 90]).shift(-1).to_numpy())  # next day 05:30 ET (pre-market)
        out["next_open"].append(pd.Series(M["open"][:, 330]).shift(-1).to_numpy())
        for k, f in (("O", "open"), ("H", "high"), ("L", "low"), ("C", "close"), ("V", "volume")):
            out[k].append(M[f][:, 330:720].astype(np.float32))
        out["sym"].append(np.full(len(dates), si))
        out["date"].append(dates)
    res = {k: np.concatenate(v) for k, v in out.items()}
    res["names"] = np.array(ALL)
    np.savez(CACHE, **res)
    return res


def prepare(z: dict) -> dict:
    O, H, L, C, V = (z[k].astype(np.float64) for k in ("O", "H", "L", "C", "V"))
    ok = np.isfinite(O[:, 0]) & np.isfinite(z["sig"]) & np.isfinite(z["pv"]) & np.isfinite(z["pc"])
    keep = np.flatnonzero(ok)
    O, H, L, C, V = O[keep], H[keep], L[keep], C[keep], V[keep]
    Cf = pd.DataFrame(C).ffill(axis=1).to_numpy()
    Of = np.where(np.isfinite(O), O, np.concatenate([Cf[:, :1], Cf[:, :-1]], axis=1))  # empty minute: last close
    Lr = np.where(np.isfinite(L), L, np.inf)
    Hr = np.where(np.isfinite(H), H, -np.inf)
    typ = (H + L + C) / 3
    pvol = np.where(np.isfinite(typ) & np.isfinite(V), typ * V, 0.0)
    cv = np.cumsum(np.nan_to_num(V), 1)
    svw = np.where(cv > 0, np.cumsum(pvol, 1) / np.maximum(cv, 1), np.nan)
    dates = pd.to_datetime(z["date"][keep])
    up = qqq_up()
    bear = soxx_bear()
    d = {
        "O": Of, "H": H, "L": L, "C": Cf, "V": np.nan_to_num(V), "Lr": Lr, "Hr": Hr, "svw": svw,
        "RL": np.minimum.accumulate(Lr, 1), "RH": np.maximum.accumulate(Hr, 1),
        "pv": z["pv"][keep], "op": Of[:, 0], "pc": z["pc"][keep], "sym": z["sym"][keep], "date": dates,
        "kk": np.clip(0.4 * z["sig"][keep], 0.015, 0.06), "sigd": z["sig"][keep],
        "trend": z["trend"][keep].astype(bool), "next_pre": z["next_pre"][keep], "next_open": z["next_open"][keep],
        "qqq": np.array([up.get(x) is True or up.get(x) == 1 for x in dates]),
        "bear": np.array([bool(bear.get(x, False)) for x in dates]),
        "names": z["names"],
    }
    d["main"] = np.isin(d["sym"], [i for i, s in enumerate(z["names"]) if s in NAMES])
    d["is"] = d["date"] <= IS_END
    return d


# ── engine ───────────────────────────────────────────────────────────────────
def first_true(cond: np.ndarray) -> np.ndarray:
    return np.where(cond.any(1), cond.argmax(1), -1)


def simulate(d: dict, j: np.ndarray, stop: np.ndarray, target: np.ndarray, t_end: int) -> pd.DataFrame:
    """j = signal bar (-1 none); buy at bar j+1's open. Returns one row per trade."""
    rows = np.flatnonzero((j >= 0) & (j + 1 < t_end))
    if not len(rows):
        return pd.DataFrame(columns=["row", "ret", "why", "e"])
    e = j[rows] + 1
    slip = np.where(e < 15, 0.001, 0.0005)
    buy = d["O"][rows, e] * (1 + slip)
    st, tg = stop[rows], target[rows]
    good = np.isfinite(buy) & (tg > buy * 1.001) & ((st < buy) | ~np.isfinite(st))
    rows, e, buy, st, tg = rows[good], e[good], buy[good], st[good], tg[good]
    cols = np.arange(390)
    win = (cols[None, :] >= e[:, None]) & (cols[None, :] <= t_end)
    Lr, Hr = d["Lr"][rows], d["Hr"][rows]
    hs = win & (Lr <= np.where(np.isfinite(st), st, -1)[:, None])
    ht = win & (Hr >= (tg * 1.0005)[:, None])
    fs = np.where(hs.any(1), hs.argmax(1), 10 ** 6)
    ft = np.where(ht.any(1), ht.argmax(1), 10 ** 6)
    o_at_s = d["O"][rows, np.minimum(fs, 389)]
    stop_px = np.where((fs > e) & (o_at_s < st), o_at_s, st) * (1 - 0.0005)
    time_px = d["C"][rows, t_end] * (1 - 0.0005)
    is_stop = (fs <= ft) & (fs < 10 ** 6)
    is_tgt = ~is_stop & (ft < 10 ** 6)
    px = np.where(is_stop, stop_px, np.where(is_tgt, tg, time_px))
    return pd.DataFrame({"row": rows, "ret": px / buy - 1 - COST, "why": np.where(is_stop, "stop", np.where(is_tgt, "target", "time")), "e": e})


def at(a: np.ndarray, j: np.ndarray) -> np.ndarray:
    jj = np.clip(j, 0, a.shape[1] - 1)
    return np.where(j >= 0, a[np.arange(len(j)), jj], np.nan)


def targets(d, kind, ref, j, stop):
    buy_est = at(d["C"], j)  # the target is fixed when the signal fires (refined against the real fill in simulate)
    if kind == "ref":
        return ref.copy()
    if kind == "dip":
        return buy_est * (1 + d["kk"])
    if kind == "2R":
        return buy_est + 2 * (buy_est - stop)
    return np.full(len(j), np.inf)


def variants(d: dict):
    """Yields (family, params dict, j, stop, ref-based target array builder inputs)."""
    C, RL, RH, svw, kk = d["C"][:, :W], d["RL"][:, :W], d["RH"][:, :W], d["svw"][:, :W], d["kk"][:, None]
    refs = {"pmVWAP": d["pv"], "open": d["op"], "prevclose": d["pc"]}
    for (rn, ref), f in itertools.product(refs.items(), (0.5, 1.0, 2.0)):
        r = ref[:, None]
        flush = RL <= r * (1 - f * kk)
        for b in (0.25, 0.5, 1.0):
            cond = flush & (C >= RL * (1 + b * kk)) & (C < r)
            j = first_true(cond)
            yield "bounce", {"ref": rn, "flush": f, "bounce": b}, j, at(RL, j) * 0.998, ref
        for frac in (0.25, 0.5):
            cond = flush & (C >= RL + frac * (r - RL)) & (C < r)
            j = first_true(cond)
            yield "retrace", {"ref": rn, "flush": f, "won back": frac}, j, at(RL, j) * 0.998, ref
        for lvl in ("open", "sessVWAP"):
            if lvl == "open" and rn == "open":
                cond = flush & (C >= d["op"][:, None])
            elif lvl == "open":
                cond = flush & (C >= d["op"][:, None]) & (C < r)
            else:
                cond = flush & (C >= svw) & (C < r) if rn != "open" else flush & (C >= svw)
            j = first_true(cond)
            yield "reclaim", {"ref": rn, "flush": f, "level": lvl}, j, at(RL, j) * 0.998, ref
        for n in (5, 15):
            rlo, rhi = RL[:, n - 1], RH[:, n - 1]
            cond = (rlo <= ref * (1 - f * d["kk"]))[:, None] & (C > rhi[:, None]) & (np.arange(W)[None, :] >= n)
            j = first_true(cond)
            yield "orr", {"ref": rn, "flush": f, "range": n}, j, at(RL, j) * 0.998, ref
        for j0 in (5, 10, 15, 30):
            cond = np.zeros_like(C, dtype=bool)
            cond[:, j0 - 1] = C[:, j0 - 1] <= ref * (1 - f * d["kk"])
            j = first_true(cond)
            yield "delay", {"ref": rn, "flush": f, "at": f"09:{30 + j0}" if j0 < 30 else "10:00", "stop": "low"}, j, at(RL, j) * 0.998, ref
            yield "delay", {"ref": rn, "flush": f, "at": f"09:{30 + j0}" if j0 < 30 else "10:00", "stop": "none"}, j, np.full(len(j), np.nan), ref
    for f in (0.5, 1.0, 2.0):
        drive = RH >= d["op"][:, None] * (1 + f * kk)
        cond = drive & (C <= svw * 1.001) & (C > d["op"][:, None])
        j = first_true(cond)
        stop = at(svw, j) * (1 - 0.5 * d["kk"])
        yield "drive", {"ref": "open", "flush": f, "pullback": "sessVWAP"}, j, stop, at(RH, j)


def stats(t: pd.DataFrame, d: dict) -> dict:
    if len(t) < 30:
        return {"n": len(t)}
    r = t["ret"].to_numpy()
    isx = d["is"][t["row"].to_numpy()]
    out = {"n": len(r), "mean": r.mean(), "t": r.mean() / r.std() * np.sqrt(len(r)), "win": (r > 0).mean()}
    for k, m in (("is", isx), ("oos", ~isx)):
        x = r[m]
        out[f"{k}_n"], out[f"{k}_mean"] = len(x), x.mean() if len(x) else np.nan
        out[f"{k}_t"] = x.mean() / x.std() * np.sqrt(len(x)) if len(x) > 2 and x.std() > 0 else 0.0
    return out


def run_all(d: dict) -> tuple[pd.DataFrame, dict]:
    res, trades = [], {}
    main = d["main"]
    for fam, p, j, stop, ref in variants(d):
        for tk in ("ref", "dip", "2R", "none"):
            if fam == "drive" and tk == "ref":
                tg = ref  # the day's high so far
            elif fam == "drive":
                tg = targets(d, tk, ref, j, stop)
            else:
                tg = targets(d, tk, ref, j, stop)
            if tk == "2R" and not np.isfinite(stop).any():
                continue
            for te_name, te in TEND.items():
                for filt in ("all", "qqq"):
                    jj = np.where(main & (d["qqq"] if filt == "qqq" else True), j, -1)
                    t = simulate(d, jj, stop, tg, te)
                    key = (fam, tuple(p.items()), tk, te_name, filt)
                    s = stats(t, d)
                    res.append({"family": fam, **{k: str(v) for k, v in p.items()}, "target": tk if not (fam == "drive" and tk == "ref") else "dayhigh",
                                "exit": te_name, "filter": filt, **s})
                    trades[key] = (j, stop, tg, te)
    return pd.DataFrame(res), trades


def fmt(s: pd.Series) -> str:
    return (f"n {int(s['n']):5d} net {s['mean']:+6.2%} (t {s['t']:+5.1f}) win {s['win']:3.0%} | 2021-23 {s['is_mean']:+6.2%} (t {s['is_t']:+4.1f}, n {int(s['is_n'])}) "
            f"| 2024-26 {s['oos_mean']:+6.2%} (t {s['oos_t']:+4.1f}, n {int(s['oos_n'])})")


def label(s: pd.Series) -> str:
    p = " ".join(f"{k}={s[k]}" for k in ("ref", "flush", "bounce", "won back", "level", "range", "at", "stop", "pullback") if k in s and isinstance(s[k], str) and s[k] != "nan")
    return f"{s['family']:8s} {p:42s} target {s['target']:8s} exit {s['exit']} {s['filter']}"


def main() -> None:
    d = prepare(load())
    print(f"{len(d['date'])} name-days ({d['main'].sum()} in the 14-name book universe), {d['date'].min():%Y-%m-%d} .. {d['date'].max():%Y-%m-%d}")

    print("\n=== A. The question: after a flush, if you wait for the bounce, how often does it work? (SOXL and the 14 names) ===")
    soxl = d["sym"] == list(d["names"]).index("SOXL")
    C, RL = d["C"][:, :W], d["RL"][:, :W]
    for f, b in ((1.0, 0.5), (1.0, 1.0), (2.0, 0.5), (2.0, 1.0)):
        cond = (RL <= d["pv"][:, None] * (1 - f * d["kk"][:, None])) & (C >= RL * (1 + b * d["kk"][:, None])) & (C < d["pv"][:, None])
        j = first_true(cond)
        stop = at(RL, j) * 0.998
        for nm, msk in (("SOXL", soxl), ("14 names", d["main"])):
            t = simulate(d, np.where(msk, j, -1), stop, d["pv"], 120)
            r = t["ret"]
            print(f"  flush {f:g} dip below the pre-market VWAP, bounce {b:g} dip off the low, buy; target = VWAP, stop = the low, out 11:30 | "
                  f"{nm:8s} n {len(r):4d} net {r.mean():+.2%} win {(r > 0).mean():.0%}: target {(t['why'] == 'target').mean():.0%}, "
                  f"stop {(t['why'] == 'stop').mean():.0%}, time {(t['why'] == 'time').mean():.0%}")

    print("\n=== B. Every variant, chosen on 2021-23 only (14 names) ===")
    res, trades = run_all(d)
    res = res[res["n"] >= 60].copy()
    print(f"  {len(res)} variant results ({res['family'].nunique()} families)")
    valid = res[(res["is_n"] >= 40) & (res["oos_n"] >= 40)]
    print(f"  positive after costs: all years {(valid['mean'] > 0).mean():.0%}, 2021-23 {(valid['is_mean'] > 0).mean():.0%}, 2024-26 {(valid['oos_mean'] > 0).mean():.0%}")
    rho = valid[["is_mean", "oos_mean"]].rank().corr().iloc[0, 1]
    print(f"  does the 2021-23 ranking predict 2024-26? rank correlation {rho:+.2f}")
    print("\n  By family (median over variants; best variant by 2021-23 t):")
    for fam, g in valid.groupby("family"):
        best = g.sort_values("is_t", ascending=False).iloc[0]
        print(f"    {fam:8s} {len(g):4d} variants: median net {g['mean'].median():+.2%}, share positive 2021-23 {(g['is_mean'] > 0).mean():.0%} "
              f"2024-26 {(g['oos_mean'] > 0).mean():.0%} | best in 2021-23: {best['is_mean']:+.2%} -> 2024-26 {best['oos_mean']:+.2%}")
    print("\n  Top 15 by 2021-23 t-stat (2024-26 is the honest test):")
    top = valid.sort_values("is_t", ascending=False).head(15)
    for _, s in top.iterrows():
        print(f"    {label(s)}\n        {fmt(s)}")
    print("\n  For reference, the top 5 by 2024-26 (hindsight, not a valid pick):")
    for _, s in valid.sort_values("oos_t", ascending=False).head(5).iterrows():
        print(f"    {label(s)}\n        {fmt(s)}")
    res.to_parquet(D / "open_reversal_variants.parquet")

    print("\n=== B2. The best 2021-23 picks, checked further ===")
    names = list(d["names"])
    for _, s in top.head(5).iterrows():
        key = next(k for k in trades if k[0] == s["family"] and k[2] == (s["target"] if s["target"] != "dayhigh" else "ref") and k[3] == s["exit"]
                   and k[4] == s["filter"] and all(str(v) == s[kk] for kk, v in k[1]))
        j, stop, tg, te = trades[key]
        print(f"  {label(s)}")
        ex = np.isin(d["sym"], [names.index(x) for x in EXTRA])
        for nm, msk in (("SOXL", d["sym"] == names.index("SOXL")), ("index ETFs (never used to choose)", ex)):
            t = simulate(d, np.where(msk & (d["qqq"] if s["filter"] == "qqq" else True), j, -1), stop, tg, te)
            if len(t) >= 20:
                print(f"     {nm:34s} n {len(t):4d} net {t['ret'].mean():+.2%} win {(t['ret'] > 0).mean():.0%}")
        t = simulate(d, np.where(d["main"] & (d["qqq"] if s["filter"] == "qqq" else True), j, -1), stop, tg, te)
        t["year"] = d["date"][t["row"]].year
        t["sym"] = [names[i] for i in d["sym"][t["row"]]]
        print("     by year: " + "  ".join(f"{y}: {g['ret'].mean():+.2%} ({len(g)})" for y, g in t.groupby("year")))
        per = t.groupby("sym")["ret"].mean()
        print(f"     names positive {int((per > 0).sum())}/{len(per)}; doubling slippage costs {(np.where(t['e'] < 15, 0.001, 0.0005) + 0.0005).mean():.2%} more per trade "
              f"-> net {t['ret'].mean() - (np.where(t['e'] < 15, 0.001, 0.0005) + 0.0005).mean():+.2%}")
        j2 = np.where(j >= 0, j + 1, -1)  # one more minute of hesitation
        t2 = simulate(d, np.where(d["main"] & (d["qqq"] if s["filter"] == "qqq" else True), j2, -1), stop, tg, te)
        print(f"     entering one minute later: net {t2['ret'].mean():+.2%} (n {len(t2)})")

    print("\n=== C. Overnight: buy at 15:55, sell the next day (14 names) ===")
    buy = d["C"][:, 385] * 1.0005
    for nm, nxt in (("next 05:30 ET pre-market (Toss limit)", d["next_pre"]), ("next 09:30 open", d["next_open"])):
        r = nxt / buy - 1 - COST - (0.0005 if "open" in nm else 0.0)
        for filt, m in (("all days", d["main"]), ("QQQ above 200d", d["main"] & d["qqq"]), ("QQQ up + name above its 50d", d["main"] & d["qqq"] & d["trend"])):
            x = pd.Series(r[m & np.isfinite(r)])
            isx = d["is"][m & np.isfinite(r)]
            print(f"  sell {nm:38s} {filt:28s} n {len(x):5d} net {x.mean():+.2%} (t {x.mean() / x.std() * np.sqrt(len(x)):+.1f}) "
                  f"2021-23 {x[isx].mean():+.2%} 2024-26 {x[~isx].mean():+.2%}")


if __name__ == "__main__":
    main()
