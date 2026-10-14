"""The scalp pot as a 3-stage daily relay: does adding the stocks-in-play sleeve on uptrend days help?

One shared pot, every trading day:
  1. pre-market dip book (live rules: 14 names, 3 slots of a third of the pot, QQQ filter, buys 04:30-09:00 ET,
     take profit at +dip, leftovers sold at 10:30, or 09:59 on breakout days; strict fills, 0.05% slippage on
     the time exit)
  2a. semis downtrend (SOXX prior close below its 50-day average): the book is flat by 09:59 and the SOXL/SOXS
     30-minute breakout uses the whole pot from 10:00 (research/scalp.py e2_orb, as live)
  2b. semis uptrend (the breakout stands aside): the book keeps its 10:30 exit, and the stocks-in-play
     opening-range breakout (research/backtest.py, the live strategy.py profiles, entries 09:35-11:30 ET)
     uses only cash that is free at the moment of each entry: slots the book never used, book trades whose
     take-profit already filled, and from 10:30 the book's time-exit proceeds. Its sizing is the live one
     (1% of the pot at risk to the ATR stop, at most max_pos_pct of the pot, 4 positions, one per name),
     whole shares, Toss fees. Positions close by 16:00 (exit_mode "eod" instead of the sleeve's
     hold-strong-overnight, so the pot is flat for the next morning's book).
The stocks-in-play data covers 2024-01 .. 2026-10 only: before 2024 the relay equals the current scalp.

Then the account (yearly rebalance between the pots, as live), 2021-2026 and 2024-2026 (each started at
$12,400):
  current       30% scalp (book + bear breakout) / 70% swing (20% volatility target, 60% stock vol cap)
  relay         30% relay / 70% swing
  relay 35/65   35% relay / 65% swing with a 28% swing volatility target (cap stays 60%, realized vol)
plus the two in-between rows that separate the effects.

    python -m trader.research.intraday_relay
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import time

import numpy as np
import pandas as pd

from . import portfolio as P
from .backtest import SEC_FEE, Params, generate_trades, load_all
from .edge_stats import orb_trades
from .pre_multi import matrices
from . import scalp as scalp_research
from .pre_slots import NAMES, all_trades, qqq_up

SLOTS = 3
START = 12400.0
SAFE = Params(strategy="orb", or_minutes=5, top_n=30, require_green=True, min_atr_pct=0.06, require_trend=True,
              stop_mode="atr", stop_atr=0.10, trail_atr=0.75, exit_mode="eod", regime="risk_on",
              entry_cutoff=time(11, 30), max_positions=4, risk_per_trade=0.01, max_pos_pct=0.25)
BIGGER = replace(SAFE, regime="none", min_atr_pct=0.05, max_pos_pct=0.35)


def minute(ts) -> int:
    """Minutes after 04:00 ET."""
    return (ts.hour - 4) * 60 + ts.minute


# ── one day of the relay (pure: unit-tested) ──────────────────────────────────
def relay_day(pot: float, book: list[tuple[int, int, float]], bear: bool, orb_net: float | None,
              sip: list[dict] | None, p: Params) -> dict:
    """P&L of one day on a pot of `pot` dollars.
    book: accepted book trades (entry minute, exit minute, net return), each a third of the pot.
    sip: stocks-in-play candidates in entry order with entry_min, exit_min, entry, stop0, exit, symbol."""
    slot = pot / SLOTS
    book_pnl = sum(slot * net for _, _, net in book)
    if bear:
        return {"book": book_pnl, "orb": pot * orb_net if orb_net is not None else 0.0, "sip": 0.0, "sip_n": 0, "sip_skipped": 0}
    cash = pot - slot * len(book)  # the book buys before 09:00, before any stocks-in-play entry (09:35+)
    pending_book = sorted((q, slot * (1 + net)) for _, q, net in book)
    open_sip: list[dict] = []
    sip_pnl, n, skipped = 0.0, 0, 0
    for tr in sip or []:
        t = tr["entry_min"]
        while pending_book and pending_book[0][0] < t:  # proceeds of book exits that happened before this minute
            cash += pending_book.pop(0)[1]
        for pos in [x for x in open_sip if x["exit_min"] < t]:
            cash += pos["proceeds"]
            open_sip.remove(pos)
        if len(open_sip) >= p.max_positions or any(x["symbol"] == tr["symbol"] for x in open_sip):
            continue
        risk = tr["entry"] - tr["stop0"]
        if risk <= 0:
            continue
        shares = math.floor(min(p.risk_per_trade * pot / risk, p.max_pos_pct * pot / tr["entry"], cash / (tr["entry"] * (1 + p.commission))))
        if shares < 1:
            skipped += 1  # wanted to trade but the pot's cash was busy
            continue
        cost = shares * tr["entry"] * (1 + p.commission)
        proceeds = shares * tr["exit"] * (1 - p.commission - SEC_FEE)
        cash -= cost
        open_sip.append({"symbol": tr["symbol"], "exit_min": tr["exit_min"], "proceeds": proceeds})
        sip_pnl += proceeds - cost
        n += 1
    return {"book": book_pnl, "orb": 0.0, "sip": sip_pnl, "sip_n": n, "sip_skipped": skipped}


# ── inputs ───────────────────────────────────────────────────────────────────
def book_trades(bear: pd.Series) -> dict[pd.Timestamp, list[tuple[int, int, float]]]:
    """The live book's accepted trades per day: strict fills, slots in time order, QQQ filter."""
    t = all_trades(matrices(), "pessimistic", bear=bear)
    up = qqq_up()
    order = {s: i for i, s in enumerate(NAMES)}
    t["o"] = t["sym"].map(order)
    out = {}
    for d, g in t.groupby("date"):
        if up.get(d) is not True and up.get(d) != 1:
            continue
        busy, acc = [], []
        for row in g.sort_values(["j", "o"]).itertuples():
            busy = [q for q in busy if q > row.j]
            if len(busy) >= SLOTS:
                continue
            busy.append(row.q)
            acc.append((int(row.j), int(row.q), float(row.net)))
        out[pd.Timestamp(d)] = acc
    return out


