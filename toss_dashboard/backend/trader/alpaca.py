"""Alpaca market-data client (free plan): SIP history, IEX realtime, crypto.

Only used for data. Orders always go through Toss.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Iterable

import aiohttp
import pandas as pd

from .config import SETTINGS

LOG = logging.getLogger(__name__)
DATA_URL = "https://data.alpaca.markets"
ASSETS_URL = "https://paper-api.alpaca.markets/v2/assets"

BAR_COLUMNS = {"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "n": "trades", "vw": "vwap"}


class AsyncRateLimiter:
    """Spaces calls to at most `per_minute` per rolling minute."""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / per_minute
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            await asyncio.sleep(delay)


class AlpacaData:
    def __init__(self, per_minute: int = 190):
        self.headers = {
            "APCA-API-KEY-ID": SETTINGS.alpaca_key,
            "APCA-API-SECRET-KEY": SETTINGS.alpaca_secret,
        }
        self.limiter = AsyncRateLimiter(per_minute)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "AlpacaData":
        self._session = aiohttp.ClientSession(headers=self.headers, timeout=aiohttp.ClientTimeout(total=60))
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    async def _get(self, url: str, params: dict) -> dict:
        assert self._session is not None
        for attempt in range(6):
            await self.limiter.wait()
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if resp.status in (401, 403):
                        raise PermissionError(f"alpaca {resp.status}: {await resp.text()}")
                    if resp.status >= 500:
                        await asyncio.sleep(1 + attempt)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                LOG.warning("alpaca GET retry %s: %s", attempt, exc)
                await asyncio.sleep(1 + attempt)
        raise RuntimeError(f"alpaca GET failed: {url} {params.get('symbols', '')[:60]}")

    async def assets(self) -> list[dict]:
        assert self._session is not None
        async with self._session.get(ASSETS_URL, params={"asset_class": "us_equity"}) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def bars(
        self,
        symbols: Iterable[str],
        timeframe: str,
        start: str,
        end: str,
        feed: str = "sip",
        adjustment: str = "split",
    ) -> pd.DataFrame:
        """Multi-symbol bars with pagination. Returns long frame [symbol, ts, o,h,l,c,v,...]."""
        params = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": start,
            "end": end,
            "feed": feed,
            "adjustment": adjustment,
            "limit": 10000,
            "sort": "asc",
        }
        frames = []
        while True:
            data = await self._get(f"{DATA_URL}/v2/stocks/bars", params)
            for sym, rows in (data.get("bars") or {}).items():
                if rows:
                    df = pd.DataFrame(rows).rename(columns=BAR_COLUMNS)
                    df["symbol"] = sym
                    frames.append(df)
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        if not frames:
            return pd.DataFrame(columns=["symbol", *BAR_COLUMNS.values()])
        out = pd.concat(frames, ignore_index=True)
        out["ts"] = pd.to_datetime(out["ts"], utc=True)
        return out

    async def snapshots(self, symbols: Iterable[str], feed: str = "iex") -> dict:
        return await self._get(f"{DATA_URL}/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": feed})
