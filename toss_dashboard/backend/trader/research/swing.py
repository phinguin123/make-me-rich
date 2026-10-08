"""Short-term mean-reversion swing strategy (daily bars), portfolio-level backtest.

Signal (evaluated near the close): liquid stock in a long-term uptrend that just
had a sharp short-term selloff (RSI(2) very low). Buy at the close, exit on the
first close back above the 5-day average or after `max_hold` days.

    python -m trader.research.swing
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, replace
from datetime import date

import numpy as np
import pandas as pd

from ..config import DATA_DIR

RES = DATA_DIR / "research"
SPLIT = pd.Timestamp("2025-07-01")
SEC_FEE = 0.0000206


@dataclass(frozen=True)
class SwingParams:
    rsi_max: float = 10.0
    trend: str = "sma200"  # sma200 | sma100 | none
    min_dollar_adv: float = 100e6
    min_price: float = 10.0
    exit_rule: str = "sma5"  # sma5 | hold3 | first_up
    max_hold: int = 10
    slots: int = 5
    max_new_per_day: int = 5
    rank_by: str = "rsi"  # rsi | ret3
    regime: str = "none"  # none | spy_sma200
    stop_atr: float = 0.0  # 0 = no stop (mean reversion usually works best without)
    commission: float = 0.001
    slip: float = 0.0005
    capital: float = 3000.0


def load_daily() -> pd.DataFrame:
    cols = ["symbol", "date", "open", "high", "low", "close", "volume", "atr14", "adv14", "is_fund"]
    f = pd.read_parquet(RES / "features.parquet", columns=cols)
    f["date"] = pd.to_datetime(f["date"])
    spy = f[f["symbol"] == "SPY"][["date", "close"]].rename(columns={"close": "spy"})
    f = f[~f["is_fund"]].sort_values(["symbol", "date"]).reset_index(drop=True)
    g = f.groupby("symbol", sort=False)["close"]
    f["sma5"] = g.transform(lambda s: s.rolling(5).mean())
    f["sma100"] = g.transform(lambda s: s.rolling(100, min_periods=90).mean())
    f["sma200"] = g.transform(lambda s: s.rolling(200, min_periods=180).mean())
    delta = g.diff()
    f["_up"], f["_dn"] = delta.clip(lower=0), (-delta).clip(lower=0)
    gg = f.groupby("symbol", sort=False)
    au = gg["_up"].transform(lambda s: s.ewm(alpha=0.5, adjust=False).mean())
    ad = gg["_dn"].transform(lambda s: s.ewm(alpha=0.5, adjust=False).mean())
    f["rsi2"] = 100 - 100 / (1 + au / ad.replace(0, np.nan))
    f["ret3"] = g.pct_change(3)
    f["dollar_adv"] = f["adv14"] * f["close"]
    spy["spy_sma200"] = spy["spy"].rolling(200, min_periods=180).mean()
    f = f.merge(spy, on="date", how="left")
    return f.drop(columns=["_up", "_dn"])


def index_by_day(f: pd.DataFrame, start: str = "2024-01-01") -> dict:
    """Restrict to symbols that were ever liquid, then split into per-day frames."""
    liquid = f.loc[f["dollar_adv"] >= 50e6, "symbol"].unique()
    f = f[(f["date"] >= start) & f["symbol"].isin(liquid)]
    return {d: x.set_index("symbol") for d, x in f.groupby("date")}


def backtest(p: SwingParams, by_day: dict) -> tuple[pd.DataFrame, pd.Series]:
    days = sorted(by_day)
    cash = p.capital
    pos: dict[str, dict] = {}
    trades = []
    equity_curve = {}
    for d in days:
        today = by_day[d]
        # exits at today's close
        for sym in list(pos):
            if sym not in today.index:
                continue
            row = today.loc[sym]
            ps = pos[sym]
            ps["held"] += 1
            exit_px = None
            reason = ""
            if p.stop_atr and row["low"] <= ps["stop"]:
                exit_px, reason = min(row["open"], ps["stop"]) * (1 - p.slip), "stop"
            elif p.exit_rule == "sma5" and row["close"] > row["sma5"]:
                exit_px, reason = row["close"] * (1 - p.slip), "sma5"
            elif p.exit_rule == "first_up" and row["close"] > ps["entry"]:
                exit_px, reason = row["close"] * (1 - p.slip), "first_up"
            elif p.exit_rule == "hold3" and ps["held"] >= 3:
                exit_px, reason = row["close"] * (1 - p.slip), "hold3"
            if exit_px is None and ps["held"] >= p.max_hold:
                exit_px, reason = row["close"] * (1 - p.slip), "max_hold"
            if exit_px is not None:
                proceeds = ps["shares"] * exit_px * (1 - p.commission - SEC_FEE)
                cash += proceeds
                trades.append({"symbol": sym, "entry_date": ps["date"], "exit_date": d, "entry": ps["entry"], "exit": exit_px,
                               "shares": ps["shares"], "pnl": proceeds - ps["cost"], "ret": proceeds / ps["cost"] - 1, "held": ps["held"], "reason": reason})
                del pos[sym]
        # entries at today's close
        free = p.slots - len(pos)
        regime_ok = p.regime == "none" or (today["spy"].iloc[0] > today["spy_sma200"].iloc[0])
        if free > 0 and regime_ok:
            c = today
            mask = (c["dollar_adv"] >= p.min_dollar_adv) & (c["close"] >= p.min_price) & (c["rsi2"] <= p.rsi_max)
            if p.trend != "none":
                mask &= c["close"] > c[p.trend]
            cands = c[mask & ~c.index.isin(list(pos))]
            cands = cands.sort_values("rsi2" if p.rank_by == "rsi" else "ret3")
            equity = cash + sum(x["shares"] * by_day[d].loc[s, "close"] if s in by_day[d].index else x["cost"] for s, x in pos.items())
            for sym, row in cands.head(min(free, p.max_new_per_day)).iterrows():
                px = row["close"] * (1 + p.slip)
                budget = min(equity / p.slots, cash)
                shares = math.floor(budget / (px * (1 + p.commission)))
                if shares < 1:
                    continue
                cost = shares * px * (1 + p.commission)
                cash -= cost
                pos[sym] = {"date": d, "entry": px, "shares": shares, "cost": cost, "held": 0, "stop": px - p.stop_atr * row["atr14"]}
        mtm = cash + sum(x["shares"] * (by_day[d].loc[s, "close"] if s in by_day[d].index else x["entry"]) for s, x in pos.items())
        equity_curve[d] = mtm
    return pd.DataFrame(trades), pd.Series(equity_curve)


def summarize(p: SwingParams, trades: pd.DataFrame, eq: pd.Series) -> dict:
    if trades.empty:
        return {"trades": 0}
    r = eq.pct_change().fillna(eq.iloc[0] / p.capital - 1)
    years = len(eq) / 252

    def sh(x):
        return float(x.mean() / x.std() * np.sqrt(252)) if x.std() > 0 else 0.0

    is_ = r.index < SPLIT
    eq_is, eq_oos = eq[is_], eq[~is_]
    return {
        "trades": len(trades),
        "per_week": len(trades) / (len(eq) / 5),
        "final": float(eq.iloc[-1]),
        "cagr": float((eq.iloc[-1] / p.capital) ** (1 / years) - 1),
        "sharpe": sh(r),
        "sharpe_is": sh(r[is_]),
        "sharpe_oos": sh(r[~is_]),
        "ret_is": float(eq_is.iloc[-1] / p.capital - 1),
        "ret_oos": float(eq_oos.iloc[-1] / eq_is.iloc[-1] - 1),
        "max_dd": float((eq / eq.cummax() - 1).min()),
        "win": float((trades["pnl"] > 0).mean()),
        "avg_ret": float(trades["ret"].mean()),
        "avg_hold": float(trades["held"].mean()),
    }


if __name__ == "__main__":
    by_day = index_by_day(load_daily())
    base = SwingParams()
    grid = {
        "rsi_max": [5.0, 10.0, 15.0],
        "trend": ["sma200", "none"],
        "exit_rule": ["sma5", "first_up", "hold3"],
        "slots": [3, 5, 8],
        "regime": ["none", "spy_sma200"],
    }
    rows = []
    keys = list(grid)
    for combo in itertools.product(*(grid[k] for k in keys)):
        p = replace(base, **dict(zip(keys, combo)))
        t, eq = backtest(p, by_day)
        rows.append({**dict(zip(keys, combo)), **summarize(p, t, eq)})
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in rows[-1].items()}, flush=True)
    res = pd.DataFrame(rows)
    res.to_csv(RES / "sweep_swing.csv", index=False)
    print(res.sort_values("sharpe_is", ascending=False).head(20).to_string())
