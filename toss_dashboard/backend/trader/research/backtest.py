"""Stocks-in-play intraday backtester on 1-minute SIP bars with Toss costs.

Each candidate trade is simulated on its own minute path, then a portfolio pass
applies cash, integer shares, max positions and compounding. Exits never depend
on other positions, so the two-pass split is exact.

    python -m trader.research.backtest            # parameter sweep + report
"""
from __future__ import annotations

import argparse
import itertools
import math
from dataclasses import asdict, dataclass, replace
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from ..config import DATA_DIR

ET = ZoneInfo("America/New_York")
RES = DATA_DIR / "research"
SEC_FEE = 0.0000206
SPLIT_DATE = date(2025, 7, 1)  # in-sample before, out-of-sample after


@dataclass(frozen=True)
class Params:
    strategy: str = "orb"  # orb | tc | overnight
    or_minutes: int = 5
    top_n: int = 20
    min_rvol: float = 1.0
    require_green: bool = True
    stop_mode: str = "atr"  # atr | or_low
    stop_atr: float = 0.10
    trail_atr: float = 0.0  # 0 = no trailing stop
    target_r: float = 0.0  # 0 = no profit target
    exit_mode: str = "eod"  # eod | hold_strong
    regime: str = "none"  # none | qqq_vwap | qqq_green | risk_on
    entry_cutoff: time = time(11, 30)
    min_atr_pct: float = 0.0
    min_gap: float = -1.0  # open vs previous close
    require_trend: bool = False  # previous close above its 50-day average
    max_positions: int = 4
    risk_per_trade: float = 0.01
    max_pos_pct: float = 0.35
    commission: float = 0.001
    slip: float = 0.0005
    capital: float = 3000.0
    tc_time: time = time(10, 0)
    tc_min_move_atr: float = 0.3
    tc_near_high_atr: float = 0.25


# ── data ──────────────────────────────────────────────────────────────────
def load_inplay() -> pd.DataFrame:
    df = pd.read_parquet(RES / "inplay.parquet")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def load_opening() -> pd.DataFrame:
    frames = [pd.read_parquet(p) for p in sorted((RES / "opening").glob("*.parquet"))]
    o = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    o["et"] = o["ts"].dt.tz_convert(ET)
    o["date"] = o["et"].dt.date
    o["slot"] = (o["et"].dt.hour * 60 + o["et"].dt.minute - 570) // 5
    return o


def opening_windows(opening: pd.DataFrame, inplay: pd.DataFrame) -> pd.DataFrame:
    """OR high/low/open/close + RVOL for 5/15/30-minute windows, per in-play row."""
    keys = inplay[["symbol", "date"]]
    o = opening.merge(keys, on=["symbol", "date"])
    out = keys.copy()
    for n, slots in ((5, 1), (15, 3), (30, 6)):
        w = o[o["slot"] < slots].groupby(["symbol", "date"]).agg(
            **{f"or{n}_open": ("open", "first"), f"or{n}_high": ("high", "max"),
               f"or{n}_low": ("low", "min"), f"or{n}_close": ("close", "last")}
        )
        out = out.merge(w.reset_index(), on=["symbol", "date"], how="left")
    return out


def load_macro() -> dict[date, pd.DataFrame]:
    m = pd.read_parquet(RES / "macro_1m.parquet")
    m["et"] = m["ts"].dt.tz_convert(ET)
    m["date"] = m["et"].dt.date
    m = m[(m["et"].dt.time >= time(9, 30)) & (m["et"].dt.time < time(16, 0))]
    q = m[m["symbol"] == "QQQ"].copy()
    q["pv"] = q["close"] * q["volume"]
    g = q.groupby("date")
    q["vwap"] = g["pv"].cumsum() / g["volume"].cumsum()
    q["day_open"] = g["open"].transform("first")
    q["above_vwap"] = q["close"] > q["vwap"]
    q["green"] = q["close"] > q["day_open"]
    v = m[m["symbol"] == "VIXY"].copy()
    v["vixy_down"] = v["close"] < v.groupby("date")["open"].transform("first")
    q = q.merge(v[["et", "vixy_down"]], on="et", how="left")
    q["vixy_down"] = q.groupby("date")["vixy_down"].ffill().fillna(False).astype(bool)
    q["risk_on"] = q["above_vwap"] & q["vixy_down"]
    return {d: x.set_index("et")[["close", "vwap", "above_vwap", "green", "risk_on"]] for d, x in q.groupby("date")}


