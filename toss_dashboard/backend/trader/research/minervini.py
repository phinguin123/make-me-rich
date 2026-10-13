"""Minervini-style swing strategy (trend template + base breakout), daily bars.

Rules approximated from "Trade Like a Stock Market Wizard":
  Trend template: close > SMA50 > SMA150 > SMA200, SMA200 rising for a month,
  close >= 1.3x 52-week low and within 25% of the 52-week high, top relative strength.
  Entry: close breaks above the prior N-day high on volume >= 1.5x its 50-day average,
  after a tight base (prior N-day range small). Buy at the next open.
  Exit: fixed stop (default 7%), then a trailing rule; optional profit target.

    python -m trader.research.minervini download     # daily bars 2018 -> now (~20 min)
    python -m trader.research.minervini              # sweep
"""
from __future__ import annotations

import asyncio
import itertools
import math
import sys
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from ..alpaca import AlpacaData
from ..config import DATA_DIR
from .download import EXCHANGES, FUND_RE

RES = DATA_DIR / "research"
LONG = RES / "daily_long.parquet"
SEC_FEE = 0.0000206


async def download(start: str = "2018-01-01", end: str = "2026-10-06T23:59:00-04:00") -> None:
    async with AlpacaData() as api:
        assets = pd.DataFrame(await api.assets())
        assets = assets[assets["exchange"].isin(EXCHANGES) & ~assets["symbol"].str.contains(r"[^A-Z.]")]
        funds = set(assets.loc[assets["name"].fillna("").str.contains(FUND_RE), "symbol"])
        symbols = sorted(set(assets["symbol"]) - funds) + ["SPY"]
        sem = asyncio.Semaphore(12)

        async def one(batch):
            async with sem:
                return await api.bars(batch, "1Day", start, end)

        batches = [symbols[i : i + 100] for i in range(0, len(symbols), 100)]
        frames = []
        for i, fut in enumerate(asyncio.as_completed([one(b) for b in batches])):
            frames.append(await fut)
            if i % 20 == 0:
                print(f"daily {i + 1}/{len(batches)}", flush=True)
    d = pd.concat(frames, ignore_index=True)
    d["date"] = d["ts"].dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
    d.drop(columns=["ts"]).sort_values(["symbol", "date"]).to_parquet(LONG)
    print(f"saved {len(d):,} bars for {d['symbol'].nunique():,} symbols", flush=True)


def features(min_dollar_vol: float = 20e6) -> pd.DataFrame:
    d = pd.read_parquet(LONG, columns=["symbol", "date", "open", "high", "low", "close", "volume"])
    d = d.sort_values(["symbol", "date"]).reset_index(drop=True)
    # Keep only symbols that were ever liquid enough to trade (plus SPY for the regime).
    dv = (d["close"] * d["volume"]).groupby(d["symbol"]).transform(lambda s: s.rolling(50, min_periods=20).mean())
    liquid = set(d.loc[dv >= min_dollar_vol, "symbol"]) | {"SPY"}
    d = d[d["symbol"].isin(liquid)].reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        d[col] = d[col].astype("float32")
    g = d.groupby("symbol", sort=False)
    c = g["close"]
    roll = lambda s, n, f="mean", mp=None: getattr(s.rolling(n, min_periods=mp or n), f)()  # noqa: E731
    d["sma10"] = c.transform(lambda s: roll(s, 10))
    d["sma20"] = c.transform(lambda s: roll(s, 20))
    d["sma50"] = c.transform(lambda s: roll(s, 50))
    d["sma150"] = c.transform(lambda s: roll(s, 150))
    d["sma200"] = c.transform(lambda s: roll(s, 200))
    d["sma200_1m"] = g["sma200"].shift(21)
    d["hi52"] = g["high"].transform(lambda s: roll(s, 252, "max", 200))
    d["lo52"] = g["low"].transform(lambda s: roll(s, 252, "min", 200))
    d["vol50"] = g["volume"].transform(lambda s: roll(s, 50).shift(1))
    d["dollar_vol50"] = d["vol50"] * d["close"]
    for n in (63, 126, 189, 252):
        d[f"r{n}"] = c.pct_change(n)
    d["rs_raw"] = 0.4 * d["r63"] + 0.2 * d["r126"] + 0.2 * d["r189"] + 0.2 * d["r252"]
    d["rs"] = d.groupby("date")["rs_raw"].rank(pct=True) * 100
    for n in (15, 25):
        d[f"hh{n}"] = g["high"].transform(lambda s, n=n: s.rolling(n).max().shift(1))  # prior N-day high (pivot)
        d[f"ll{n}"] = g["low"].transform(lambda s, n=n: s.rolling(n).min().shift(1))
    d["next_open"] = g["open"].shift(-1)
    return d


