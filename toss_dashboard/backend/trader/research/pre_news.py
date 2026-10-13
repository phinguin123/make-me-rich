"""Do pre-market dips WITH fresh news rebound less? (Alpaca news, Benzinga feed, 2021-2026.)

For every multi-name signal (pre_slots), count the symbol's news items between the previous
regular close (16:00 ET) and the entry minute. Compare per-trade results with and without news.

    python -m trader.research.pre_news
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..alpaca import AlpacaData
from ..config import DATA_DIR
from .pre_multi import matrices
from .pre_slots import all_trades, qqq_up
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

NEWS = "https://data.alpaca.markets/v1beta1/news"
CACHE = DATA_DIR / "research" / "pre_news_counts.parquet"


async def count(api: AlpacaData, sym: str, start: datetime, end: datetime) -> tuple[int, list[str]]:
    res = await api._get(NEWS, {"symbols": sym, "start": start.isoformat(), "end": end.isoformat(), "limit": 50, "sort": "desc"})
    items = res.get("news", [])
    return len(items), [i.get("headline", "")[:90] for i in items[:3]]


async def fetch(t: pd.DataFrame) -> pd.DataFrame:
    async with AlpacaData() as api:
        sem = asyncio.Semaphore(12)

        async def one(row):
            d = row.date.date()
            prev = d - timedelta(days=3 if d.weekday() == 0 else 1)
            start = datetime.combine(prev, datetime.min.time()).replace(hour=16, tzinfo=ET)
            end = datetime.combine(d, datetime.min.time()).replace(hour=4, tzinfo=ET) + timedelta(minutes=int(row.j))
            async with sem:
                try:
                    n, heads = await count(api, row.sym, start, end)
                except Exception:  # noqa: BLE001
                    n, heads = -1, []
            return n, " | ".join(heads)

        res = await asyncio.gather(*(one(r) for r in t.itertuples()))
    t = t.copy()
    t["news"] = [r[0] for r in res]
    t["heads"] = [r[1] for r in res]
    return t


def main() -> None:
    mats = matrices()
    up = qqq_up()
    for model in ("reactive", "pessimistic"):
        t = all_trades(mats, model)
        if model == "reactive":
            t = asyncio.run(fetch(t))
            t[["date", "sym", "j", "news", "heads"]].to_parquet(CACHE)
        else:
            t = t.merge(pd.read_parquet(CACHE)[["date", "sym", "j", "news"]], on=["date", "sym", "j"], how="left")
        t = t[t["news"] >= 0]
        t["up"] = t["date"].map(up) == True  # noqa: E712
        print(f"=== {model} fills, {len(t)} signals with news data ===")
        for label, sub in (("all", t), ("QQQ uptrend only", t[t["up"]])):
            for name, g in (("no news", sub[sub["news"] == 0]), ("1-2 news", sub[sub["news"].between(1, 2)]), ("3+ news", sub[sub["news"] >= 3])):
                if len(g) > 5:
                    tt = g["net"].mean() / g["net"].std() * np.sqrt(len(g))
                    print(f"  [{label:16s}] {name:9s} n {len(g):4d} avg {g['net'].mean():+.2%} (t {tt:+.1f}) win {(g['net'] > 0).mean():.0%}")
        if model == "reactive":
            worst = t[t["news"] >= 3].nsmallest(5, "net")
            for r in worst.itertuples():
                print(f"    {r.date:%Y-%m-%d} {r.sym} {r.net:+.1%}: {r.heads[:150]}")


if __name__ == "__main__":
    main()
