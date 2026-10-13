"""Swing / position portfolio backtester on daily bars (weeks-to-months holding).

Signals use the close of day t, orders fill at the open of day t+1 with Toss
commission (0.1%/side), SEC fee and slippage. Fractional shares are allowed
(Toss supports amount-based US market buys and fractional market sells).

    python -m trader.research.portfolio
"""
from __future__ import annotations

import itertools
from dataclasses import asdict, dataclass, replace

import numpy as np
import pandas as pd

from ..config import DATA_DIR

RES = DATA_DIR / "research"
SEC_FEE = 0.0000206
SPLIT = pd.Timestamp("2023-01-01")  # tune on 2019-2022 (incl. the 2022 bear), test on 2023-2026


@dataclass(frozen=True)
class PP:
    score: str = "rs"  # mom12_1 | mom6_1 | rs | mom_vol
    top_n: int = 10
    buffer: float = 2.0  # keep a holding while its rank <= buffer * top_n
    rebalance: str = "M"  # M | W
    trend_filter: str = "sma200"  # stock must be above this average to be bought (none to disable)
    near_high: float = 0.0  # require close >= near_high * 52-week high (0 = off)
    exit_rule: str = "none"  # none | sma100 | sma200 | trail20 | trail30
    regime: str = "none"  # none | spy_sma200 | spy_sma100
    regime_off_exposure: float = 0.0  # fraction of equity kept in stocks while regime is off
    hedge: str = "none"  # none | soxs | soxs_broad
    hedge_weight: float = 0.15
    vol_target: float = 0.20  # scale stock exposure to this annualized volatility (0 = off)
    vol_mode: str = "realized"  # realized (strategy history) | exante (current basket's trailing 63-day vol)
    min_price: float = 10.0
    min_dollar_vol: float = 30e6
    universe_skip: int = 0  # exclude the N most-traded names (test smaller-cap universes)
    universe_top: int = 1000  # only the N most-traded stocks by 50-day dollar volume (large/liquid names)
    max_vol: float = 0.6  # skip names with annualized 63-day volatility above this (0 = off)
    max_mom: float = 0.0  # skip names up more than this over 12-1 months (0 = off; avoids blow-off tops)
    commission: float = 0.001
    slip: float = 0.001
    capital: float = 5000.0
    start: str = "2019-01-01"


def load() -> dict[str, pd.DataFrame]:
    st = pd.read_parquet(RES / "daily_long.parquet", columns=["symbol", "date", "open", "high", "close", "volume"])
    etf = pd.read_parquet(RES / "etf_daily.parquet", columns=["symbol", "date", "open", "high", "close", "volume"])
    return build(st, etf, "2016-01-01")


def build(st: pd.DataFrame, etf: pd.DataFrame, start: str) -> dict:
    """Wide feature matrices from long daily bars (stocks + ETFs). Shared by research and live."""
    st = st[~st["symbol"].isin(etf["symbol"].unique())]
    d = pd.concat([st, etf], ignore_index=True)
    d = d[d["date"] >= start]
    wide = {k: d.pivot(index="date", columns="symbol", values=k).astype("float32") for k in ("open", "high", "close", "volume")}
    days = wide["close"].index
    etf_syms = set(etf["symbol"].unique())
    c = wide["close"]
    out = dict(wide)
    out["dv50"] = (c * wide["volume"]).rolling(50, min_periods=20).mean()
    for n in (20, 50, 100, 200):
        out[f"sma{n}"] = c.rolling(n, min_periods=int(n * 0.8)).mean()
    out["hi52"] = wide["high"].rolling(252, min_periods=200).max()
    out["mom12_1"] = c.shift(21) / c.shift(252) - 1
    out["mom6_1"] = c.shift(21) / c.shift(126) - 1
    r = c.pct_change(fill_method=None)
    out["vol63"] = r.rolling(63, min_periods=40).std() * np.sqrt(252)
    out["rs"] = 0.4 * c.pct_change(63, fill_method=None) + 0.2 * c.pct_change(126, fill_method=None) + 0.2 * c.pct_change(189, fill_method=None) + 0.2 * c.pct_change(252, fill_method=None)
    out["mom_vol"] = out["mom12_1"] / out["vol63"]
    out["is_stock"] = pd.Series([s not in etf_syms for s in c.columns], index=c.columns)
    out["days"] = days
    return out


