"""The relay's stocks-in-play leg with regime "none": why it earns less per trade than with the risk-on filter,
and whether stock-level filters or overnight runners raise its per-trade edge and Deflated Sharpe ratio without
bringing back the index filter. 2024-01 .. 2026-10 SIP minutes; "IS" = before July 2025, "OOS" = after
(research/backtest.py's split). Research only: nothing here is used by the live bot.

  A. trades taken with vs without the risk-on condition (QQQ above VWAP, VIXY below its open) at entry
  B. stock-level filters on the base (bigger-swing, regime none, closed by 16:00): scanner rank (top 10 / 15 / 30
     by opening relative volume), relative volume >= 2 / 3, opening gap >= 0 / +2%, and at least one Alpaca news
     item between the previous close and 09:30. For each: per-trade net (all candidate trades, and as taken with
     the live sizing on its own pot), the Deflated Sharpe ratio (trials = 250 earlier + this grid), and the relay
     on the 30/70 account
  C. overnight runners inside the relay: the sleeve's hold-strong rule (>= 1R up and closing in the top 20% of the
     day's range: hold, stop to breakeven), sold at the next day's 09:31 open or at the next day's close / stop.
     A runner's cash stays tied up the next morning: the book's slots and the breakout use only what is free.

    python -m trader.research.relay_quality
"""
from __future__ import annotations

import asyncio
import itertools
import math
from dataclasses import replace
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd

from . import backtest as BT
from . import intraday_relay as IR
from . import scalp as scalp_research
from ..alpaca import AlpacaData
from ..config import DATA_DIR
from .edge_stats import deflated_sharpe, orb_trades

NEWS = "https://data.alpaca.markets/v1beta1/news"
NEWS_CACHE = DATA_DIR / "research" / "inplay_news.parquet"
SPLIT = pd.Timestamp("2025-07-01")
COMM = 0.001
BASE = replace(IR.BIGGER, regime="none")
PRIOR_TRIALS = 250


# ── news before the open for every in-play candidate ─────────────────────────
async def _fetch_news(inplay: pd.DataFrame) -> pd.DataFrame:
    days = sorted(inplay["date"].unique())
    prev = {d: days[i - 1] for i, d in enumerate(days) if i}
    rows = []
    async with AlpacaData() as api:
        sem = asyncio.Semaphore(8)

        async def one(d, syms):
            p = prev.get(d, d - timedelta(days=1))
            start = datetime.combine(p, time(16, 0), BT.ET).isoformat()
            end = datetime.combine(d, time(9, 30), BT.ET).isoformat()
            counts = dict.fromkeys(syms, 0)
            token = None
            async with sem:
                for _ in range(20):
                    params = {"symbols": ",".join(syms), "start": start, "end": end, "limit": 50, "sort": "asc"}
                    if token:
                        params["page_token"] = token
                    res = await api._get(NEWS, params)
                    for item in res.get("news", []):
                        for s in item.get("symbols", []):
                            if s in counts:
                                counts[s] += 1
                    token = res.get("next_page_token")
                    if not token:
                        break
            return [(d, s, n) for s, n in counts.items()]

        jobs = [one(d, list(g["symbol"])) for d, g in inplay.groupby("date")]
        for chunk in range(0, len(jobs), 80):
            for res in await asyncio.gather(*jobs[chunk:chunk + 80]):
                rows += res
    return pd.DataFrame(rows, columns=["date", "symbol", "news"])


def news_counts(inplay: pd.DataFrame) -> pd.DataFrame:
    if NEWS_CACHE.exists():
        return pd.read_parquet(NEWS_CACHE)
    n = asyncio.run(_fetch_news(inplay))
    n.to_parquet(NEWS_CACHE)
    return n


# ── trades ───────────────────────────────────────────────────────────────────
def walk_open(p, path, first, entry, stop, atr, entry_bar_fill=True):
    """BT.walk, but an overnight runner is sold at the next day's first bar (09:31 open)."""
    px, i, reason, held = _walk(p, path, first, entry, stop, atr, entry_bar_fill)
    if held and reason in ("close_d1", "stop", "gap_stop", "target") and i > path.d_idx[-1]:
        j = path.d_idx[-1] + 1
        if j <= path.end:
            if reason == "gap_stop" and i == j:
                return px, i, reason, held  # gapped through the stop at the open: same price
            return path.o[j] * (1 - p.slip), j, "open_d1", held
    return px, i, reason, held


_walk = BT.walk


