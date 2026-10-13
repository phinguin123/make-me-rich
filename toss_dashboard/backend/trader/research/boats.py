"""Day-market (Blue Ocean ATS) quote quality for SOXL/SOXS from Alpaca's BOATS feed.

Samples the Blue Ocean best bid/offer every 15 minutes of every day-market session since
Toss relaunched it (Nov 2025) and reports the half-spread by hour: the real cost of a
marketable order there, which the day-market research had to assume (0.10%/side).

    python -m trader.research.boats
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from ..alpaca import DATA_URL, AlpacaData
from ..config import DATA_DIR
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

OUT = DATA_DIR / "research" / "scalp" / "boats_spreads.parquet"


async def sample(api: AlpacaData, sym: str, t: datetime) -> dict | None:
    params = {"symbols": sym, "start": t.isoformat(), "end": (t + timedelta(minutes=5)).isoformat(), "feed": "boats", "limit": 1}
    try:
        q = ((await api._get(f"{DATA_URL}/v2/stocks/quotes", params)).get("quotes") or {}).get(sym) or []
    except Exception:  # noqa: BLE001
        return None
    if not q or not q[0].get("bp") or not q[0].get("ap"):
        return None
    bp, ap = float(q[0]["bp"]), float(q[0]["ap"])
    return {"symbol": sym, "t": t, "bid": bp, "ask": ap, "half_spread": (ap - bp) / (ap + bp)}


async def collect() -> pd.DataFrame:
    if OUT.exists():
        return pd.read_parquet(OUT)
    times = []
    d = datetime(2025, 11, 3, tzinfo=ET)
    end = datetime.now(ET) - timedelta(days=1)
    while d < end:
        if d.weekday() in (6, 0, 1, 2, 3):  # sessions start Sun-Thu 20:00 ET
            start = d.replace(hour=20, minute=0)
            times += [start + timedelta(minutes=15 * k) for k in range(32)]  # 20:00 .. 03:45
        d += timedelta(days=1)
    rows = []
    async with AlpacaData() as api:
        for sym in ("SOXL", "SOXS"):
            for i in range(0, len(times), 200):
                got = await asyncio.gather(*(sample(api, sym, t) for t in times[i : i + 200]))
                rows += [g for g in got if g]
    df = pd.DataFrame(rows)
    df.to_parquet(OUT)
    return df


def main() -> None:
    df = asyncio.run(collect())
    df["t"] = pd.to_datetime(df["t"]).dt.tz_convert(ET)
    df["hour_kst"] = df["t"].dt.tz_convert("Asia/Seoul").dt.hour
    df = df[df["half_spread"].between(0, 0.05)]
    print(f"{len(df)} quote samples, {df['t'].dt.date.nunique()} sessions")
    for sym, g in df.groupby("symbol"):
        print(f"\n{sym}: median half-spread {g['half_spread'].median():.3%}, mean {g['half_spread'].mean():.3%}, "
              f"90th pct {g['half_spread'].quantile(0.9):.3%}")
        by = g.groupby("hour_kst")["half_spread"].agg(["count", "median", lambda x: x.quantile(0.9)])
        by.columns = ["n", "median", "p90"]
        print((by.assign(median=by["median"].map("{:.3%}".format), p90=by["p90"].map("{:.3%}".format))).to_string())
        m = g.set_index("t")["half_spread"].resample("MS").median()
        print("by month:", " ".join(f"{k:%Y-%m} {v:.3%}" for k, v in m.items() if np.isfinite(v)))


if __name__ == "__main__":
    main()