def _rebalance_days(days: pd.DatetimeIndex, freq: str, final_complete: bool) -> set:
    """Last trading day of each period (signal at its close). The final period in the data
    counts only when the caller knows it is complete (live: today is already a new month)."""
    s = pd.Series(days, index=days)
    key = s.dt.to_period("M") if freq == "M" else s.dt.to_period("W")
    out = set(s.groupby(key).max())
    if not final_complete:
        out.discard(days[-1])
    return out


def backtest(w: dict, p: PP, state: dict | None = None, final_complete: bool = False) -> tuple[pd.Series, pd.DataFrame]:
    """Run the strategy. When `state` is a dict it receives the model's final book:
    weights at the last close, pending rebalance targets, exposure and hedge flag."""
    days = w["days"][w["days"] >= pd.Timestamp(p.start) - pd.Timedelta(days=7)]
    cols = w["close"].columns
    ix = {s: i for i, s in enumerate(cols)}
    C = w["close"].loc[days].to_numpy()
    O = w["open"].loc[days].to_numpy()
    score = w[p.score].loc[days].to_numpy()
    elig = (w["close"].loc[days] >= p.min_price) & (w["dv50"].loc[days] >= p.min_dollar_vol)
    elig &= np.broadcast_to(w["is_stock"].to_numpy(), elig.shape)
    if p.universe_top:
        dv = w["dv50"].loc[days].where(np.broadcast_to(w["is_stock"].to_numpy(), elig.shape))
        rk = dv.rank(axis=1, ascending=False)
        elig &= (rk <= p.universe_top) & (rk > p.universe_skip)
    if p.max_vol:
        elig &= w["vol63"].loc[days] <= p.max_vol
    if p.max_mom:
        elig &= w["mom12_1"].loc[days] <= p.max_mom
    if p.trend_filter != "none":
        elig &= w["close"].loc[days] > w[p.trend_filter].loc[days]
    if p.near_high:
        elig &= w["close"].loc[days] >= p.near_high * w["hi52"].loc[days]
    elig = elig.to_numpy() & np.isfinite(score)
    exit_ma = w[p.exit_rule].loc[days].to_numpy() if p.exit_rule in ("sma100", "sma200") else None
    spy = w["close"]["SPY"].loc[days].to_numpy()
    spy_ma = (w["sma200"] if p.regime in ("none", "spy_sma200") else w["sma100"])["SPY"].loc[days].to_numpy()
    smh = w["close"]["SMH"].loc[days].to_numpy()
    smh50 = w["sma50"]["SMH"].loc[days].to_numpy()
    smh20 = w["sma20"]["SMH"].loc[days].to_numpy()
    spy50 = w["sma50"]["SPY"].loc[days].to_numpy()
    R = w["close"].loc[days].pct_change(fill_method=None).to_numpy()
    exposure = 1.0
    eq_hist: list[float] = []
    soxs = ix.get("SOXS")
    reb = _rebalance_days(days, p.rebalance, final_complete)

    cash = p.capital
    shares: dict[int, float] = {}
    peak: dict[int, float] = {}
    last_px: dict[int, float] = {}
    stale: dict[int, int] = {}

    def px_or_last(arr, i):
        v = arr[i]
        return float(v) if np.isfinite(v) else last_px.get(i, 0.0)
    pending_targets: set[int] | None = None
    pending_exits: set[int] = set()
    hedge_on = False
    pending_hedge: bool | None = None
    curve, trades = [], []

    def sell(i, px, reason, d):
        nonlocal cash
        q = shares.pop(i)
        cash += q * px * (1 - p.slip) * (1 - p.commission - SEC_FEE)
        peak.pop(i, None)
        stale.pop(i, None)
        trades.append((d, cols[i], "SELL", reason))

    def buy(i, amount, px, d, reason):
        nonlocal cash
        amount = min(amount, cash)
        if amount <= 1 or not np.isfinite(px) or px <= 0:
            return
        fill = px * (1 + p.slip)
        q = amount / (fill * (1 + p.commission))
        cash -= amount
        shares[i] = shares.get(i, 0.0) + q
        peak[i] = max(peak.get(i, 0.0), fill)
        trades.append((d, cols[i], "BUY", reason))

    for t, d in enumerate(days):
        o = O[t]
        # 1) execute yesterday's decisions at today's open
        for i in list(pending_exits):
            if i in shares and np.isfinite(o[i]):
                sell(i, o[i], "exit", d)
        pending_exits = set()
        if pending_hedge is not None and soxs is not None:
            if not pending_hedge and soxs in shares and np.isfinite(o[soxs]):
                sell(soxs, o[soxs], "hedge_off", d)
            hedge_on = pending_hedge
            pending_hedge = None
        if pending_targets is not None:
            for i in [i for i in shares if i != soxs and i not in pending_targets]:
                if np.isfinite(o[i]):
                    sell(i, o[i], "rebalance", d)
            equity = cash + sum(q * px_or_last(o, i) for i, q in shares.items())
            stock_budget = equity * (1 - (p.hedge_weight if hedge_on else 0.0)) * exposure
            n = max(len(pending_targets), 1)
            per = stock_budget / max(n, p.top_n)
            for i in list(pending_targets):
                if not np.isfinite(o[i]) or i not in shares:
                    continue
                have = shares[i] * o[i]
                if have > per * 1.3:  # trim when well over target (vol targeting de-risk)
                    q = shares[i] * (1 - per / have)
                    shares[i] -= q
                    cash += q * o[i] * (1 - p.slip) * (1 - p.commission - SEC_FEE)
            for i in pending_targets:
                if not np.isfinite(o[i]):
                    continue
                have = shares.get(i, 0.0) * o[i]
                if have < per * 0.7:  # top up only when well under target (limits turnover)
                    buy(i, per - have, o[i], d, "rebalance")
            pending_targets = None
        if hedge_on and soxs is not None and soxs not in shares and np.isfinite(o[soxs]):
            equity = cash + sum(q * px_or_last(o, i) for i, q in shares.items())
            if cash < p.hedge_weight * equity:  # free cash for the hedge by trimming the largest holding
                big = max((i for i in shares if i != soxs), key=lambda i: shares[i] * o[i] if np.isfinite(o[i]) else 0, default=None)
                if big is not None:
                    sell(big, o[big], "fund_hedge", d)
            buy(soxs, p.hedge_weight * equity, o[soxs], d, "hedge_on")
        # 2) end-of-day: mark, exits, regime, rebalance signals
        c = C[t]
        for i in list(shares):
            if np.isfinite(c[i]):
                peak[i] = max(peak.get(i, 0.0), c[i])
                last_px[i] = float(c[i])
                stale[i] = 0
            else:
                stale[i] = stale.get(i, 0) + 1
                if stale[i] >= 5:  # delisted / halted: exit at the last traded price
                    sell(i, last_px.get(i, 0.0), "delisted", d)
        equity = cash + sum(q * px_or_last(c, i) for i, q in shares.items())
        if d >= pd.Timestamp(p.start):
            curve.append((d, equity))
        eq_hist.append(equity)
        held_now = [i for i in shares if i != soxs]
        if p.vol_target and p.vol_mode == "exante" and held_now and t > 63:
            with np.errstate(all="ignore"):
                basket = np.nanmean(R[t - 62 : t + 1][:, held_now], axis=1)
            realized = float(np.nanstd(basket) * np.sqrt(252))
        elif p.vol_target and len(eq_hist) > 25:
            rets = np.diff(np.array(eq_hist[-64:])) / np.array(eq_hist[-64:-1])
            # vol of the stock sleeve, not of the cash-diluted equity
            invested = max(1e-6, 1 - cash / equity) if equity > 0 else 1.0
            realized = float(np.std(rets) * np.sqrt(252)) / invested
        else:
            realized = 0.0
        if p.vol_target and realized > 0:
            target = float(np.clip(p.vol_target / realized, 0.25, 1.0))
            if abs(target - exposure) > 0.2:  # trade only meaningful changes
                exposure = target
                if d not in reb and held_now:  # resize current holdings at the next open
                    pending_targets = set(held_now)
        for i in list(shares):
            if i == soxs or not np.isfinite(c[i]):
                continue
            if exit_ma is not None and c[i] < exit_ma[t][i]:
                pending_exits.add(i)
            elif p.exit_rule == "trail20" and c[i] < 0.80 * peak[i]:
                pending_exits.add(i)
            elif p.exit_rule == "trail30" and c[i] < 0.70 * peak[i]:
                pending_exits.add(i)
        regime_on = p.regime == "none" or spy[t] > spy_ma[t]
        if p.hedge in ("soxs", "soxs_broad") and soxs is not None:
            want = bool(smh[t] < smh50[t] and t >= 5 and smh20[t] < smh20[t - 5])
            if p.hedge == "soxs_broad":  # semis AND the broad market breaking down together
                want = bool(want and spy[t] < spy50[t] and smh50[t] < (smh50[t - 10] if t >= 10 else smh50[t]))
            if want != hedge_on:
                pending_hedge = want
        if d in reb or (not regime_on and any(i != soxs for i in shares) and p.regime_off_exposure == 0):
            if not regime_on and p.regime_off_exposure == 0:
                pending_targets = set()
                continue
            sc = np.where(elig[t], score[t], -np.inf)
            order = np.argsort(-sc)
            ranked = [i for i in order[: int(p.top_n * p.buffer)] if np.isfinite(sc[i])]
            keep = {i for i in shares if i != soxs and i in set(ranked)}
            target = list(keep)
            for i in ranked:
                if len(target) >= p.top_n:
                    break
                if i not in keep:
                    target.append(i)
            n_target = p.top_n if regime_on else max(1, int(p.top_n * p.regime_off_exposure))
            pending_targets = set(target[:n_target])
    eq = pd.Series(dict(curve))
    if state is not None:
        c = C[-1]
        equity = cash + sum(q * px_or_last(c, i) for i, q in shares.items())
        state.update({
            "as_of": days[-1],
            "equity": equity,
            "weights": {cols[i]: q * px_or_last(c, i) / equity for i, q in shares.items() if equity > 0},
            "pending_targets": None if pending_targets is None else sorted(cols[i] for i in pending_targets),
            "pending_exits": sorted(cols[i] for i in pending_exits),
            "exposure": exposure,
            "hedge_on": bool(pending_hedge if pending_hedge is not None else hedge_on),
            "top_n": p.top_n,
            "hedge_weight": p.hedge_weight,
        })
    return eq, pd.DataFrame(trades, columns=["date", "symbol", "side", "reason"])


