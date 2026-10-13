"""Replay check: run the LIVE scalp code over past days and compare it with the research backtests.

Unit tests check pieces. This checks behaviour: the real ScalpSleeve / PremarketBook classes (with the
order guard switched on) trade historical SIP minute bars through a simulated clock and a paper
broker, using the same scheduling method the engine calls (ScalpSleeve.tick). Every day's result is
compared with what the validated research code did on the same day:
  breakout    research/scalp.py e2_orb (30-minute range, bear regime only)
  pre-market  research/scalp_ext.py _dip_trade ("level" fills) on the book's 14 names, with the same
              3 slots (signals taken in time order) and QQQ trend filter (research/pre_slots.py)
A change that passes the unit tests but trades differently (wrong side, window, size, stop, a guard
that blocks real trades, an exception) shows up as a parity drop, and the deploy is refused.

Inside each minute the price walks open -> low -> high -> close (open -> high -> low -> close on down
bars) in steps of ~0.25% during the minutes that matter (06:55-09:30 ET for the book, 09:59-16:00 for SOXL/SOXS), like a live
stream crossing levels; elsewhere one tick per minute. bid/ask = price -/+ 1 cent. Fills follow the
research's rule: a buy priced at or above the ask (marketable) fills at the ask, a resting buy fills
at its limit only when the price trades 0.3% through it (and a resting sell 0.05% through its limit).
Prices are rescaled per day (~$100 at 09:29) so old split-adjusted prices size like today's.

    python -m trader.replay                 # full report (all days since 2021)
    python -m trader.replay --gate          # fixed sample, exit code 1 if parity is below the thresholds
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import multiprocessing
import os
import sys
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .broker import PaperBroker
from .config import Settings
from .guard import OrderGuard
from .journal import J
from .market import Bar, MarketData
from .research import scalp as R
from .research.pre_multi import matrices as pre_matrices
from .research.pre_slots import all_trades, qqq_up
from .scalp import ScalpSleeve, ScalpState
from .scalp_pre import NAMES, PreState

ET = ZoneInfo("America/New_York")
CAPITAL = 10000.0
SLOTS = 3
GATE = {"orb_match": 0.85, "orb_extra": 0.05, "orb_ret_gap": 0.0025, "pre_match": 0.70, "pre_ret_gap": 0.005,
        "refusal_share": 0.003}  # guard refusals allowed per live trade (extreme spike prints copied into quotes)


class ReplayBroker(PaperBroker):
    """Paper broker with the research fill rules (see the module docstring)."""

    async def submit(self, symbol, side, qty, limit, tag):
        o = await super().submit(symbol, side, qty, limit, tag)  # marketable orders fill here, at the market
        if not o.done:
            o._resting = True  # type: ignore[attr-defined]  # from now on it only fills at its limit
        return o

    def try_fill(self, o) -> None:
        if o.done:
            return
        s = self.market.get(o.symbol)
        if getattr(o, "_resting", False):
            if o.side == "BUY" and s.last and s.last <= o.limit * 0.997:
                self._fill(o, o.limit)
            elif o.side == "SELL" and s.last and s.last >= o.limit * 1.0005:
                self._fill(o, o.limit)
        elif o.side == "BUY":
            if o.limit is None or o.limit >= s.ask:
                self._fill(o, s.ask or s.last)
        elif o.limit is None or o.limit <= s.bid:
            self._fill(o, s.bid or s.last)


def path(points: tuple, step: float = 0.0025, max_steps: int = 6) -> list[float]:
    """Walk through the bar's turning points in small steps, like a stream crossing every level."""
    out = [points[0]]
    for a, b in zip(points[:-1], points[1:]):
        if not (np.isfinite(a) and np.isfinite(b)):
            continue
        n = int(min(max_steps, max(1, math.ceil(abs(b / a - 1) / step)))) if a else 1
        out += [a + (b - a) * (i + 1) / n for i in range(n)]
    return out


