"""Korean investor-flow (수급) research data: daily net buying by participant + prices.

Source: Toss Open API (investor-trading from 2020, daily candles), KOSPI + KOSDAQ common stocks.
Universe: the 2,000 largest names today, ranked point-in-time later (delisted names are unavailable).

    python -m trader.research.kr_flows download
"""
from __future__ import annotations

import asyncio
import sys

import pandas as pd

from ..config import DATA_DIR
from ..toss.client import TossAPIError, TossClient

OUT = DATA_DIR / "research" / "kr"
CATS = {
    "individual": ("individual",), "foreigner": ("foreigner",), "institution": ("institution",),
    "other_corp": ("otherCorporation",), "fin_inv": ("institution", "breakdown", "financialInvestment"),
    "insurance": ("institution", "breakdown", "insurance"), "trust": ("institution", "breakdown", "trust"),
    "pef": ("institution", "breakdown", "privateEquityFund"), "bank": ("institution", "breakdown", "bank"),
    "pension": ("institution", "breakdown", "pensionFund"),
}


def _dig(rec: dict, path: tuple) -> float | None:
    x = rec
    for k in path:
        x = (x or {}).get(k)
    try:
        return float((x or {}).get("netBuyVolume"))
    except (TypeError, ValueError):
        return None


async def universe(c: TossClient, n: int = 2000) -> pd.DataFrame:
    rows = []
    for market in ("KOSPI", "KOSDAQ"):
        res = await c.request("GET", "/api/v1/stocks/all", "STOCK_ALL", {"market": market, "status": "ACTIVE", "securityType": "STOCK", "commonShare": "true"})
        rows += [{"symbol": s["symbol"], "name": s.get("name"), "market": market} for s in res]
    df = pd.DataFrame(rows)
    prices, shares = {}, {}
    syms = list(df["symbol"])
    for i in range(0, len(syms), 50):
        for p in await c.prices(syms[i : i + 50]):
            prices[p["symbol"]] = float(p["lastPrice"])
        for s in await c.stocks(syms[i : i + 50]):
            shares[s["symbol"]] = float(s.get("sharesOutstanding") or 0)
    df["shares"] = df["symbol"].map(shares)
    df["mcap"] = df["shares"] * df["symbol"].map(prices)
    return df.sort_values("mcap", ascending=False).head(n).reset_index(drop=True)


async def flows(c: TossClient, sym: str, start: str = "2019-12-01") -> pd.DataFrame:
    rows, until = [], None
    while True:
        params = {"count": 100} | ({"until": until} if until else {})
        try:
            r = await c.request("GET", f"/api/v1/stocks/{sym}/investor-trading", "STOCK_TRADING_TREND", params)
        except TossAPIError:
            break
        recs = r.get("records") or []
        for rec in recs:
            rows.append({"date": rec["date"], **{k: _dig(rec, path) for k, path in CATS.items()}})
        until = r.get("nextUntil")
        if not recs or not until or until < start:
            break
    df = pd.DataFrame(rows)
    if not df.empty:
        df["symbol"] = sym
    return df


async def candles(c: TossClient, sym: str, start: str = "2019-06-01") -> pd.DataFrame:
    rows, before = [], None
    while True:
        try:
            r = await c.candles(sym, "1d", count=200, before=before)
        except TossAPIError:
            break
        cs = r.get("candles") or []
        rows += [{"date": x["timestamp"][:10], "open": float(x["openPrice"]), "high": float(x["highPrice"]), "low": float(x["lowPrice"]),
                  "close": float(x["closePrice"]), "volume": float(x["volume"])} for x in cs]
        before = r.get("nextBefore")
        if not cs or not before or before[:10] < start:
            break
    df = pd.DataFrame(rows)
    if not df.empty:
        df["symbol"] = sym
    return df


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    async with TossClient() as c:
        uni = await universe(c)
        uni.to_parquet(OUT / "universe.parquet")
        print(f"universe: {len(uni)} stocks", flush=True)
        done = {p.stem for p in (OUT / "parts").glob("*.parquet")} if (OUT / "parts").exists() else set()
        (OUT / "parts").mkdir(exist_ok=True)
        sem = asyncio.Semaphore(6)

        async def one(sym):
            async with sem:
                f, px = await flows(c, sym), await candles(c, sym)
            if not f.empty and not px.empty:
                f.merge(px, on=["symbol", "date"], how="outer").to_parquet(OUT / "parts" / f"{sym}.parquet")

        todo = [s for s in uni["symbol"] if s not in done]
        for i in range(0, len(todo), 60):
            await asyncio.gather(*(one(s) for s in todo[i : i + 60]))
            print(f"{min(i + 60, len(todo))}/{len(todo)}", flush=True)
    parts = [pd.read_parquet(p) for p in (OUT / "parts").glob("*.parquet")]
    pd.concat(parts, ignore_index=True).to_parquet(OUT / "kr_daily.parquet")
    print("saved", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["download"]:
        asyncio.run(main())
