"""Download research data from Alpaca (SIP) into data/research/.

Stages (each is cached and resumable):
  1. assets + daily bars for every NYSE/NASDAQ/AMEX/ARCA/BATS symbol
  2. opening 5-minute bars (09:30-10:00 ET) for each day's liquid candidates
  3. 1-minute bars for each day's top stocks-in-play (day D and D+1)
  4. 1-minute bars for macro proxy ETFs

    python -m trader.research.download --start 2024-01-01 --end 2026-10-07
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..alpaca import AlpacaData
from ..config import DATA_DIR

LOG = logging.getLogger("download")
ET = ZoneInfo("America/New_York")
OUT = DATA_DIR / "research"
EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
MACRO = ["SPY", "QQQ", "IWM", "TLT", "IEF", "UUP", "GLD", "USO", "VIXY", "IBIT", "HYG", "SMH"]
FUND_RE = re.compile(
    r"\b(?:ETF|ETN|ETP|Fund|Trust|iShares|SPDR|ProShares|Direxion|Invesco|Vanguard|Leveraged|Inverse|"
    r"Ultra|UltraPro|Bull|Bear|2X|3X|-1X|Daily|Portfolio|Index|Notes|Warrant|Rights?|Units?)\b",
    re.IGNORECASE,
)
TOP_N = 30


def et_iso(d: date, hh: int, mm: int) -> str:
    return datetime.combine(d, time(hh, mm), ET).isoformat()


async def stage_daily(api: AlpacaData, start: str, end: str) -> pd.DataFrame:
    path = OUT / "daily.parquet"
    if path.exists():
        return pd.read_parquet(path)
    assets = pd.DataFrame(await api.assets())
    assets = assets[assets["exchange"].isin(EXCHANGES)]
    assets = assets[~assets["symbol"].str.contains(r"[^A-Z.]", regex=True)]
    assets["is_fund"] = assets["name"].fillna("").str.contains(FUND_RE)
    assets[["symbol", "name", "exchange", "status", "is_fund"]].to_parquet(OUT / "assets.parquet")
    symbols = sorted(assets["symbol"].unique())
    LOG.info("daily bars for %d symbols", len(symbols))
    batches = [symbols[i : i + 100] for i in range(0, len(symbols), 100)]
    sem = asyncio.Semaphore(12)

    async def one(batch):
        async with sem:
            return await api.bars(batch, "1Day", start, end)

    frames = []
    for i, fut in enumerate(asyncio.as_completed([one(b) for b in batches])):
        frames.append(await fut)
        if i % 20 == 0:
            LOG.info("daily %d/%d", i + 1, len(batches))
    daily = pd.concat(frames, ignore_index=True)
    daily["date"] = daily["ts"].dt.tz_convert(ET).dt.date
    daily = daily.drop(columns=["ts"]).sort_values(["symbol", "date"]).reset_index(drop=True)
    daily.to_parquet(path)
    return daily


def daily_features(daily: pd.DataFrame, assets: pd.DataFrame) -> pd.DataFrame:
    """Per symbol-day features known BEFORE that day's open (all shifted by one day)."""
    d = daily.sort_values(["symbol", "date"]).copy()
    g = d.groupby("symbol", sort=False)
    prev_close = g["close"].shift(1)
    tr = np.maximum(d["high"], prev_close) - np.minimum(d["low"], prev_close)
    d["tr"] = tr.fillna(d["high"] - d["low"])
    d["atr14"] = g["tr"].transform(lambda s: s.rolling(14, min_periods=10).mean().shift(1))
    d["adv14"] = g["volume"].transform(lambda s: s.rolling(14, min_periods=10).mean().shift(1))
    d["prev_close"] = prev_close
    d["prev_high"] = g["high"].shift(1)
    d["sma50_prev"] = g["close"].transform(lambda s: s.rolling(50, min_periods=30).mean().shift(1))
    fund = assets.groupby("symbol")["is_fund"].max()
    d["is_fund"] = d["symbol"].map(fund).fillna(True)
    return d