def load_minutes(d: date) -> pd.DataFrame | None:
    p = RES / "minute" / f"{d.isoformat()}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.empty:
        return None
    df["et"] = df["ts"].dt.tz_convert(ET)
    df = df[(df["et"].dt.time >= time(9, 30)) & (df["et"].dt.time < time(16, 0))]
    return df


# ── single trade paths ────────────────────────────────────────────────────
class DayPath:
    """Minute arrays for one symbol over day D (and D+1 when present)."""

    def __init__(self, bars: pd.DataFrame, day: date):
        self.et = bars["et"]
        self.o, self.h, self.l, self.c, self.v = (bars[k].values for k in ("open", "high", "low", "close", "volume"))
        mask = self.et.dt.date.values == day
        self.d_idx = np.flatnonzero(mask)
        self.t = self.et.dt.time.values
        self.end = len(bars) - 1

    @property
    def ok(self) -> bool:
        return len(self.d_idx) >= 30

    def vwap(self) -> np.ndarray:
        i0, i1 = self.d_idx[0], self.d_idx[-1] + 1
        tp = (self.h[i0:i1] + self.l[i0:i1] + self.c[i0:i1]) / 3
        vol = self.v[i0:i1]
        return np.cumsum(tp * vol) / np.maximum(np.cumsum(vol), 1e-9)


def regime_ok(p: Params, regime: pd.DataFrame | None, ts) -> bool:
    if p.regime == "none" or regime is None:
        return True
    r = regime.loc[: ts - pd.Timedelta(minutes=1)]
    if r.empty:
        return False
    col = {"qqq_vwap": "above_vwap", "qqq_green": "green", "risk_on": "risk_on"}[p.regime]
    return bool(r[col].iloc[-1])


def walk(p: Params, path: DayPath, first: int, entry: float, stop: float, atr: float, entry_bar_fill: bool = True):
    """Manage a long from bar index `first` until stop/target/close. Returns (exit_px, exit_i, reason, overnight)."""
    risk = entry - stop
    target = entry + p.target_r * risk if p.target_r else math.inf
    highest = entry
    last_d = path.d_idx[-1]
    d0 = path.d_idx[0]
    o, h, l, c = path.o, path.h, path.l, path.c
    held = False
    i = first
    while i <= path.end:
        if i > last_d and not held:
            break
        lo, hi, op = l[i], h[i], o[i]
        on_entry_bar = i == first and entry_bar_fill
        # Entry bar: assume O->L->H->C for green bars (the low came before the
        # breakout, so only a close back under the stop counts) and O->H->L->C for red.
        stop_hit = (c[i] <= stop) if (on_entry_bar and c[i] >= op) else lo <= stop
        if not on_entry_bar and op <= stop:
            return op * (1 - p.slip), i, "gap_stop", held
        if stop_hit:
            return stop * (1 - p.slip), i, "stop", held
        if hi >= target:
            return (target if on_entry_bar else max(op, target)), i, "target", held
        highest = max(highest, hi)
        if p.trail_atr:
            stop = max(stop, highest - p.trail_atr * atr)
        if i == last_d:
            strong = False
            if p.exit_mode == "hold_strong" and i < path.end:
                day_hi, day_lo = h[d0 : last_d + 1].max(), l[d0 : last_d + 1].min()
                strong = c[i] - entry >= risk and (c[i] - day_lo) / max(day_hi - day_lo, 1e-9) >= 0.8
            if not strong:
                return c[i] * (1 - p.slip / 2), i, "close", held
            held = True
            stop = max(stop, entry)
        i += 1
    i = min(i, path.end)
    return c[i] * (1 - p.slip / 2), i, "close_d1", held


def trade_record(p, row, path: DayPath, first: int, entry: float, stop: float, atr: float, entry_bar_fill=True) -> dict:
    exit_px, exit_i, reason, held = walk(p, path, first, entry, stop, atr, entry_bar_fill)
    return {
        "date": row.date, "symbol": row.symbol, "entry_ts": path.et.iloc[first], "exit_ts": path.et.iloc[exit_i],
        "entry": entry, "stop0": stop, "exit": exit_px, "reason": reason, "rvol": row.rvol,
        "atr_pct": atr / row.prev_close, "overnight": held,
    }