class FakeToss:
    def __init__(self, daily: dict[str, pd.Series], day: date):
        self.daily, self.day = daily, day

    async def candles(self, symbol, interval, count=200, before=None):
        s = self.daily[symbol].dropna()  # Toss never returns blank closes
        s = s[s.index < pd.Timestamp(self.day)].tail(count)[::-1]
        return {"candles": [{"timestamp": f"{d.date().isoformat()}T16:00:00-04:00", "closePrice": str(v)} for d, v in s.items()]}


class FakeAlpaca:
    """SIP minute bars of the replay day (start-stamped, like Alpaca) and daily closes before it."""

    def __init__(self, bars: pd.DataFrame, daily: dict[str, pd.Series], day: date):
        self.bars_df, self.daily, self.day = bars, daily, day

    async def bars(self, symbols, timeframe, start, end, feed="sip", adjustment="raw"):
        if timeframe == "1Day":
            rows = []
            for sym in symbols:
                ser = self.daily.get(sym)
                if ser is None:
                    continue
                ser = ser.dropna()
                ser = ser[ser.index < pd.Timestamp(self.day)].tail(300)
                rows += [{"symbol": sym, "ts": pd.Timestamp(d).tz_localize("UTC") + pd.Timedelta(hours=20), "close": float(v)}
                         for d, v in ser.items()]
            return pd.DataFrame(rows, columns=["symbol", "ts", "close"])
        s0, e0 = pd.Timestamp(start), pd.Timestamp(end)
        b = self.bars_df
        return b[(b["symbol"].isin(symbols)) & (b["ts"] >= s0) & (b["ts"] < e0)]


def day_frames(m: dict, pm: dict, d: pd.Timestamp) -> dict[str, np.ndarray]:
    """Per symbol [open, high, low, close, volume] x 960 minutes from 04:00 ET, rescaled to ~$100 at 09:29."""
    out = {}
    for sym in set(NAMES) | {"SOXL", "SOXS"}:
        if sym in ("SOXL", "SOXS"):
            src = {f: m[(sym, f)] for f in ("open", "high", "low", "close", "volume")}
        elif sym in pm:
            src = dict(pm[sym])
        else:
            continue
        if d not in src["close"].index:
            continue
        arr = {f: np.full(960, np.nan) for f in ("open", "high", "low", "close", "volume")}
        for f in ("high", "low", "close", "volume"):
            v = src[f].loc[d].to_numpy(dtype=float)[:960]
            arr[f][: len(v)] = v
        if "open" in src:
            v = src["open"].loc[d].to_numpy(dtype=float)[:960]
            arr["open"][: len(v)] = v
        else:  # minute data without opens: the previous close opens the bar
            prev = np.concatenate([[np.nan], arr["close"][:-1]])
            arr["open"] = np.where(np.isfinite(arr["high"]), np.where(np.isfinite(prev), prev, arr["close"]), np.nan)
        if np.isfinite(arr["close"][329]):
            ref = arr["close"][329]
        elif np.isfinite(arr["close"][:330]).any():
            ref = np.nanmedian(arr["close"][:330])
        else:
            continue
        if ref <= 0:
            continue
        k = 100.0 / ref
        out[sym] = np.vstack([arr["open"] * k, arr["high"] * k, arr["low"] * k, arr["close"] * k, np.nan_to_num(arr["volume"])])
    return out