@dataclass(frozen=True)
class P:
    base: int = 25  # pivot = highest high of the prior `base` days
    max_base_depth: float = 0.15  # base range (high-low)/high must be tighter than this
    min_rs: float = 80.0
    vol_mult: float = 1.5
    stop: float = 0.07
    target: float = 0.0  # 0 = no fixed target
    trail: str = "sma20"  # sma10 | sma20 | sma50 | none
    trail_after: float = 0.10  # trailing rule activates once the trade is up this much
    breakeven_at: float = 0.10  # move the stop to entry once up this much (0 = off)
    max_hold: int = 60
    entry: str = "next_open"  # next_open | close (closing auction on the breakout day)
    regime: str = "spy_sma50"  # none | spy_sma50 | spy_sma200
    slots: int = 6
    risk: float = 0.01
    max_pos: float = 0.25
    min_price: float = 10.0
    min_dollar_vol: float = 20e6
    commission: float = 0.001
    slip: float = 0.001
    capital: float = 5000.0


def signals(d: pd.DataFrame, p: P) -> pd.DataFrame:
    tt = (
        (d["close"] > d["sma50"]) & (d["sma50"] > d["sma150"]) & (d["sma150"] > d["sma200"])
        & (d["sma200"] > d["sma200_1m"]) & (d["close"] >= 1.3 * d["lo52"]) & (d["close"] >= 0.75 * d["hi52"])
        & (d["rs"] >= p.min_rs) & (d["close"] >= p.min_price) & (d["dollar_vol50"] >= p.min_dollar_vol)
    )
    hh, ll = d[f"hh{p.base}"], d[f"ll{p.base}"]
    tight = (hh - ll) / hh <= p.max_base_depth
    brk = (d["close"] > hh) & (d["volume"] >= p.vol_mult * d["vol50"])
    return d[tt & tight & brk & d["next_open"].notna()]


