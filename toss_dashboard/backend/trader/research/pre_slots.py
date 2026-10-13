"""Multi-name pre-market dip with FIXED SLOTS, signals taken in time order (as live), QQQ filter.

Live can't split the pot by the day's number of signals (they arrive one by one), so each trade
takes 1/slots of the pot when a slot is free (one position per name). Names: the 14 non-index names.

    python -m trader.research.pre_slots
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .pre_multi import matrices
from .scalp_ext import D, ET, _dip_trade, net

NAMES = ["SOXL", "SOXS", "NVDA", "MU", "AMD", "TSLA", "MSTR", "PLTR", "COIN", "META", "AAPL", "AMZN", "TSLL", "NVDL"]


def qqq_up() -> pd.Series:
    q = pd.read_parquet(D / "alpaca_1m.parquet", columns=["symbol", "ts", "close"])
    q = q[q["symbol"] == "QQQ"]
    q["et"] = q["ts"].dt.tz_convert(ET)
    q = q[(q["et"].dt.hour * 60 + q["et"].dt.minute) <= 959]
    c = q.groupby(q["et"].dt.tz_localize(None).dt.normalize())["close"].last()
    return (c > c.rolling(200, min_periods=150).mean()).shift(1)


def all_trades(mats: dict, model: str) -> pd.DataFrame:
    rows = []
    for sym in NAMES:
        M = mats[sym]
        C, L, H, V = M["close"], M["low"], M["high"], M["volume"]
        sig = np.log(C[719]).diff().rolling(20).std().shift(1)
        for d in C.index:
            s = sig.get(d)
            if not np.isfinite(s):
                continue
            c, lo, hi, v = C.loc[d].to_numpy(), L.loc[d].to_numpy(), H.loc[d].to_numpy(), V.loc[d].to_numpy()
            first = np.flatnonzero(np.isfinite(lo[:60]))
            if not len(first):
                continue
            r = _dip_trade(c, lo, hi, np.nan_to_num(v), first[0], min(0.06, max(0.015, 0.4 * s)), model, detail=True)
            if r is not None:
                rows.append({"date": d, "sym": sym, "j": r[1], "q": r[2], "net": float(net([r[0]], "pre", limit=True)[0])})
    return pd.DataFrame(rows)


def simulate(t: pd.DataFrame, slots: int, up: pd.Series | None, max_per_day: int = 99) -> pd.Series:
    """Daily pot return: each accepted trade uses 1/slots of the pot; slots free up at the trade's exit."""
    out = {}
    for d, g in t.groupby("date"):
        if up is not None and up.get(d) is not True and up.get(d) != 1:
            continue
        busy: list[int] = []  # exit minutes of open slots
        taken = 0
        r = 0.0
        for row in g.sort_values("j").itertuples():
            busy = [q for q in busy if q > row.j]
            if len(busy) >= slots or taken >= max_per_day:
                continue
            busy.append(row.q)
            taken += 1
            r += row.net / slots
        out[d] = r
    return pd.Series(out)


def main() -> None:
    mats = matrices()
    up = qqq_up()
    days = pd.bdate_range("2021-03-01", "2026-10-09")
    for model in ("reactive", "pessimistic"):
        t = all_trades(mats, model)
        print(f"=== {model} fills: {len(t)} signals on 14 names ===")
        for slots in (1, 2, 3, 4, 6):
            for filt in (None, up):
                r = simulate(t, slots, filt).reindex(days).fillna(0.0)
                eq = (1 + r).cumprod()
                yrs = len(r) / 252
                yearly = (1 + r).groupby(r.index.year).prod() - 1
                n = int((r != 0).sum())
                print(f"  slots {slots} {'QQQ filter' if filt is not None else 'no filter ':10s} CAGR {eq.iloc[-1] ** (1 / yrs) - 1:+6.1%} "
                      f"Sharpe {r.mean() / r.std() * np.sqrt(252):4.2f} maxDD {(eq / eq.cummax() - 1).min():+6.1%} trade days/yr {n / yrs:4.0f} | "
                      + " ".join(f"{k}:{v:+.0%}" for k, v in yearly.items()))
        # half split for the chosen config
        r = simulate(t, 3, up).reindex(days).fillna(0.0)
        a, b = r.loc[:"2023-12-31"], r.loc["2024-01-01":]
        print(f"  slots 3 + QQQ filter: Sharpe 2021-23 {a.mean() / a.std() * np.sqrt(252):.2f}, 2024-26 {b.mean() / b.std() * np.sqrt(252):.2f}")


if __name__ == "__main__":
    main()