async def replay_day(d: pd.Timestamp, m: dict, pm: dict, daily: dict, tmp: Path) -> dict:
    day = d.date()
    md = MarketData(client=None)
    fr = day_frames(m, pm, d)
    base = datetime.combine(day, time(4, 0), ET)
    rows = []
    for sym, (o, h, l, c, v) in fr.items():
        for k in range(720):
            if np.isfinite(h[k]):
                rows.append({"symbol": sym, "ts": pd.Timestamp(base + timedelta(minutes=k)).tz_convert("UTC"),
                             "open": o[k], "high": h[k], "low": l[k], "close": c[k], "volume": v[k]})
    md.alpaca = FakeAlpaca(pd.DataFrame(rows), daily, day)
    clock = {"now": base}
    broker = ReplayBroker(md, 0.001, CAPITAL)
    broker.guard = OrderGuard(lambda: CAPITAL * 2, lambda s: (md.get(s).bid, md.get(s).ask), broker.open_orders,
                              now=lambda: clock["now"], scalp_symbols={"SOXL", "SOXS"} | set(NAMES))
    refused: list[str] = []
    broker.on_refuse = refused.append
    s = Settings(mode="paper", capital=0.0, swing_capital=0.0, scalp_capital=CAPITAL, scalp_position_frac=1.0, scalp_force_day="",
                 scalp_pre=True, pre_names=NAMES, pre_slots=SLOTS, pre_qqq_filter=True)
    sleeve = ScalpSleeve(s, broker, md, FakeToss(daily, day), lambda urgent: True, lambda msg, lvl=20: None)
    sleeve.path = tmp / "scalp.json"
    sleeve.st, sleeve.cash, sleeve.realized, sleeve.retry_seconds = ScalpState(), CAPITAL, 0.0, 0.0
    for sym, n in sleeve.pre.names.items():
        n.st = PreState(symbol=sym)
    broker.on_fill = lambda order, q, px: sleeve.on_fill(order, q, px)

    async def fast_wait(order, seconds):
        broker.try_fill(order)

    sleeve._wait = fast_wait
    session = {"open": datetime.combine(day, time(9, 30), ET), "close": datetime.combine(day, time(16, 0), ET)}
    pre_session = {"open": base, "close": session["open"]}
    errors: list[str] = []

    def set_px(sym: str, px: float) -> None:
        st = md.get(sym)
        wall = datetime.now(ET)
        st.last, st.last_ts, st.bid, st.ask, st.quote_ts = px, wall, round(px - 0.01, 4), round(px + 0.01, 4), wall

    async def run(now: datetime, fn) -> None:
        clock["now"] = now
        tasks: list = []
        try:
            fn(tasks.append)
            for t in tasks:
                await t
        except Exception as exc:  # noqa: BLE001 — a crash is a finding, not a harness failure
            errors.append(f"{now:%H:%M:%S} {exc!r}")
        broker.on_quote()

    soxl = fr.get("SOXL")
    for k in range(0, 722):
        t0 = base + timedelta(minutes=k)
        if soxl is not None and 331 <= k <= 720 and np.isfinite(soxl[3][k - 1]):  # the bar that just closed (end-stamped)
            md.get("SOXL").bars[t0] = Bar(t0, soxl[0][k - 1], soxl[1][k - 1], soxl[2][k - 1], soxl[3][k - 1], soxl[4][k - 1])
            await run(t0 + timedelta(seconds=3), lambda sp: sleeve.on_minute(t0 + timedelta(seconds=3), session["open"]))
        movers = list(fr) if k < 330 else [x for x in ("SOXL", "SOXS") if x in fr]
        pts = {}
        for sym in movers:
            o, h, l, c = (fr[sym][i][k] for i in range(4))
            if not np.isfinite(h):
                continue
            nm = sleeve.pre.names.get(sym)
            near = nm is not None and (nm.active() or (nm.st.level > 0 and l <= nm.st.level * 1.01))  # only where levels matter
            fine = (175 <= k < 330 and near) or (359 <= k <= 720 and sym in ("SOXL", "SOXS"))
            up = c >= o
            pts[sym] = path((o, l, h, c) if up else (o, h, l, c)) if fine and np.isfinite(o) else [c]
        steps = max((len(p) for p in pts.values()), default=1)
        for j in range(steps):
            now = t0 + timedelta(seconds=5 + 50 * j / max(1, steps - 1)) if steps > 1 else t0 + timedelta(seconds=30)
            for sym, p in pts.items():
                set_px(sym, p[min(len(p) - 1, int(j * len(p) / steps))])
            await run(now, lambda sp, now=now: sleeve.tick(now, session, pre_session, sp))
    pre_tr = [t for t in sleeve.st.trades if str(t.get("reason", "")).startswith("pre")]
    orb_tr = [t for t in sleeve.st.trades if not str(t.get("reason", "")).startswith("pre")]
    ret = lambda trs: sum(t["pnl"] for t in trs) / sum(t["qty"] * t["entry"] for t in trs) if trs else None  # noqa: E731
    pre_by = {sym: ret([t for t in pre_tr if t["side"] == sym]) for sym in {t["side"] for t in pre_tr}}
    return {"date": d, "orb_side": orb_tr[0]["side"] if orb_tr else None, "orb_ret": ret(orb_tr), "orb_reason": orb_tr[-1]["reason"] if orb_tr else "",
            "pre": pre_by, "regime": sleeve.st.regime, "orb_note": sleeve.st.note, "qqq_ok": sleeve.pre.regime.get("ok"),
            "refused": refused, "errors": errors}