def backtest(d: pd.DataFrame, p: P, start: str = "2019-01-01") -> tuple[pd.DataFrame, pd.Series]:
    d = d[d["date"] >= pd.Timestamp(start) - pd.Timedelta(days=5)]
    spy = d[d["symbol"] == "SPY"].set_index("date")
    spy_ok = (spy["close"] > spy["sma50"]) if p.regime == "spy_sma50" else (spy["close"] > spy["sma200"]) if p.regime == "spy_sma200" else spy["close"] > 0
    sig = signals(d, p)
    sig = sig[sig["date"].map(spy_ok).fillna(False).astype(bool)]
    sig_by_day = {k: v.sort_values("rs", ascending=False) for k, v in sig.groupby("date")}
    px = {k: v.set_index("symbol") for k, v in d[d["date"] >= start].groupby("date")}
    days = sorted(px)
    cash, pos, trades, curve = p.capital, {}, [], {}
    pending: list = []
    equity = p.capital
    for day in days:
        bars = px[day]
        # fills for yesterday's signals at today's open, sized on yesterday's closing equity
        for row in pending:
            s = row.symbol
            if s in pos or s not in bars.index or len(pos) >= p.slots:
                continue
            entry = bars.loc[s, "open"] * (1 + p.slip)
            if entry > row.close * 1.05:  # gapped too far above the breakout
                continue
            shares = math.floor(min(p.risk * equity / (entry * p.stop), p.max_pos * equity / entry, cash / (entry * (1 + p.commission))))
            if shares < 1:
                continue
            cost = shares * entry * (1 + p.commission)
            cash -= cost
            pos[s] = {"entry": entry, "shares": shares, "cost": cost, "stop": entry * (1 - p.stop), "date": day, "held": 0, "peak": entry, "last": entry, "missing": 0}
        pending = []
        # manage open positions on today's bar
        for s in list(pos):
            x = pos[s]
            if s not in bars.index:
                x["missing"] += 1
                if x["missing"] >= 5:  # delisted / halted: assume exit at the last traded price
                    proceeds = x["shares"] * x["last"] * (1 - p.slip) * (1 - p.commission)
                    cash += proceeds
                    trades.append({"symbol": s, "entry_date": x["date"], "exit_date": day, "entry": x["entry"], "exit": x["last"],
                                   "ret": proceeds / x["cost"] - 1, "pnl": proceeds - x["cost"], "held": x["held"], "reason": "delisted"})
                    del pos[s]
                continue
            b = bars.loc[s]
            x["missing"] = 0
            x["held"] += 1
            exit_px = reason = None
            if b["open"] <= x["stop"]:
                exit_px, reason = b["open"] * (1 - p.slip), "gap_stop"
            elif b["low"] <= x["stop"]:
                exit_px, reason = x["stop"] * (1 - p.slip), "stop"
            elif p.target and b["high"] >= x["entry"] * (1 + p.target):
                exit_px, reason = max(b["open"], x["entry"] * (1 + p.target)), "target"
            else:
                x["peak"] = max(x["peak"], b["high"])
                gain = b["close"] / x["entry"] - 1
                if p.breakeven_at and x["peak"] >= x["entry"] * (1 + p.breakeven_at):
                    x["stop"] = max(x["stop"], x["entry"])
                if p.trail != "none" and x["peak"] >= x["entry"] * (1 + p.trail_after) and b["close"] < b[p.trail]:
                    exit_px, reason = b["close"] * (1 - p.slip), p.trail
                elif x["held"] >= p.max_hold:
                    exit_px, reason = b["close"] * (1 - p.slip), "time"
            if exit_px is not None:
                proceeds = x["shares"] * exit_px * (1 - p.commission - SEC_FEE)
                cash += proceeds
                trades.append({"symbol": s, "entry_date": x["date"], "exit_date": day, "entry": x["entry"], "exit": exit_px,
                               "ret": proceeds / x["cost"] - 1, "pnl": proceeds - x["cost"], "held": x["held"], "reason": reason})
                del pos[s]
        if day in sig_by_day:
            if p.entry == "close":
                for row in sig_by_day[day].itertuples(index=False):
                    s = row.symbol
                    if s in pos or len(pos) >= p.slots:
                        continue
                    entry = row.close * (1 + p.slip)
                    shares = math.floor(min(p.risk * equity / (entry * p.stop), p.max_pos * equity / entry, cash / (entry * (1 + p.commission))))
                    if shares < 1:
                        continue
                    cost = shares * entry * (1 + p.commission)
                    cash -= cost
                    pos[s] = {"entry": entry, "shares": shares, "cost": cost, "stop": entry * (1 - p.stop), "date": day, "held": 0,
                              "peak": entry, "last": entry, "missing": 0}
            else:
                pending = list(sig_by_day[day].itertuples(index=False))
        for s, x in pos.items():
            if s in bars.index:
                x["last"] = float(bars.loc[s, "close"])
        equity = cash + sum(x["shares"] * x["last"] for x in pos.values())
        curve[day] = equity
    return pd.DataFrame(trades), pd.Series(curve)


def summary(p: P, t: pd.DataFrame, eq: pd.Series) -> dict:
    if t.empty:
        return {"trades": 0}
    r = eq.pct_change().fillna(eq.iloc[0] / p.capital - 1)
    yearly = eq.resample("YE").last()
    yearly = (yearly / yearly.shift(1).fillna(p.capital) - 1).round(3)
    wins, losses = t[t["ret"] > 0], t[t["ret"] <= 0]
    return {
        "trades": len(t), "per_month": len(t) / (len(eq) / 21),
        "cagr": (eq.iloc[-1] / p.capital) ** (252 / len(eq)) - 1,
        "sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() else 0.0,
        "max_dd": (eq / eq.cummax() - 1).min(),
        "win": len(wins) / len(t), "avg_win": wins["ret"].mean() if len(wins) else 0.0,
        "avg_loss": losses["ret"].mean() if len(losses) else 0.0, "avg_hold": t["held"].mean(),
        **{f"y{k.year}": v for k, v in yearly.items()},
    }


if __name__ == "__main__":
    if sys.argv[1:] == ["download"]:
        asyncio.run(download())
        sys.exit()
    d = features()
    rows = []
    grid = {
        "stop": [0.05, 0.07, 0.08],
        "trail": ["sma10", "sma20", "sma50"],
        "target": [0.0, 0.20],
        "regime": ["none", "spy_sma50", "spy_sma200"],
        "min_rs": [80.0, 90.0],
    }
    keys = list(grid)
    for combo in itertools.product(*grid.values()):
        p = replace(P(), **dict(zip(keys, combo)))
        t, eq = backtest(d, p)
        rows.append({**dict(zip(keys, combo)), **summary(p, t, eq)})
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in rows[-1].items()}, flush=True)
    pd.DataFrame(rows).to_csv(RES / "sweep_minervini.csv", index=False)
