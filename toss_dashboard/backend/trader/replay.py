"""Replay check: run the LIVE scalp code over past days and compare it with the research backtests.

Unit tests check pieces. This checks behaviour: the real ScalpSleeve / PremarketDip classes (with the
order guard switched on) trade historical SIP minute bars through a simulated clock and a paper
broker, using the same scheduling method the engine calls (ScalpSleeve.tick). Every day's result is
compared with what the validated research code did on the same day:
  breakout   research/scalp.py e2_orb (30-minute range, bear regime only)
  pre-market research/scalp_ext.py _dip_trade, "level" fill model
A change that passes the unit tests but trades differently (wrong side, window, size, stop, a guard
that blocks real trades, an exception) shows up as a parity drop, and the deploy is refused.

Inside each minute the price walks open -> low -> high -> close (open -> high -> low -> close on down
bars) in steps of ~0.25% during the minutes that matter (06:55-09:30 and 09:59-16:00 ET), like a live
stream crossing levels; elsewhere one tick per minute. bid/ask = price -/+ 1 cent. Fills follow the
research's rule: a buy priced at or above the ask (marketable) fills at the ask, a resting buy fills
at its limit only when the price trades 0.3% through it (and a resting sell 0.05% through its limit).
Prices are rescaled per day (SOXL ~$100, SOXS ~$50) so old split-adjusted prices size like today's.

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
from .research import scalp_ext as RX
from .scalp import ScalpSleeve, ScalpState
from .scalp_pre import PreState

ET = ZoneInfo("America/New_York")
CAPITAL = 10000.0
GATE = {"orb_match": 0.85, "orb_extra": 0.05, "orb_ret_gap": 0.0025, "pre_match": 0.75, "pre_ret_gap": 0.005}


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
    """SIP 1-minute bars of the replay day (start-stamped, like Alpaca)."""

    def __init__(self, bars: pd.DataFrame):
        self.bars_df = bars

    async def bars(self, symbols, timeframe, start, end, feed="sip", adjustment="raw"):
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        b = self.bars_df
        return b[(b["symbol"].isin(symbols)) & (b["ts"] >= s) & (b["ts"] < e)]


def day_frames(m: dict, d: pd.Timestamp) -> dict[str, np.ndarray]:
    out = {}
    for sym in ("SOXL", "SOXS"):
        o, h, l, c, v = (m[(sym, f)].loc[d].to_numpy(dtype=float) for f in ("open", "high", "low", "close", "volume"))
        ref = c[329] if np.isfinite(c[329]) else np.nanmedian(c)
        k = (100.0 if sym == "SOXL" else 50.0) / ref
        out[sym] = np.vstack([o * k, h * k, l * k, c * k, np.nan_to_num(v)])
    return out


async def replay_day(d: pd.Timestamp, m: dict, daily: dict, tmp: Path) -> dict:
    day = d.date()
    md = MarketData(client=None)
    fr = day_frames(m, d)
    base = datetime.combine(day, time(4, 0), ET)
    rows = []
    o, h, l, c, v = fr["SOXL"]
    for k in range(960):
        if np.isfinite(o[k]):
            rows.append({"symbol": "SOXL", "ts": pd.Timestamp(base + timedelta(minutes=k)).tz_convert("UTC"),
                         "open": o[k], "high": h[k], "low": l[k], "close": c[k], "volume": v[k]})
    md.alpaca = FakeAlpaca(pd.DataFrame(rows))
    clock = {"now": base}
    broker = ReplayBroker(md, 0.001, CAPITAL)
    broker.guard = OrderGuard(lambda: CAPITAL * 2, lambda s: (md.get(s).bid, md.get(s).ask), broker.open_orders, now=lambda: clock["now"])
    refused: list[str] = []
    broker.on_refuse = refused.append
    s = Settings(mode="paper", capital=0.0, swing_capital=0.0, scalp_capital=CAPITAL, scalp_position_frac=1.0, scalp_force_day="", scalp_pre=True)
    notes: list[str] = []
    sleeve = ScalpSleeve(s, broker, md, FakeToss(daily, day), lambda urgent: True, lambda msg, lvl=20: notes.append(msg))
    sleeve.path = tmp / "scalp.json"
    sleeve.st, sleeve.cash, sleeve.realized, sleeve.retry_seconds = ScalpState(), CAPITAL, 0.0, 0.0
    sleeve.pre.st = PreState()
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

    lo_, ho_, cl_ = fr["SOXL"][2], fr["SOXL"][1], fr["SOXL"][3]
    for k in range(0, 722):
        t0 = base + timedelta(minutes=k)
        if 331 <= k <= 720 and np.isfinite(cl_[k - 1]):  # the bar that just closed, end-stamped like Toss/MarketData
            end = t0
            md.get("SOXL").bars[end] = Bar(end, fr["SOXL"][0][k - 1], ho_[k - 1], lo_[k - 1], cl_[k - 1], fr["SOXL"][4][k - 1])
            await run(t0 + timedelta(seconds=3), lambda sp: sleeve.on_minute(t0 + timedelta(seconds=3), session["open"]))
        if not np.isfinite(fr["SOXL"][0][k]):
            await run(t0 + timedelta(seconds=30), lambda sp: sleeve.tick(t0 + timedelta(seconds=30), session, pre_session, sp))
            continue
        bo, bh, bl, bc = (fr["SOXL"][i][k] for i in range(4))
        so, sh, sl_, sc = (fr["SOXS"][i][k] for i in range(4))
        up = bc >= bo
        fine = 175 <= k < 330 or 359 <= k <= 720
        pts_l = path((bo, bl, bh, bc) if up else (bo, bh, bl, bc)) if fine else [bc]
        pts_s = path((so, sh, sl_, sc) if up else (so, sl_, sh, sc)) if fine else [sc]  # SOXS moves the other way
        n = len(pts_l)
        for j, pl in enumerate(pts_l):
            now = t0 + timedelta(seconds=5 + 50 * j / max(1, n - 1)) if n > 1 else t0 + timedelta(seconds=30)
            set_px("SOXL", pl)
            ps = pts_s[min(len(pts_s) - 1, int(j * len(pts_s) / n))]
            if np.isfinite(ps):
                set_px("SOXS", ps)
            await run(now, lambda sp, now=now: sleeve.tick(now, session, pre_session, sp))
    pre_tr = [t for t in sleeve.st.trades if str(t.get("reason", "")).startswith("pre")]
    orb_tr = [t for t in sleeve.st.trades if not str(t.get("reason", "")).startswith("pre")]
    ret = lambda trs: sum(t["pnl"] for t in trs) / sum(t["qty"] * t["entry"] for t in trs) if trs else None  # noqa: E731
    return {"date": d, "orb_side": orb_tr[0]["side"] if orb_tr else None, "orb_ret": ret(orb_tr), "orb_reason": orb_tr[-1]["reason"] if orb_tr else "",
            "pre_ret": ret(pre_tr), "regime": sleeve.st.regime, "orb_note": sleeve.st.note, "refused": refused, "errors": errors}


def research(m: dict, days: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.Series]:
    orb = R.e2_orb(m, days, 30, "opposite", 0.0)
    orb = orb[orb["regime"] == "bear"].set_index("date")
    C, L, H, V = (m[("SOXL", f)] for f in ("close", "low", "high", "volume"))
    sig = np.log(C[719]).diff().rolling(20).std().shift(1)
    pre = {}
    for d in days:
        s = sig.get(d)
        if not np.isfinite(s):
            continue
        c, lo, hi, v = (x.loc[d].to_numpy() for x in (C, L, H, V))
        first = np.flatnonzero(np.isfinite(lo[:60]))
        if len(first):
            r = RX._dip_trade(c, lo, hi, np.nan_to_num(v), first[0], min(0.06, max(0.015, 0.4 * s)), "level")
            if r is not None:
                pre[d] = r - 0.002  # same commission convention as the live paper broker (0.1%/side)
    return orb, pd.Series(pre)


_CTX: tuple | None = None


def _replay_one(d: pd.Timestamp) -> dict:
    m, daily = _CTX
    with tempfile.TemporaryDirectory() as tmp:
        J.dir = Path(tmp) / "journal"
        return asyncio.run(replay_day(d, m, daily, Path(tmp)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", action="store_true", help="fixed sample + thresholds; exit 1 on failure")
    ap.add_argument("--days", type=int, default=0, help="only the last N days (0 = all)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = ap.parse_args()
    logging.disable(logging.WARNING)
    m, days = R.load_matrices()
    daily = {"SOXX": m[("SOXX", "close")][R.CLOSE_M], "SOXL": m[("SOXL", "close")][R.CLOSE_M]}
    orb_r, pre_r = research(m, days)
    sample = days[60:]
    if args.gate:  # every research trade day in the last 3 years + every 10th other day
        recent = sample[sample >= sample[-1] - pd.Timedelta(days=3 * 365)]
        trade_days = set(orb_r.index) | set(pre_r.index)
        sample = pd.DatetimeIndex([d for i, d in enumerate(recent) if d in trade_days or i % 10 == 0])
    elif args.days:
        sample = sample[-args.days:]
    global _CTX
    _CTX = (m, daily)
    ctx = multiprocessing.get_context("fork")  # workers share the loaded minute data
    with ctx.Pool(min(args.jobs, len(sample))) as pool:
        rows = pool.map(_replay_one, list(sample), chunksize=4)
    res = pd.DataFrame(rows).set_index("date")
    res["r_orb_side"] = orb_r["sym"].reindex(res.index)
    res["r_orb_ret"] = orb_r["net"].reindex(res.index)
    res["r_pre_ret"] = pre_r.reindex(res.index)
    rd, ld = res["r_orb_side"].notna(), res["orb_side"].notna()
    both = rd & ld
    orb_match = (res.loc[rd, "orb_side"] == res.loc[rd, "r_orb_side"]).mean() if rd.any() else 1.0
    orb_extra = (ld & ~rd).mean()
    orb_gap = (res.loc[both, "orb_ret"] - res.loc[both, "r_orb_ret"]).mean() if both.any() else 0.0
    pr, pl = res["r_pre_ret"].notna(), res["pre_ret"].notna()
    pre_match = ((pr & pl) | (~pr & ~pl)).loc[pr | pl].mean() if (pr | pl).any() else 1.0
    pre_gap = (res.loc[pr & pl, "pre_ret"] - res.loc[pr & pl, "r_pre_ret"]).mean() if (pr & pl).any() else 0.0
    refused = sum(len(x) for x in res["refused"])
    errors = sum(len(x) for x in res["errors"])
    print(f"Replay of the live scalp code: {len(res)} days {res.index[0]:%Y-%m-%d} .. {res.index[-1]:%Y-%m-%d}")
    print(f"  breakout: research traded {rd.sum()} days, live {ld.sum()}; same side on {orb_match:.0%} of research days, "
          f"live-only days {orb_extra:.1%}; return gap live-research {orb_gap:+.3%}/trade "
          f"(live {res.loc[both, 'orb_ret'].mean():+.3%} vs research {res.loc[both, 'r_orb_ret'].mean():+.3%}, n {both.sum()})")
    print(f"  pre-market: research {pr.sum()} trades, live {pl.sum()}; agree on {pre_match:.0%} of days with a trade; "
          f"return gap {pre_gap:+.3%} (n {(pr & pl).sum()})")
    print(f"  guard refusals {refused}, exceptions {errors}")
    mism = res[(rd & (res["orb_side"] != res["r_orb_side"])) | (ld & ~rd) | (pr ^ pl)]
    if len(mism):
        print("  differences (first 15):")
        for d, r in mism.head(15).iterrows():
            print(f"    {d:%Y-%m-%d} breakout research {r['r_orb_side']} live {r['orb_side']} ({r['orb_note'] or r['orb_reason']}) | "
                  f"pre research {r['r_pre_ret'] if pd.notna(r['r_pre_ret']) else '-'} live {r['pre_ret'] if pd.notna(r['pre_ret']) else '-'}")
    for x in [e for es in res["refused"] for e in es][:5] + [e for es in res["errors"] for e in es][:5]:
        print("    !", x)
    ok = (orb_match >= GATE["orb_match"] and orb_extra <= GATE["orb_extra"] and abs(orb_gap) <= GATE["orb_ret_gap"]
          and pre_match >= GATE["pre_match"] and abs(pre_gap) <= GATE["pre_ret_gap"] and refused == 0 and errors == 0)
    print(f"  parity gate {'PASSED' if ok else 'FAILED'} (thresholds {GATE}, no refusals, no exceptions)")
    return 0 if ok or not args.gate else 1


if __name__ == "__main__":
    sys.exit(main())