def simulate_orb(p: Params, row, path: DayPath, regime) -> dict | None:
    n = p.or_minutes
    or_high, or_low = getattr(row, f"or{n}_high"), getattr(row, f"or{n}_low")
    or_open, or_close = getattr(row, f"or{n}_open"), getattr(row, f"or{n}_close")
    if not np.isfinite(or_high) or not np.isfinite(row.atr14):
        return None
    if p.require_green and not or_close > or_open:
        return None
    if p.min_atr_pct and row.atr14 / row.prev_close < p.min_atr_pct:
        return None
    if row.open / row.prev_close - 1 < p.min_gap:
        return None
    if p.require_trend and not row.prev_close > row.sma50_prev:
        return None
    or_end = time(9, 30 + n) if n < 30 else time(10, 0)
    trigger = or_high + 0.01
    di = path.d_idx
    for k in range(np.searchsorted(path.t[di], or_end), len(di)):
        i = di[k]
        if path.t[i] >= p.entry_cutoff:
            return None
        if path.h[i] >= trigger:
            if not regime_ok(p, regime, path.et.iloc[i]):
                return None
            entry = max(path.o[i], trigger) * (1 + p.slip)
            stop = entry - p.stop_atr * row.atr14 if p.stop_mode == "atr" else or_low - 0.01
            if stop >= entry:
                return None
            return trade_record(p, row, path, i, entry, stop, row.atr14)
    return None


def simulate_tc(p: Params, row, path: DayPath, regime) -> dict | None:
    """Trend continuation: at `tc_time`, buy if above VWAP, up on the day and near the high."""
    di = path.d_idx
    k = np.searchsorted(path.t[di], p.tc_time)
    if k >= len(di) or k < 5:
        return None
    i = di[k]
    vw = path.vwap()[k - 1]
    prev_close = path.c[di[k - 1]]
    day_open = path.o[di[0]]
    day_hi = path.h[di[0] : i].max()
    atr = row.atr14
    if not (prev_close > vw and prev_close - day_open >= p.tc_min_move_atr * atr and day_hi - prev_close <= p.tc_near_high_atr * atr):
        return None
    if not regime_ok(p, regime, path.et.iloc[i]):
        return None
    entry = path.o[i] * (1 + p.slip)
    stop = min(entry - p.stop_atr * atr, vw - 0.01) if p.stop_mode == "vwap" else entry - p.stop_atr * atr
    if stop >= entry:
        return None
    return trade_record(p, row, path, i, entry, stop, atr, entry_bar_fill=False)


def simulate_overnight(p: Params, row, path: DayPath, regime) -> dict | None:
    """Buy strong in-play closers at the last minute, sell at the next session's open."""
    di = path.d_idx
    last = di[-1]
    if last >= path.end:
        return None
    day_hi, day_lo = path.h[di].max(), path.l[di].min()
    close = path.c[last]
    vw = path.vwap()[-1]
    if (close - day_lo) / max(day_hi - day_lo, 1e-9) < 0.8 or close <= vw or close <= path.o[di[0]]:
        return None
    entry = close * (1 + p.slip / 2)
    nxt = last + 1
    exit_px = path.o[nxt] * (1 - p.slip)
    return {
        "date": row.date, "symbol": row.symbol, "entry_ts": path.et.iloc[last], "exit_ts": path.et.iloc[nxt],
        "entry": entry, "stop0": entry - row.atr14, "exit": exit_px, "reason": "next_open", "rvol": row.rvol,
        "atr_pct": row.atr14 / row.prev_close, "overnight": True,
    }


SIMULATORS = {"orb": simulate_orb, "tc": simulate_tc, "overnight": simulate_overnight}


def generate_trades(p: Params, inplay: pd.DataFrame, macro: dict, cache: dict) -> pd.DataFrame:
    trades = []
    sel = inplay[(inplay["rank"] <= p.top_n) & (inplay["rvol"] >= p.min_rvol)]
    for d, rows in sel.groupby("date"):
        grouped = cache.get(d)
        if grouped is None:
            continue
        reg = macro.get(d)
        for row in rows.itertuples(index=False):
            path = grouped.get(row.symbol)
            if path is None or not path.ok:
                continue
            tr = SIMULATORS[p.strategy](p, row, path, reg)
            if tr:
                trades.append(tr)
    return pd.DataFrame(trades)


