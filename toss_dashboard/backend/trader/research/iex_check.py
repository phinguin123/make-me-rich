"""How well does the live IEX-based opening-RVOL ranking reproduce the SIP ranking
used in the backtest? Samples days, rebuilds both top-N lists, reports overlap.

    python -m trader.research.iex_check --days 40
"""
from __future__ import annotations

import argparse
import asyncio
import random

import pandas as pd

from ..alpaca import AlpacaData
from .backtest import RES, load_opening
from .download import et_iso


async def main(n_days: int, top: int) -> None:
    opening = load_opening()
    first = opening[opening["slot"] == 0][["symbol", "date", "volume", "open"]]
    days = sorted(first["date"].unique())
    sample = sorted(random.Random(7).sample(days[20:], n_days))
    inplay = pd.read_parquet(RES / "inplay.parquet")
    inplay["date"] = pd.to_datetime(inplay["date"]).dt.date
    overlaps, rank_corr = [], []
    async with AlpacaData() as api:
        for d in sample:
            idx = days.index(d)
            window = days[idx - 14 : idx + 1]
            syms = sorted(first.loc[first["date"] == d, "symbol"])
            iex = []
            for wd in window:
                for i in range(0, len(syms), 700):
                    b = await api.bars(syms[i : i + 700], "5Min", et_iso(wd, 9, 30), et_iso(wd, 9, 34), feed="iex")
                    b["date"] = wd
                    iex.append(b[["symbol", "date", "volume"]])
            iex = pd.concat(iex)
            base = iex[iex["date"] < d].groupby("symbol")["volume"].agg(["mean", "count"])
            today = iex[iex["date"] == d].set_index("symbol")["volume"]
            base = base[base["count"] >= 5]
            rv = (today / base["mean"]).dropna()
            rv = rv[rv >= 1.0].sort_values(ascending=False)
            sip_top = inplay[(inplay["date"] == d) & (inplay["rank"] <= top)]
            iex_top = set(rv.index[:top])
            sip_set = set(sip_top["symbol"])
            if not sip_set:
                continue
            overlaps.append(len(iex_top & sip_set) / len(sip_set))
            both = sip_top.set_index("symbol")["rvol"].to_frame("sip").join(rv.rename("iex"), how="inner")
            if len(both) > 3:
                rank_corr.append(both.corr(method="spearman").iloc[0, 1])
            print(f"{d}: top-{top} overlap {overlaps[-1]:.0%}  (sip {len(sip_set)}, iex {len(iex_top)})", flush=True)
    print(f"\nmean overlap {sum(overlaps) / len(overlaps):.0%} over {len(overlaps)} days; "
          f"spearman rvol (within SIP top) {sum(rank_corr) / max(len(rank_corr), 1):.2f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=20)
    a = ap.parse_args()
    asyncio.run(main(a.days, a.top))