def trades_for(p, data, open_exit: bool = False) -> pd.DataFrame:
    inplay, macro, cache, _ = data
    BT.walk = walk_open if open_exit else _walk
    try:
        tr = BT.generate_trades(p, inplay, macro, cache)
    finally:
        BT.walk = _walk
    tr["d"] = pd.to_datetime(tr["date"])
    tr["net"] = tr["exit"] * (1 - COMM - BT.SEC_FEE) / (tr["entry"] * (1 + COMM)) - 1
    return tr


def annotate(tr: pd.DataFrame, data, news: pd.DataFrame) -> pd.DataFrame:
    inplay, macro, _, _ = data
    k = inplay[["symbol", "date", "rank", "open", "prev_close"]].copy()
    k["gap"] = k["open"] / k["prev_close"] - 1
    tr = tr.merge(k[["symbol", "date", "rank", "gap"]], on=["symbol", "date"], how="left")
    tr = tr.merge(news, on=["symbol", "date"], how="left")
    tr["news"] = tr["news"].fillna(0)
    ro = replace(BASE, regime="risk_on")
    tr["risk_on"] = [BT.regime_ok(ro, macro.get(d), ts) for d, ts in zip(tr["date"], tr["entry_ts"])]
    return tr


def to_day_dict(tr: pd.DataFrame) -> dict[pd.Timestamp, list[dict]]:
    out: dict[pd.Timestamp, list[dict]] = {}
    for row in tr.sort_values(["entry_ts", "rvol"], ascending=[True, False]).itertuples(index=False):
        out.setdefault(pd.Timestamp(row.date), []).append({
            "entry_min": IR.minute(row.entry_ts), "exit_min": IR.minute(row.exit_ts), "exit_day": pd.Timestamp(row.exit_ts.date()),
            "entry": row.entry, "stop0": row.stop0, "exit": row.exit, "symbol": row.symbol})
    return out


# ── the relay with overnight carry ───────────────────────────────────────────
def relay_account(days, share, swing_r, book, bear, orb, sip: dict | None, p, start=IR.START):
    """Account value per day (scalp pot share `share`, yearly rebalance). Stocks-in-play positions may carry
    overnight: their cost stays out of the pot's free cash until they are sold, and the next day's book slots
    and breakout are sized from free cash only."""
    cash, swing = start * share, start * (1 - share)
    carried: list[dict] = []  # open runners: cost, proceeds, exit day, exit minute, symbol
    year, vals = days[0].year, []
    info = {"sip_n": 0, "runners": 0, "runner_pnl": 0.0, "book_shrunk": 0, "sip_pnl_is": 0.0, "sip_pnl_oos": 0.0}
    for d in days:
        tied = sum(x["cost"] for x in carried)
        if d.year != year and not carried:  # yearly rebalance (waits while a runner is held)
            total = cash + swing
            cash, swing, year = total * share, total * (1 - share), d.year
        pot = cash + tied
        slot = pot / IR.SLOTS
        events = []  # (minute, delta cash) for releases
        for x in [x for x in carried if x["exit_day"] <= d]:
            events.append((x["exit_min"], x["proceeds"]))
            info["runner_pnl"] += x["proceeds"] - x["cost"]
            carried.remove(x)
        free = cash
        open_book = []
        book_pnl = 0.0
        for j, q, net in book.get(d, []):
            for m, amt in [e for e in events if e[0] < j]:  # runners sold before this minute (none before 09:31)
                free += amt
                events.remove((m, amt))
            for b in [b for b in open_book if b[0] < j]:
                free += b[1]
                open_book.remove(b)
            size = min(slot, free)
            if size < slot - 1e-6:
                info["book_shrunk"] += 1
            if size <= 0:
                continue
            free -= size
            open_book.append((q, size * (1 + net)))
            book_pnl += size * net
        if bool(bear.get(d, False)):
            for b in open_book:  # the book is flat by 09:59
                free += b[1]
            for m, amt in [e for e in events if e[0] < 360]:  # runners sold before 10:00
                free += amt
                events.remove((m, amt))
            orb_net = orb.get(d)
            if orb_net is not None:  # the breakout trades the free cash (all of the pot unless a runner is still held)
                free *= 1 + orb_net
            cash = free + sum(amt for _, amt in events)
        else:
            pending = sorted([(q, amt) for q, amt in open_book] + events)
            open_sip: list[dict] = []
            for tr in (sip or {}).get(d, []):
                t = tr["entry_min"]
                while pending and pending[0][0] < t:
                    free += pending.pop(0)[1]
                for pos in [x for x in open_sip if x["exit_day"] == d and x["exit_min"] < t]:
                    free += pos["proceeds"]
                    open_sip.remove(pos)
                held_syms = {x["symbol"] for x in open_sip} | {x["symbol"] for x in carried}
                if len(open_sip) + len(carried) >= p.max_positions or tr["symbol"] in held_syms:
                    continue
                risk = tr["entry"] - tr["stop0"]
                if risk <= 0:
                    continue
                shares = math.floor(min(p.risk_per_trade * pot / risk, p.max_pos_pct * pot / tr["entry"], free / (tr["entry"] * (1 + COMM))))
                if shares < 1:
                    continue
                cost = shares * tr["entry"] * (1 + COMM)
                proceeds = shares * tr["exit"] * (1 - COMM - BT.SEC_FEE)
                free -= cost
                info["sip_n"] += 1
                info["sip_pnl_is" if d < SPLIT else "sip_pnl_oos"] += proceeds - cost
                pos = {"symbol": tr["symbol"], "exit_day": tr["exit_day"], "exit_min": tr["exit_min"], "cost": cost, "proceeds": proceeds}
                if tr["exit_day"] > d:
                    carried.append(pos)
                    info["runners"] += 1
                else:
                    open_sip.append(pos)
            for q, amt in pending:
                free += amt
            for pos in open_sip:
                free += pos["proceeds"]
            cash = free
        swing *= 1 + swing_r.get(d, 0.0)
        vals.append(cash + sum(x["cost"] for x in carried) + swing)
    return pd.Series(vals, index=days), info


