"""SOXL/SOXS scalping research data.

  Alpaca SIP 1-minute bars, 2021 -> now, incl. pre/after market (04:00-20:00 ET), for SOXL, SOXS
  and their drivers (SOXX, SMH, QQQ, NVDA, MU).
  Toss 1-minute bars for SOXL/SOXS covering the Korean-hours day market (Blue Ocean), available
  since Toss relaunched it (2025-11-04).

    python -m trader.research.scalp_data
"""
from __future__ import annotations

import asyncio

import pandas as pd

from ..alpaca import AlpacaData
from ..config import DATA_DIR
from ..toss.client import TossClient

OUT = DATA_DIR / "research" / "scalp"
SYMS = ["SOXL", "SOXS", "SOXX", "SMH", "QQQ", "NVDA", "MU"]


async def alpaca_minutes(start="2021-01-01", end="2026-10-12T20:00:00-04:00"):
    async with AlpacaData() as api:
        frames = await asyncio.gather(*(api.bars([s], "1Min", start, end, adjustment="all") for s in SYMS))
    d = pd.concat(frames, ignore_index=True)
    d.to_parquet(OUT / "alpaca_1m.parquet")
    print("alpaca 1m bars:", len(d), d.groupby("symbol").size().to_dict(), flush=True)


async def toss_minutes(start="2025-11-01"):
    rows = []
    async with TossClient() as c:
        for sym in ("SOXL", "SOXS"):
            before = None
            while True:
                r = await c.candles(sym, "1m", count=200, before=before)
                cs = r.get("candles") or []
                rows += [{"symbol": sym, "ts": x["timestamp"], "open": float(x["openPrice"]), "high": float(x["highPrice"]),
                          "low": float(x["lowPrice"]), "close": float(x["closePrice"]), "volume": float(x["volume"])} for x in cs]
                before = r.get("nextBefore")
                if not cs or not before or before[:10] < start:
                    break
    d = pd.DataFrame(rows)
    d["ts"] = pd.to_datetime(d["ts"], utc=True)
    d.drop_duplicates(["symbol", "ts"]).to_parquet(OUT / "toss_1m.parquet")
    print("toss 1m bars:", len(d), flush=True)


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    await asyncio.gather(alpaca_minutes(), toss_minutes())


if __name__ == "__main__":
    asyncio.run(main())