def stats(eq: pd.Series, trades: pd.DataFrame, capital: float) -> dict:
    r = eq.pct_change().fillna(eq.iloc[0] / capital - 1)
    yrs = eq.resample("YE").last()
    yrs = yrs / yrs.shift(1).fillna(capital) - 1
    is_, oos = r[r.index < SPLIT], r[r.index >= SPLIT]
    sh = lambda x: float(x.mean() / x.std() * np.sqrt(252)) if x.std() > 0 else 0.0  # noqa: E731
    return {
        "cagr": float((eq.iloc[-1] / capital) ** (252 / len(eq)) - 1),
        "sharpe": sh(r), "sh_is": sh(is_), "sh_oos": sh(oos),
        "max_dd": float((eq / eq.cummax() - 1).min()),
        "buys_per_month": float((trades["side"] == "BUY").sum() / (len(eq) / 21)) if len(trades) else 0.0,
        **{f"y{k.year}": round(float(v), 3) for k, v in yrs.items()},
    }


def benchmark(w: dict, sym: str, start: str, capital: float) -> dict:
    c = w["close"][sym].loc[start:]
    eq = capital * c / c.iloc[0]
    return stats(eq, pd.DataFrame(columns=["side"]), capital)


if __name__ == "__main__":
    w = load()
    for b in ("SPY", "QQQ", "SMH"):
        print(b, {k: round(v, 3) for k, v in benchmark(w, b, "2019-01-02", 5000).items()})
    grid = {
        "universe_top": [500, 1000],
        "score": ["mom12_1", "mom6_1", "rs", "mom_vol"],
        "top_n": [5, 10, 15],
        "max_vol": [0.0, 0.6],
        "vol_target": [0.0, 0.20],
    }
    rows = []
    for combo in itertools.product(*grid.values()):
        p = replace(PP(), **dict(zip(grid, combo)))
        eq, tr = backtest(w, p)
        rows.append({**dict(zip(grid, combo)), **stats(eq, tr, p.capital)})
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in rows[-1].items()}, flush=True)
    pd.DataFrame(rows).to_csv(RES / "sweep_portfolio.csv", index=False)
    eq, tr = backtest(w, PP())
    print("CHOSEN (PP defaults):", {k: round(v, 3) for k, v in stats(eq, tr, 5000).items()})