def per_trade(r: np.ndarray, isx: np.ndarray, trials: int) -> dict:
    out = {"n": len(r), "mean": r.mean(), "is": r[isx].mean() if isx.any() else np.nan, "oos": r[~isx].mean() if (~isx).any() else np.nan}
    out.update(deflated_sharpe(r, trials) if len(r) > 30 else {"dsr": np.nan})
    return out


def main() -> None:
    print("loading ...", flush=True)
    data = BT.load_all()
    news = news_counts(data[0])
    m, _ = scalp_research.load_matrices()
    bear = m["regime"] == "bear"
    del m
    book = IR.book_trades(bear)
    orb = orb_trades().groupby("date")["net"].sum()
    orb.index = pd.to_datetime(orb.index)
    swing = IR.swing_returns(0.20)
    days = swing.index[(swing.index >= "2024-01-02") & (swing.index <= pd.Timestamp("2026-10-09"))]
    yrs = len(days) / 252

    base = annotate(trades_for(BASE, data), data, news)
    print(f"{len(base)} candidate trades (bigger-swing, regime none, closed by 16:00); news coverage: "
          f"{(base['news'] > 0).mean():.0%} of trades had pre-open news")

    print("\n=== A. Why 'none' earns less per trade: trades by the risk-on condition at entry (every candidate trade, net of fees) ===")
    for lab, g in (("all (regime none)", base), ("risk-on at entry", base[base["risk_on"]]), ("risk-off at entry", base[~base["risk_on"]])):
        isx = (g["d"] < SPLIT).to_numpy()
        r = g["net"].to_numpy()
        print(f"  {lab:20s} n {len(r):5d}  avg {r.mean():+.2%}  win {(r > 0).mean():.0%}  before July 2025 {r[isx].mean():+.2%} (n {isx.sum()})  "
              f"after {r[~isx].mean():+.2%} (n {(~isx).sum()})")

    grid = {"top_n": (30, 15, 10), "min_rvol": (1.0, 2.0, 3.0), "min_gap": (-1.0, 0.0, 0.02), "news": (False, True)}
    combos = list(itertools.product(*grid.values()))
    trials = PRIOR_TRIALS + len(combos)
    print(f"\n=== B. Stock-level filters ({len(combos)} variants; Deflated Sharpe with {trials} trials) ===")
    rows = []
    for top_n, rv, gap, nw in combos:
        f = base[(base["rank"] <= top_n) & (base["rvol"] >= rv) & (base["gap"] >= gap) & ((base["news"] > 0) if nw else True)]
        if len(f) < 40:
            continue
        p = replace(BASE, top_n=top_n, min_rvol=rv, min_gap=gap, capital=3720.0)
        taken, _ = BT.run_portfolio(p, f.drop(columns=["d", "net", "rank", "gap", "news", "risk_on"]))
        tk = taken["ret"].to_numpy()
        tk_is = (pd.to_datetime(taken["date"]) < SPLIT).to_numpy()
        eq, info = relay_account(days, 0.30, swing, book, bear, orb, to_day_dict(f), p)
        mm = IR.metrics(eq)
        rows.append({"top_n": top_n, "rvol": rv, "gap": gap, "news": nw, "cand_n": len(f), "cand_mean": f["net"].mean(),
                     **{f"taken_{k}": v for k, v in per_trade(tk, tk_is, trials).items()},
                     "CAGR": mm["CAGR"], "Sharpe": mm["Sharpe"], "maxDD": mm["maxDD"], "sip_is": info["sip_pnl_is"], "sip_oos": info["sip_pnl_oos"]})
    v = pd.DataFrame(rows)
    v.to_parquet(DATA_DIR / "research" / "relay_quality_grid.parquet")
    show = lambda s: (f"top {s['top_n']:2d} rvol>={s['rvol']:.0f} gap>={'off' if s['gap'] < -0.5 else format(s['gap'], '.0%'):>3s} news {'yes' if s['news'] else 'any':3s} | "  # noqa: E731
                      f"taken n {int(s['taken_n']):4d} ({s['taken_n'] / yrs:3.0f}/yr) avg {s['taken_mean']:+.2%} IS {s['taken_is']:+.2%} OOS {s['taken_oos']:+.2%} "
                      f"DSR {s['taken_dsr']:.2f} | 30/70 relay CAGR {s['CAGR']:+.1%} Sh {s['Sharpe']:.2f} DD {s['maxDD']:+.1%} | "
                      f"SIP P&L IS ${s['sip_is']:+,.0f} OOS ${s['sip_oos']:+,.0f}")
    b0 = v[(v["top_n"] == 30) & (v["rvol"] == 1.0) & (v["gap"] < -0.5) & (~v["news"])].iloc[0]
    print("  base:  " + show(b0))
    print("  one filter at a time:")
    for col, vals in (("top_n", (15, 10)), ("rvol", (2.0, 3.0)), ("gap", (0.0, 0.02)), ("news", (True,))):
        for val in vals:
            sel = {"top_n": 30, "rvol": 1.0, "gap": -1.0, "news": False}
            sel[col] = val
            s = v[(v["top_n"] == sel["top_n"]) & (v["rvol"] == sel["rvol"]) & (v["gap"] == sel["gap"]) & (v["news"] == sel["news"])]
            if len(s):
                print("    " + show(s.iloc[0]))
    print("  top 8 by the before-July-2025 per-trade average (n >= 60 taken), then after:")
    for _, s in v[v["taken_n"] >= 60].sort_values("taken_is", ascending=False).head(8).iterrows():
        print("    " + show(s))
    print(f"  variants that beat the base on per-trade avg AND DSR AND keep >= 80% of its after-July SIP P&L: "
          f"{int(((v['taken_mean'] > b0['taken_mean']) & (v['taken_dsr'] > b0['taken_dsr']) & (v['sip_oos'] >= 0.8 * b0['sip_oos'])).sum())} of {len(v)}")

    print("\n=== C. Overnight runners inside the relay (base filters) ===")
    variants = {"closed by 16:00 (live)": trades_for(BASE, data),
                "hold strong, sold next day 09:31": trades_for(replace(BASE, exit_mode="hold_strong"), data, open_exit=True),
                "hold strong, sold next day close/stop": trades_for(replace(BASE, exit_mode="hold_strong"), data)}
    for lab, t in variants.items():
        r, isx = t["net"].to_numpy(), (t["d"] < SPLIT).to_numpy()
        runners = t[t["overnight"]]
        eq, info = relay_account(days, 0.30, swing, book, bear, orb, to_day_dict(t), BASE)
        mm = IR.metrics(eq)
        print(f"  {lab:38s} candidates avg {r.mean():+.2%} (IS {r[isx].mean():+.2%} OOS {r[~isx].mean():+.2%}), runners {len(runners)} "
              f"(their avg {runners['net'].mean() if len(runners) else 0:+.2%}) | 30/70: CAGR {mm['CAGR']:+.1%} Sharpe {mm['Sharpe']:.2f} "
              f"maxDD {mm['maxDD']:+.1%} | relay runners taken {info['runners']}, book slots shrunk {info['book_shrunk']}, "
              f"SIP P&L IS ${info['sip_pnl_is']:+,.0f} OOS ${info['sip_pnl_oos']:+,.0f}")


if __name__ == "__main__":
    main()
