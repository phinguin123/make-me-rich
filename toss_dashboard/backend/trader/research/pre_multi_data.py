"""Download 1-minute SIP bars (04:00-16:00 ET, split/dividend adjusted) for liquid pre-market names.

    python -m trader.research.pre_multi_data
"""
from __future__ import annotations

import asyncio

import pandas as pd

from ..alpaca import AlpacaData
from .scalp_ext import D

SYMBOLS = ["TQQQ", "SQQQ", "TNA", "SPXL", "TSLA", "AMD", "MSTR", "PLTR", "COIN", "META", "AAPL", "AMZN", "TSLL", "NVDL"]
OUT = D / "pre_multi_1m.parquet"


async def one(api: AlpacaData, sym: str) -> pd.DataFrame:
    frames = []
    for y in range(2021, 2027):
        b = await api.bars([sym], "1Min", f"{y}-01-01T00:00:00Z", f"{y}-12-31T23:59:59Z" if y < 2026 else "2026-10-13T00:00:00Z",
                           feed="sip", adjustment="all")
        if not b.empty:
            et = b["ts"].dt.tz_convert("America/New_York")
            mins = et.dt.hour * 60 + et.dt.minute
            frames.append(b[(mins >= 240) & (mins < 960)])
    df = pd.concat(frames) if frames else pd.DataFrame()
    print(f"{sym}: {len(df):,} bars", flush=True)
    return df


async def main() -> None:
    async with AlpacaData() as api:
        sem = asyncio.Semaphore(5)

        async def guarded(s):
            async with sem:
                return await one(api, s)

        frames = await asyncio.gather(*(guarded(s) for s in SYMBOLS))
    pd.concat([f for f in frames if not f.empty], ignore_index=True).to_parquet(OUT)


if __name__ == "__main__":
    asyncio.run(main())
