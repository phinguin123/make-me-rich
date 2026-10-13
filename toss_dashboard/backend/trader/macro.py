"""Free macro data: FRED (rates, credit spreads, dollar) and Yahoo (futures, VIX curve).

No API keys needed. Used by the research scripts and by the live regime model.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import pandas as pd
import requests

LOG = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0"}

FRED_SERIES = {
    "DGS10": "10y Treasury yield",
    "DGS2": "2y Treasury yield",
    "T10Y3M": "10y-3m curve",
    "BAMLH0A0HYM2": "High-yield credit spread",
    "VIXCLS": "VIX close",
    "DTWEXBGS": "Broad dollar index",
    "NFCI": "Chicago Fed financial conditions",
}
YAHOO_SYMBOLS = {
    "ES=F": "S&P 500 futures",
    "NQ=F": "Nasdaq 100 futures",
    "ZN=F": "10y note futures",
    "CL=F": "Crude oil futures",
    "GC=F": "Gold futures",
    "DX-Y.NYB": "Dollar index (DXY)",
    "^VIX": "VIX",
    "^VIX3M": "3-month VIX",
    "^MOVE": "Treasury volatility (MOVE)",
    "^TNX": "10y yield (CBOE)",
    "^SOX": "Philadelphia semiconductor index",
}


def fred(series: str, start: str = "2015-01-01") -> pd.Series:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", series]
    df["date"] = pd.to_datetime(df["date"])
    s = pd.to_numeric(df[series], errors="coerce")
    return pd.Series(s.values, index=df["date"], name=series).dropna()


def yahoo(symbol: str, start: str = "2015-01-01") -> pd.DataFrame:
    """Daily OHLCV from Yahoo's chart endpoint (index = exchange-local date)."""
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.now(timezone.utc).timestamp())
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    r = requests.get(url, params={"period1": p1, "period2": p2, "interval": "1d"}, headers=UA, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    idx = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(res["meta"]["exchangeTimezoneName"]).tz_localize(None).normalize()
    df = pd.DataFrame({k: q.get(k) for k in ("open", "high", "low", "close", "volume")}, index=idx)
    return df[~df.index.duplicated(keep="last")].dropna(subset=["close"])


def load_all(start: str = "2015-01-01") -> pd.DataFrame:
    """Wide daily frame of macro closes (FRED + Yahoo), forward-filled across holidays."""
    cols = {}
    for s in FRED_SERIES:
        try:
            cols[s] = fred(s, start)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("FRED %s failed: %s", s, exc)
    for s in YAHOO_SYMBOLS:
        try:
            cols[s] = yahoo(s, start)["close"]
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Yahoo %s failed: %s", s, exc)
    return pd.DataFrame(cols).sort_index().ffill()