# ── portfolio ─────────────────────────────────────────────────────────────
def run_portfolio(p: Params, trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    if trades.empty:
        return trades, pd.Series(dtype=float)
    trades = trades.sort_values(["entry_ts", "rvol"], ascending=[True, False]).reset_index(drop=True)
    cash = p.capital
    open_pos: list[dict] = []
    taken = []
    for tr in trades.itertuples(index=False):
        still = []
        for pos in open_pos:
            if pos["exit_ts"] <= tr.entry_ts:
                cash += pos["proceeds"]
            else:
                still.append(pos)
        open_pos = still
        if len(open_pos) >= p.max_positions or any(x["symbol"] == tr.symbol for x in open_pos):
            continue
        equity = cash + sum(x["cost"] for x in open_pos)
        risk = tr.entry - tr.stop0
        shares = math.floor(min(p.risk_per_trade * equity / risk, p.max_pos_pct * equity / tr.entry, cash / (tr.entry * (1 + p.commission))))
        if shares < 1:
            continue
        cost = shares * tr.entry * (1 + p.commission)
        proceeds = shares * tr.exit * (1 - p.commission - SEC_FEE)
        cash -= cost
        open_pos.append({"symbol": tr.symbol, "exit_ts": tr.exit_ts, "cost": cost, "proceeds": proceeds})
        taken.append({**tr._asdict(), "shares": shares, "pnl": proceeds - cost, "ret": proceeds / cost - 1, "equity_at_entry": equity})
    out = pd.DataFrame(taken)
    if out.empty:
        return out, pd.Series(dtype=float)
    out["exit_date"] = out["exit_ts"].apply(lambda x: x.date())
    daily_pnl = out.groupby("exit_date")["pnl"].sum()
    return out, daily_pnl


def metrics(p: Params, taken: pd.DataFrame, daily_pnl: pd.Series, days: list[date]) -> dict:
    if taken.empty:
        return {"trades": 0}
    pnl = daily_pnl.reindex(days, fill_value=0.0)
    equity = p.capital + pnl.cumsum()
    rets = equity.pct_change().fillna(pnl.iloc[0] / p.capital)
    years = len(days) / 252
    dd = (equity / equity.cummax() - 1).min()
    wins = taken[taken["pnl"] > 0]
    losses = taken[taken["pnl"] <= 0]
    is_mask = np.array([d < SPLIT_DATE for d in pnl.index])

    def sharpe(r):
        return float(r.mean() / r.std() * np.sqrt(252)) if r.std() > 0 else 0.0

    def period_ret(mask):
        e = p.capital + pnl[mask].cumsum()
        return float(e.iloc[-1] / p.capital - 1) if len(e) else 0.0

    return {
        "trades": len(taken),
        "per_day": len(taken) / len(days),
        "final": float(equity.iloc[-1]),
        "cagr": float((equity.iloc[-1] / p.capital) ** (1 / years) - 1),
        "sharpe": sharpe(rets),
        "sharpe_is": sharpe(rets[is_mask]),
        "sharpe_oos": sharpe(rets[~is_mask]),
        "ret_is": period_ret(is_mask),
        "ret_oos": period_ret(~is_mask),
        "max_dd": float(dd),
        "win_rate": len(wins) / len(taken),
        "avg_ret": float(taken["ret"].mean()),
        "pf": float(wins["pnl"].sum() / -losses["pnl"].sum()) if len(losses) and losses["pnl"].sum() < 0 else float("inf"),
        "overnight": int(taken["overnight"].sum()),
    }


def load_all():
    inplay = load_inplay()
    windows = opening_windows(load_opening(), inplay)
    inplay = inplay.merge(windows, on=["symbol", "date"], how="left")
    macro = load_macro()
    days = sorted(inplay["date"].unique())
    cache = {}
    for d in days:
        bars = load_minutes(d)
        if bars is not None:
            cache[d] = {sym: DayPath(g.reset_index(drop=True), d) for sym, g in bars.groupby("symbol")}
    days = [d for d in days if cache.get(d) is not None]
    return inplay, macro, cache, days


def sweep(grid: dict, base: Params, inplay, macro, cache, days) -> pd.DataFrame:
    keys = list(grid)
    rows = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        p = replace(base, **dict(zip(keys, combo)))
        trades = generate_trades(p, inplay, macro, cache)
        taken, daily = run_portfolio(p, trades)
        m = metrics(p, taken, daily, days)
        rows.append({**{k: v for k, v in zip(keys, combo)}, **m})
        print({k: (round(v, 3) if isinstance(v, float) else v) for k, v in rows[-1].items()}, flush=True)
    return pd.DataFrame(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(RES / "sweep.csv"))
    args = ap.parse_args()
    inplay, macro, cache, days = load_all()
    print(f"{len(days)} days, {len(inplay)} in-play rows")
    grid = {
        "or_minutes": [5, 15, 30],
        "stop_mode": ["atr", "or_low"],
        "stop_atr": [0.1, 0.25, 0.5],
        "trail_atr": [0.0, 0.5, 1.0],
        "exit_mode": ["eod", "hold_strong"],
        "regime": ["none", "qqq_vwap"],
    }
    res = sweep(grid, Params(), inplay, macro, cache, days)
    res.to_csv(args.out, index=False)
    print(res.sort_values("sharpe", ascending=False).head(25).to_string())