def research_pre(pm: dict, up: pd.Series) -> dict[tuple, float]:
    """The book in the research: level fills, 3 slots in time order, QQQ filter -> {(date, sym): net}."""
    t = all_trades(pm, "level")
    order = {sym: i for i, sym in enumerate(NAMES)}
    t["o"] = t["sym"].map(order)
    out = {}
    for d, g in t.groupby("date"):
        if up.get(d) is not True and up.get(d) != 1:
            continue
        busy: list[int] = []
        for row in g.sort_values(["j", "o"]).itertuples():
            busy = [q for q in busy if q > row.j]
            if len(busy) >= SLOTS:
                continue
            busy.append(row.q)
            out[(d, row.sym)] = row.net
    return out


_CTX: tuple | None = None


def _replay_one(d: pd.Timestamp) -> dict:
    m, pm, daily = _CTX
    with tempfile.TemporaryDirectory() as tmp:
        J.dir = Path(tmp) / "journal"
        return asyncio.run(replay_day(d, m, pm, daily, Path(tmp)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true", help="fixed sample + thresholds; exit 1 on failure")
    ap.add_argument("--days", type=int, default=0, help="only the last N days (0 = all)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = ap.parse_args()
    logging.disable(logging.WARNING)
    m, days = R.load_matrices()
    pm = pre_matrices()
    up = qqq_up()
    q = pd.read_parquet(R.D / "alpaca_1m.parquet", columns=["symbol", "ts", "close"])
    q = q[q["symbol"] == "QQQ"]
    qe = q["ts"].dt.tz_convert("America/New_York")
    keep = (qe.dt.hour * 60 + qe.dt.minute) <= 959
    qqq_daily = q[keep].groupby(qe[keep].dt.tz_localize(None).dt.normalize())["close"].last()
    daily = {"SOXX": m[("SOXX", "close")][R.CLOSE_M], "QQQ": qqq_daily}
    for sym, M in pm.items():
        daily[sym] = M["close"][719]
    orb = R.e2_orb(m, days, 30, "opposite", 0.0)
    orb_r = orb[orb["regime"] == "bear"].set_index("date")
    pre_r = research_pre({sym: pm[sym] for sym in NAMES if sym in pm}, up)
    sample = days[60:]
    if args.gate:  # every research trade day in the last 2 years + every 10th other day
        recent = sample[sample >= sample[-1] - pd.Timedelta(days=2 * 365)]
        trade_days = set(orb_r.index) | {d for d, _ in pre_r}
        sample = pd.DatetimeIndex([d for i, d in enumerate(recent) if d in trade_days or i % 10 == 0])
    elif args.days:
        sample = sample[-args.days:]
    global _CTX
    _CTX = (m, pm, daily)
    ctx = multiprocessing.get_context("fork")  # workers share the loaded minute data
    with ctx.Pool(min(args.jobs, len(sample))) as pool:
        rows = pool.map(_replay_one, list(sample), chunksize=2)
    res = pd.DataFrame(rows).set_index("date")
    res["r_orb_side"] = orb_r["sym"].reindex(res.index)
    res["r_orb_ret"] = orb_r["net"].reindex(res.index)
    rd, ld = res["r_orb_side"].notna(), res["orb_side"].notna()
    both = rd & ld
    orb_match = (res.loc[rd, "orb_side"] == res.loc[rd, "r_orb_side"]).mean() if rd.any() else 1.0
    orb_extra = (ld & ~rd).mean()
    orb_gap = (res.loc[both, "orb_ret"] - res.loc[both, "r_orb_ret"]).mean() if both.any() else 0.0
    live_pairs = {(d, sym): r for d, row in res.iterrows() for sym, r in (row["pre"] or {}).items()}
    res_pairs = {k: v for k, v in pre_r.items() if k[0] in res.index}
    common = set(live_pairs) & set(res_pairs)
    union = set(live_pairs) | set(res_pairs)
    pre_match = len(common) / len(union) if union else 1.0
    pre_gap = float(np.mean([live_pairs[k] - res_pairs[k] for k in common])) if common else 0.0
    refused = sum(len(x) for x in res["refused"])
    errors = sum(len(x) for x in res["errors"])
    print(f"Replay of the live scalp code: {len(res)} days {res.index[0]:%Y-%m-%d} .. {res.index[-1]:%Y-%m-%d}")
    print(f"  breakout: research traded {rd.sum()} days, live {ld.sum()}; same side on {orb_match:.0%} of research days, "
          f"live-only days {orb_extra:.1%}; return gap live-research {orb_gap:+.3%}/trade "
          f"(live {res.loc[both, 'orb_ret'].mean():+.3%} vs research {res.loc[both, 'r_orb_ret'].mean():+.3%}, n {both.sum()})")
    print(f"  pre-market book ({len(NAMES)} names, {SLOTS} slots, QQQ filter): research {len(res_pairs)} trades, live {len(live_pairs)}; "
          f"same name+day on {pre_match:.0%} of all trades; return gap {pre_gap:+.3%} (n {len(common)}); "
          f"live avg {np.mean(list(live_pairs.values())) if live_pairs else 0:+.3%} vs research {np.mean(list(res_pairs.values())) if res_pairs else 0:+.3%}")
    print(f"  guard refusals {refused}, exceptions {errors}")
    only_l = sorted(set(live_pairs) - set(res_pairs))[:6]
    only_r = sorted(set(res_pairs) - set(live_pairs))[:6]
    if only_l or only_r:
        print("  book differences (first 6 each): live only " + ", ".join(f"{d:%Y-%m-%d} {sym}" for d, sym in only_l)
              + " | research only " + ", ".join(f"{d:%Y-%m-%d} {sym}" for d, sym in only_r))
    for x in [e for es in res["refused"] for e in es][:5] + [e for es in res["errors"] for e in es][:5]:
        print("    !", x)
    ok = (orb_match >= GATE["orb_match"] and orb_extra <= GATE["orb_extra"] and abs(orb_gap) <= GATE["orb_ret_gap"]
          and pre_match >= GATE["pre_match"] and abs(pre_gap) <= GATE["pre_ret_gap"] and errors == 0
          and refused <= GATE["refusal_share"] * max(1, len(live_pairs) + ld.sum()))
    print(f"  parity gate {'PASSED' if ok else 'FAILED'} (thresholds {GATE}, no exceptions)")
    return 0 if ok or not args.gate else 1


if __name__ == "__main__":
    sys.exit(main())
