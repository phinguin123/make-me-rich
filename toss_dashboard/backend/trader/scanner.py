"""Live "stocks in play" scanner.

Selection mirrors the backtest: liquid common stocks (prev close > $5, 14-day ADV
>= 1M shares, ATR14 >= $0.50, no funds/leveraged products) ranked by the relative
volume of the first 5-minute bar versus its own 14-session average.

Opening volume comes from Alpaca's free real-time IEX feed, compared against an
IEX baseline so the ratio is like-for-like. If Alpaca is unavailable, Toss's
realtime rankings provide the candidate pool instead.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .alpaca import AlpacaData
from .config import DATA_DIR
from .toss.client import TossAPIError, TossClient

LOG = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
LIVE_DIR = DATA_DIR / "live"
EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
FUND_RE = re.compile(
    r"\b(?:ETF|ETN|ETP|Fund|Trust|iShares|SPDR|ProShares|Direxion|Invesco|Vanguard|Leveraged|Inverse|"
    r"Ultra|UltraPro|Bull|Bear|2X|3X|-1X|Daily|Portfolio|Index|Notes|Warrant|Rights?|Units?)\b",
    re.IGNORECASE,
)
STOCK_TYPES = {"STOCK", "FOREIGN_STOCK", "DEPOSITARY_RECEIPT"}


@dataclass
class Candidate:
    symbol: str
    rvol: float
    atr14: float
    prev_close: float
    adv14: float
    or_volume: float
    sma50: float = 0.0
    rank: int = 0

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def et_iso(d: date, hh: int, mm: int, ss: int = 0) -> str:
    return datetime.combine(d, time(hh, mm, ss), ET).isoformat()


class Scanner:
    def __init__(self, toss: TossClient, top_n: int = 20):
        self.toss = toss
        self.top_n = top_n
        self.universe: pd.DataFrame | None = None  # symbol, prev_close, atr14, adv14, or_base
        self.prepared_for: date | None = None
        self.candidates: list[Candidate] = []

    async def prepare(self, day: date) -> None:
        """Daily stats + IEX opening-volume baselines. Run before the open."""
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        cache = LIVE_DIR / f"universe_{day.isoformat()}.parquet"
        if cache.exists():
            self.universe = pd.read_parquet(cache)
            self.prepared_for = day
            LOG.info("Universe loaded from cache: %d symbols", len(self.universe))
            return
        async with AlpacaData() as api:
            assets = pd.DataFrame(await api.assets())
            assets = assets[(assets["status"] == "active") & assets["tradable"] & assets["exchange"].isin(EXCHANGES)]
            assets = assets[~assets["symbol"].str.contains(r"[^A-Z.]") & ~assets["name"].fillna("").str.contains(FUND_RE)]
            symbols = sorted(assets["symbol"])
            start = (day - timedelta(days=100)).isoformat()  # enough for a 50-day average
            end = et_iso(day - timedelta(days=1), 23, 59)
            frames = []
            for i in range(0, len(symbols), 200):
                frames.append(await api.bars(symbols[i : i + 200], "1Day", start, end))
            daily = pd.concat(frames, ignore_index=True)
            daily["date"] = daily["ts"].dt.tz_convert(ET).dt.date
            daily = daily[daily["date"] < day].sort_values(["symbol", "date"])
            stats = daily.groupby("symbol").apply(_daily_stats, include_groups=False).dropna()
            uni = stats[(stats["prev_close"] > 5) & (stats["adv14"] >= 1_000_000) & (stats["atr14"] >= 0.5)].copy()
            sessions = sorted(daily.loc[daily["symbol"] == "AAPL", "date"])[-14:]
            base = []
            syms = list(uni.index)
            for d in sessions:
                for i in range(0, len(syms), 700):
                    b = await api.bars(syms[i : i + 700], "5Min", et_iso(d, 9, 30), et_iso(d, 9, 34), feed="iex")
                    base.append(b[["symbol", "volume"]])
            if base:
                b = pd.concat(base)
                uni["or_base"] = b.groupby("symbol")["volume"].mean()
                uni["or_days"] = b.groupby("symbol")["volume"].count()
            uni = uni[(uni.get("or_days", 0) >= 5)].reset_index()
        uni.to_parquet(cache)
        self.universe = uni
        self.prepared_for = day
        LOG.info("Universe prepared: %d liquid stocks with IEX baselines", len(uni))

    async def scan(self, day: date) -> list[Candidate]:
        cands: list[Candidate] = []
        try:
            cands = await self._scan_iex(day)
        except Exception as exc:  # noqa: BLE001
            LOG.error("IEX scan failed (%s); falling back to Toss rankings", exc)
        if not cands:
            cands = await self._scan_toss()
        cands = await self._verify_with_toss(cands)
        for i, c in enumerate(cands, 1):
            c.rank = i
        self.candidates = cands[: self.top_n]
        LOG.info("Stocks in play: %s", ", ".join(f"{c.symbol}({c.rvol:.1f}x)" for c in self.candidates))
        return self.candidates

    async def _scan_iex(self, day: date) -> list[Candidate]:
        if self.universe is None or self.universe.empty:
            return []
        uni = self.universe.set_index("symbol")
        syms = list(uni.index)
        frames = []
        async with AlpacaData() as api:
            for i in range(0, len(syms), 700):
                frames.append(await api.bars(syms[i : i + 700], "5Min", et_iso(day, 9, 30), et_iso(day, 9, 34), feed="iex"))
        bars = pd.concat(frames, ignore_index=True)
        if bars.empty:
            return []
        bars = bars.set_index("symbol")
        out = []
        for sym, b in bars.iterrows():
            u = uni.loc[sym]
            if b["open"] <= 5 or not u["or_base"]:
                continue
            rvol = float(b["volume"] / u["or_base"])
            if rvol >= 1.0:
                out.append(Candidate(sym, rvol, float(u["atr14"]), float(u["prev_close"]), float(u["adv14"]), float(b["volume"]), float(u["sma50"])))
        out.sort(key=lambda c: c.rvol, reverse=True)
        return out[: self.top_n * 2]

    async def _scan_toss(self) -> list[Candidate]:
        """Fallback pool: most-traded + biggest movers right now, ranked by volume vs ADV."""
        pool: dict[str, dict] = {}
        for kind, dur in (("MARKET_TRADING_VOLUME", "realtime"), ("MARKET_TRADING_AMOUNT", "realtime"), ("TOP_GAINERS", "1d")):
            try:
                res = await self.toss.rankings(kind, dur, 100)
                for it in res.get("rankings", []):
                    pool.setdefault(it["symbol"], it)
            except TossAPIError as exc:
                LOG.warning("rankings %s failed: %s", kind, exc)
        uni = self.universe.set_index("symbol") if self.universe is not None else pd.DataFrame()
        out = []
        for sym, it in pool.items():
            if sym not in uni.index:
                continue
            u = uni.loc[sym]
            vol = float(it["tradingVolume"])
            rvol = vol / (u["adv14"] * 0.05)  # first ~5 min is roughly 5% of daily volume
            out.append(Candidate(sym, rvol, float(u["atr14"]), float(u["prev_close"]), float(u["adv14"]), vol, float(u["sma50"])))
        out.sort(key=lambda c: c.rvol, reverse=True)
        return out[: self.top_n * 2]

    async def _verify_with_toss(self, cands: list[Candidate]) -> list[Candidate]:
        if not cands:
            return cands
        info: dict[str, dict] = {}
        syms = [c.symbol for c in cands]
        for i in range(0, len(syms), 50):
            try:
                for s in await self.toss.stocks(syms[i : i + 50]):
                    info[s["symbol"]] = s
            except TossAPIError as exc:
                LOG.warning("Toss stock master lookup failed: %s", exc)
                return cands
        keep = []
        for c in cands:
            s = info.get(c.symbol)
            if not s or s.get("status") != "ACTIVE" or s.get("securityType") not in STOCK_TYPES or s.get("leverageFactor"):
                continue
            keep.append(c)
        return keep


def _daily_stats(g: pd.DataFrame) -> pd.Series:
    sma50 = float(g["close"].tail(50).mean()) if len(g) >= 30 else np.nan
    g = g.tail(20)
    if len(g) < 10:
        return pd.Series({"prev_close": np.nan, "atr14": np.nan, "adv14": np.nan, "sma50": np.nan})
    prev = g["close"].shift(1)
    tr = np.maximum(g["high"], prev) - np.minimum(g["low"], prev)
    tr = tr.fillna(g["high"] - g["low"])
    return pd.Series({
        "prev_close": float(g["close"].iloc[-1]),
        "atr14": float(tr.tail(14).mean()),
        "adv14": float(g["volume"].tail(14).mean()),
        "sma50": sma50,
    })