def sip_trades(p: Params, data) -> dict[pd.Timestamp, list[dict]]:
    inplay, macro, cache, _ = data
    tr = generate_trades(p, inplay, macro, cache)
    tr = tr[~tr["overnight"]].copy()
    tr = tr.sort_values(["entry_ts", "rvol"], ascending=[True, False])
    out: dict[pd.Timestamp, list[dict]] = {}
    for row in tr.itertuples(index=False):
        out.setdefault(pd.Timestamp(row.date), []).append({"entry_min": minute(row.entry_ts), "exit_min": minute(row.exit_ts), "entry": row.entry,
                                                           "stop0": row.stop0, "exit": row.exit, "symbol": row.symbol})
    return out


def swing_returns(vol_target: float) -> pd.Series:
    w = P.load()
    eq, _ = P.backtest(w, replace(P.PP(), vol_target=vol_target))
    r = eq.pct_change().fillna(eq.iloc[0] / P.PP().capital - 1)
    r.index = pd.to_datetime(r.index)
    return r


# ── the account ──────────────────────────────────────────────────────────────
def account(days: pd.DatetimeIndex, scalp_share: float, swing_r: pd.Series, book: dict, bear: pd.Series, orb: pd.Series,
            sip: dict | None, p: Params) -> tuple[pd.Series, dict]:
    scalp, swing = START * scalp_share, START * (1 - scalp_share)
    year = days[0].year
    vals, info = [], {"sip_n": 0, "sip_skipped": 0, "sip_pnl": 0.0, "bull_days": 0, "sip_days": 0}
    for d in days:
        if d.year != year:  # yearly rebalance, as the allocator does on the first trading day
            total = scalp + swing
            scalp, swing = total * scalp_share, total * (1 - scalp_share)
            year = d.year
        b = bool(bear.get(d, False))
        res = relay_day(scalp, book.get(d, []), b, orb.get(d), (sip or {}).get(d), p)
        if not b:
            info["bull_days"] += 1
            info["sip_days"] += int(res["sip_n"] > 0)
        info["sip_n"] += res["sip_n"]
        info["sip_skipped"] += res["sip_skipped"]
        info["sip_pnl"] += res["sip"]
        scalp += res["book"] + res["orb"] + res["sip"]
        swing *= 1 + swing_r.get(d, 0.0)
        vals.append(scalp + swing)
    return pd.Series(vals, index=days), info


def metrics(eq: pd.Series) -> dict:
    r = eq.pct_change().fillna(eq.iloc[0] / START - 1)
    yrs = len(eq) / 252
    yearly = eq.resample("YE").last()
    yearly = yearly / yearly.shift(1).fillna(START) - 1
    return {"CAGR": (eq.iloc[-1] / START) ** (1 / yrs) - 1, "Sharpe": r.mean() / r.std() * np.sqrt(252),
            "maxDD": (eq / eq.cummax() - 1).min(), "worst yr": yearly.min(), "end": eq.iloc[-1]}