async def stage_opening(api: AlpacaData, feats: pd.DataFrame, days: list[date]) -> pd.DataFrame:
    """5-minute bars 09:30-10:00 for each day's candidates. Saved per month."""
    odir = OUT / "opening"
    odir.mkdir(exist_ok=True)
    cand = feats[
        (feats["prev_close"] > 5) & (feats["adv14"] >= 1_000_000) & (feats["atr14"] >= 0.5) & (~feats["is_fund"])
    ]
    by_day = cand.groupby("date")["symbol"].apply(list).to_dict()
    months = sorted({(d.year, d.month) for d in days})
    sem = asyncio.Semaphore(12)

    async def one(d: date, syms: list[str]):
        async with sem:
            return await api.bars(syms, "5Min", et_iso(d, 9, 30), et_iso(d, 9, 59))

    out = []
    for y, m in months:
        path = odir / f"{y}-{m:02d}.parquet"
        if path.exists():
            out.append(pd.read_parquet(path))
            continue
        jobs = []
        for d in (x for x in days if x.year == y and x.month == m):
            syms = by_day.get(d, [])
            for i in range(0, len(syms), 700):
                jobs.append(one(d, syms[i : i + 700]))
        frames = [f for f in await asyncio.gather(*jobs) if not f.empty]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        df.to_parquet(path)
        LOG.info("opening %d-%02d: %d bars", y, m, len(df))
        out.append(df)
    return pd.concat(out, ignore_index=True)


def rank_in_play(opening: pd.DataFrame, feats: pd.DataFrame) -> pd.DataFrame:
    """Relative volume of the first 5-minute bar vs its 14-session average."""
    o = opening.copy()
    o["et"] = o["ts"].dt.tz_convert(ET)
    o["date"] = o["et"].dt.date
    first = o[o["et"].dt.time == time(9, 30)][["symbol", "date", "open", "high", "low", "close", "volume"]]
    first = first.sort_values(["symbol", "date"])
    first["or_vol_avg"] = first.groupby("symbol")["volume"].transform(
        lambda s: s.rolling(14, min_periods=5).mean().shift(1)
    )
    first["rvol"] = first["volume"] / first["or_vol_avg"]
    f = feats[["symbol", "date", "atr14", "adv14", "prev_close", "prev_high", "sma50_prev"]]
    first = first.merge(f, on=["symbol", "date"], how="left")
    first = first[(first["open"] > 5) & first["rvol"].notna() & (first["rvol"] >= 1.0)]
    first["rank"] = first.groupby("date")["rvol"].rank(ascending=False, method="first")
    return first[first["rank"] <= TOP_N].reset_index(drop=True)


async def stage_minutes(api: AlpacaData, inplay: pd.DataFrame, days: list[date]) -> None:
    mdir = OUT / "minute"
    mdir.mkdir(exist_ok=True)
    nxt = {d: days[i + 1] for i, d in enumerate(days[:-1])}
    by_day = inplay.groupby("date")["symbol"].apply(list).to_dict()
    sem = asyncio.Semaphore(12)

    async def one(d: date, syms: list[str]):
        path = mdir / f"{d.isoformat()}.parquet"
        if path.exists() or d not in nxt:
            return
        async with sem:
            df = await api.bars(syms, "1Min", et_iso(d, 9, 30), et_iso(nxt[d], 15, 59))
        df.to_parquet(path)

    items = sorted(by_day.items())
    for i in range(0, len(items), 60):
        await asyncio.gather(*(one(d, s) for d, s in items[i : i + 60]))
        LOG.info("minutes %d/%d days", min(i + 60, len(items)), len(items))


async def stage_macro(api: AlpacaData, start: str, end: str) -> None:
    path = OUT / "macro_1m.parquet"
    if path.exists():
        return
    df = await api.bars(MACRO, "1Min", start, end)
    df.to_parquet(path)
    LOG.info("macro bars: %d", len(df))


async def main(start: str, end: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    warm = (date.fromisoformat(start) - timedelta(days=120)).isoformat()
    async with AlpacaData() as api:
        daily = await stage_daily(api, warm, f"{end}T23:59:00-04:00")
        assets = pd.read_parquet(OUT / "assets.parquet")
        feats = daily_features(daily, assets)
        feats.to_parquet(OUT / "features.parquet")
        days = sorted(d for d in daily.loc[daily["symbol"] == "SPY", "date"] if d >= date.fromisoformat(start))
        LOG.info("%d trading days", len(days))
        await stage_macro(api, start, f"{end}T23:59:00-04:00")
        opening = await stage_opening(api, feats, days)
        inplay = rank_in_play(opening, feats)
        inplay.to_parquet(OUT / "inplay.parquet")
        LOG.info("in-play rows: %d", len(inplay))
        await stage_minutes(api, inplay, days)
    LOG.info("done")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-10-06")
    args = ap.parse_args()
    asyncio.run(main(args.start, args.end))