def main() -> None:
    print("loading the book, the breakout, swing and stocks-in-play ...", flush=True)
    m, _ = scalp_research.load_matrices()
    bear = m["regime"] == "bear"  # the breakout's own regime (as live and in the replay check)
    del m
    book = book_trades(bear)
    orb = orb_trades().groupby("date")["net"].sum()
    orb.index = pd.to_datetime(orb.index)
    swing = {0.20: swing_returns(0.20), 0.28: swing_returns(0.28)}
    data = load_all()
    sips = {"safe": sip_trades(SAFE, data), "bigger-swing": sip_trades(BIGGER, data)}
    params = {"safe": SAFE, "bigger-swing": BIGGER}
    allday = swing[0.20].index
    first_sip = min(min(v) for v in sips.values())
    print(f"stocks-in-play candidates from {first_sip:%Y-%m-%d}: safe {sum(len(v) for v in sips['safe'].values())}, "
          f"bigger-swing {sum(len(v) for v in sips['bigger-swing'].values())} (close-by-16:00 trades)")

    configs = [
        ("current: 30% scalp / 70% swing (vol 20%)", 0.30, 0.20, None),
        ("relay safe: 30% / 70% swing (vol 20%)", 0.30, 0.20, "safe"),
        ("relay bigger-swing: 30% / 70% swing (vol 20%)", 0.30, 0.20, "bigger-swing"),
        ("relay safe: 35% / 65% swing (vol 28%)", 0.35, 0.28, "safe"),
        ("relay bigger-swing: 35% / 65% swing (vol 28%)", 0.35, 0.28, "bigger-swing"),
        ("  in between: current 30% / 70% swing (vol 28%)", 0.30, 0.28, None),
        ("  in between: current 35% / 65% swing (vol 28%)", 0.35, 0.28, None),
    ]
    for label, lo in (("2021-01 .. 2026-10 (stocks-in-play only from 2024)", "2021-01-04"), ("2024-01 .. 2026-10", "2024-01-02")):
        days = allday[(allday >= lo) & (allday <= pd.Timestamp("2026-10-09"))]
        print(f"\n=== {label}: {len(days)} days, start ${START:,.0f} ===")
        print(f"{'plan':50s} {'CAGR':>7s} {'Sharpe':>6s} {'maxDD':>7s} {'worst yr':>8s} {'end $':>9s} | stocks-in-play")
        for name, share, vt, prof in configs:
            eq, info = account(days, share, swing[vt], book, bear, orb, sips[prof] if prof else None, params[prof] if prof else SAFE)
            m = metrics(eq)
            extra = (f"{info['sip_n']} trades on {info['sip_days']} of {info['bull_days']} uptrend days, P&L ${info['sip_pnl']:+,.0f}, "
                     f"{info['sip_skipped']} skipped for busy cash") if prof else ""
            print(f"{name:50s} {m['CAGR']:+7.1%} {m['Sharpe']:6.2f} {m['maxDD']:+7.1%} {m['worst yr']:+8.1%} {m['end']:9,.0f} | {extra}")

    # the relay pot on its own, 2024-2026: how much does the stocks-in-play leg add, and does it collide with the book?
    days = allday[(allday >= "2024-01-02") & (allday <= pd.Timestamp("2026-10-09"))]
    print("\n=== The scalp pot alone, 2024-01 .. 2026-10 (start $3,720) ===")
    for prof in (None, "safe", "bigger-swing"):
        pot, vals, legs = 3720.0, [], {"book": 0.0, "orb": 0.0, "sip": 0.0}
        for d in days:
            res = relay_day(pot, book.get(d, []), bool(bear.get(d, False)), orb.get(d), sips[prof].get(d) if prof else None, params[prof] if prof else SAFE)
            for k in legs:
                legs[k] += res[k]
            pot += res["book"] + res["orb"] + res["sip"]
            vals.append(pot)
        eq = pd.Series(vals, index=days)
        r = eq.pct_change().fillna(eq.iloc[0] / 3720 - 1)
        yrs = len(r) / 252
        print(f"  {('relay ' + prof) if prof else 'current (book + bear breakout)':28s} CAGR {(eq.iloc[-1] / 3720) ** (1 / yrs) - 1:+6.1%} "
              f"Sharpe {r.mean() / r.std() * np.sqrt(252):4.2f} maxDD {(eq / eq.cummax() - 1).min():+6.1%} | P&L book ${legs['book']:+,.0f}, "
              f"breakout ${legs['orb']:+,.0f}, stocks-in-play ${legs['sip']:+,.0f}")


if __name__ == "__main__":
    main()
