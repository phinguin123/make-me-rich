"""Keeps the scalp and swing pots at their target shares of the bot's money.

Targets come from TRADER_ALLOC_SCALP / TRADER_ALLOC_SWING (validated 30/70 in
trader/research/allocation_opt.py). The allocator moves cash between the two sleeves' ledgers;
it never trades by itself. The swing sleeve then trades to its new budget at its next rebalance
(told to use tight bands once), and the scalp sleeve simply sizes its next trades from its new pot.

When it runs:
  - targets changed (including the first switch): as soon as both sleeves are safe
  - yearly: on the first trading day of a new year, 09:26 ET to open + 5 min (after the pre-market
    trade is closed, before the swing rebalance at open + 6 min); later that day or the next days
    if the sleeves weren't safe then
"Safe" = the scalp sleeve holds nothing and has no orders working, and the swing sleeve isn't
trading. Only money a sleeve actually owns is counted: a sleeve that has never run (no state
file) counts as zero, so turning on a new sleeve can't double-count the account.

Targets that sum to less than 100% leave a cash reserve the bot never trades (tracked here so
it stays part of the total at the next rebalance).

Glide path (optional): phases [(threshold, targets)] on the bot's money. The bot moves to a later
(safer) phase once its money reaches that phase's threshold and never steps back by itself.

Money in / out: deposit(amount) adds (or, negative, removes) money from the bot's books; it is spread
over the sleeves at the next safe moment (the account must actually hold it).

Drawdown circuit breaker: if scalp + swing + reserve falls `halt_dd` below its high-water mark,
new entries are paused (scalp entries and swing buys; exits and swing sells still run) and a
warning is raised. Pressing Resume on the dashboard acknowledges it and resets the high-water
mark to the current value.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .journal import jr

ET = ZoneInfo("America/New_York")


class Allocator:
    def __init__(self, targets: dict[str, float], scalp, swing, broker, path: Path, log: Callable[[str, int], None],
                 yearly: bool = True, halt_dd: float = 0.0, on_breach: Callable[[str], None] | None = None,
                 is_paused: Callable[[], bool] = lambda: False, phases: list[tuple[float, dict]] | None = None):
        self.targets = {k: round(float(v), 4) for k, v in targets.items()}
        self.phases = sorted(phases or [], key=lambda p: p[0])
        self.scalp, self.swing, self.broker, self.path, self.log, self.yearly = scalp, swing, broker, path, log, yearly
        self.halt_dd, self.on_breach, self.is_paused = halt_dd, on_breach, is_paused
        self.state: dict = {"targets": {}, "last": "", "history": [], "reserve": 0.0, "hwm": 0.0, "breached": False}
        if path.exists():
            self.state.update(json.loads(path.read_text()))
        self.state.setdefault("pending", 0.0)
        self.state.setdefault("phase", 0)
        if self.phases:
            self.targets = self._phase_targets(int(self.state["phase"]))
        self.busy = False
        self.note = ""
        self._px_at: datetime | None = None  # last swing price refresh (the breaker needs current values)
        self._retry_at: datetime | None = None  # after a postponed rebalance

    @staticmethod
    def parse_phases(spec: str) -> list[tuple[float, dict]]:
        """"0:0.50/0.50,50000:0.30/0.70,100000:0.20/0.50" -> [(0, {scalp, swing}), ...]"""
        out = []
        for part in [p for p in spec.replace(" ", "").split(",") if p]:
            thr, mix = part.split(":")
            sc, sw = mix.split("/")
            out.append((float(thr), {"scalp": float(sc), "swing": float(sw)}))
        return out

    def _phase_targets(self, i: int) -> dict:
        return {k: round(float(v), 4) for k, v in self.phases[min(i, len(self.phases) - 1)][1].items()}

    def update_phase(self) -> None:
        if not self.phases or not self.state.get("last"):
            return
        total = self.total() + float(self.state.get("pending", 0.0))
        reached = max(i for i, (thr, _) in enumerate(self.phases) if total >= thr or i == 0)
        if reached > int(self.state.get("phase", 0)):  # one-way: only towards safer phases
            self.state["phase"] = reached
            self.targets = self._phase_targets(reached)
            jr("D", "alloc", "phase", {"phase": reached + 1, "total": total})
            self.log(f"Glide path: bot money ${total:,.0f} reached ${self.phases[reached][0]:,.0f} -> phase {reached + 1}: "
                     f"scalp {self.targets['scalp']:.0%} / swing {self.targets['swing']:.0%}", logging.WARNING)
            self.save()

    def deposit(self, amount: float) -> None:
        """Add (positive) or remove (negative) money from the bot's books; spread at the next safe moment."""
        self.state["pending"] = round(float(self.state.get("pending", 0.0)) + float(amount), 2)
        self.note = ""
        self._retry_at = None
        self.log(f"Allocator: ${amount:+,.2f} {'added to' if amount >= 0 else 'removed from'} the bot "
                 f"(pending ${self.state['pending']:+,.2f}, applied when the sleeves are flat)", logging.INFO)
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.path)

    # ── scheduling ────────────────────────────────────────────────────────
    def due(self, today: date) -> str:
        if abs(float(self.state.get("pending", 0.0))) >= 0.01:
            return "deposit" if self.state["pending"] > 0 else "withdrawal"
        if self.state.get("targets") != self.targets:
            return "targets changed"
        last = self.state.get("last") or ""
        if self.yearly and last and int(last[:4]) < today.year:
            return "yearly"
        return ""

    def safe(self) -> str:
        """Empty string if money can move now, else the reason it can't."""
        sc = self.scalp
        if sc.st.qty > 0 or sc.busy:
            return "scalp holds a position"
        if sc.pre and (sc.pre.holding() or sc.pre.any_busy):
            return "pre-market trade open"
        if sc.relay_busy():
            return "stocks-in-play position open (relay)"
        if self.swing.busy:
            return "swing is trading"
        if any(o.tag.startswith(("scalp", "swing", "relay")) for o in self.broker.open_orders()):
            return "orders working"
        return ""

    def total(self) -> float:
        return self.scalp.equity() + self.swing.value() + float(self.state.get("reserve", 0.0))

    def check_drawdown(self) -> None:
        if not self.halt_dd or not self.state.get("last"):
            return
        total = self.total()
        if self.state.get("breached"):
            if not self.is_paused():  # the user pressed Resume: accept the loss, start a new high-water mark
                self.state.update(breached=False, hwm=total)
                self.log(f"Drawdown breaker reset by Resume; new high-water mark ${total:,.2f}", logging.WARNING)
                self.save()
            return
        if total > self.state.get("hwm", 0.0):
            self.state["hwm"] = total
            return
        hwm = self.state["hwm"]
        if hwm and total < hwm * (1 - self.halt_dd):
            self.state["breached"] = True
            self.save()
            jr("D", "alloc", "breaker", {"total": total, "hwm": hwm, "dd": total / hwm - 1})
            msg = (f"Account drawdown breaker: bot money ${total:,.0f} is {total / hwm - 1:.1%} below its high of ${hwm:,.0f} "
                   f"(limit {self.halt_dd:.0%}). New entries paused; exits still run. Review, then press Resume.")
            self.log(msg, logging.ERROR)
            if self.on_breach:
                self.on_breach(msg)

    def tick(self, now: datetime, regular_open: datetime, spawn: Callable) -> None:
        if self.busy:
            return
        if (self.swing.holdings and regular_open <= now <= regular_open + timedelta(hours=6, minutes=35)
                and (self._px_at is None or (now - self._px_at).total_seconds() >= 900)):
            self._px_at = now
            spawn(self._refresh_prices())
        self.check_drawdown()
        self.update_phase()
        reason = self.due(now.date())
        if not reason:
            self.note = ""
            return
        if self._retry_at and now < self._retry_at:
            return
        if reason == "yearly" and now.time() < time(9, 26):
            self.note = "yearly rebalance due today at 09:26 ET"
            return
        blocked = self.safe()
        if blocked:
            self.note = f"{reason}: waiting ({blocked})"
            return
        self.busy = True
        spawn(self._guard(self.run(now, reason)))

    async def _refresh_prices(self) -> None:
        try:
            self.swing.last_prices.update(await self.swing.prices(sorted(self.swing.holdings)))
        except Exception as exc:  # noqa: BLE001 — cosmetic for one cycle; retried in 15 minutes
            self.log(f"Allocator: swing price refresh failed ({exc!r})", logging.WARNING)

    async def _guard(self, coro) -> None:
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            self.log(f"Allocator error {exc!r}", logging.ERROR)
        finally:
            self.busy = False

    # ── the move ──────────────────────────────────────────────────────────
    async def run(self, now: datetime, reason: str) -> None:
        sc, sw = self.scalp, self.swing
        if sw.holdings:
            sw.last_prices.update(await sw.prices(sorted(sw.holdings)))
        if self.safe():  # re-check after the await
            return
        owned = {"scalp": 0.0 if sc.fresh else sc.equity(), "swing": 0.0 if sw.fresh else sw.value(),
                 "reserve": float(self.state.get("reserve", 0.0))}
        pending = float(self.state.get("pending", 0.0))
        total = max(0.0, sum(owned.values()) + pending)
        if total <= 0:  # very first run of both sleeves: start from the configured pots
            total = sc.cash + sw.cash
        try:  # the account must actually hold the money the sleeves own
            ceiling = await self.broker.cash() + sum(q * sw.last_prices.get(s, 0.0) for s, q in sw.holdings.items())
        except Exception:  # noqa: BLE001 — can't check now; try again later
            ceiling = None
        if self.broker.name == "live" and (ceiling is None or total > ceiling * 1.01):
            # Money in use outside the bot (e.g. a manual trade). Never shrink the sleeves' books for that:
            # wait until the cash is back. The sleeves cap every order by real buying power meanwhile.
            msg = (f"{reason}: waiting, the bot owns ${total:,.0f} but only ${ceiling or 0:,.0f} is free in the account "
                   "(money in use outside the bot)")
            if self.note != msg:
                jr("D", "alloc", "wait", {"reason": reason, "owned": total, "free": ceiling})
                self.log(f"Allocator {msg}", logging.WARNING)
            self.note = msg
            self._retry_at = now + timedelta(minutes=5)
            return
        tgt_scalp = round(total * self.targets.get("scalp", 0.0), 2)
        tgt_swing = round(total * self.targets.get("swing", 0.0), 2)
        before = (sc.cash, owned["swing"])
        if sw.fresh:
            sw.cash = tgt_swing
        else:  # holdings stay; cash moves so the sleeve's total value equals its target
            sw.cash = round(sw.cash + tgt_swing - owned["swing"], 2)
        sc.cash = tgt_scalp  # the scalp sleeve is flat here, so its value is all cash
        sc.fresh = sw.fresh = False
        sw.force_next = True  # trade to the new budget with tight bands at the next rebalance
        sc.save()
        sw.save()
        entry = {"date": now.date().isoformat(), "reason": reason, "total": round(total, 2),
                 "scalp": tgt_scalp, "swing": tgt_swing, "reserve": round(total - tgt_scalp - tgt_swing, 2), "targets": self.targets}
        self.state["targets"], self.state["last"] = dict(self.targets), now.date().isoformat()
        self.state["reserve"] = round(total - tgt_scalp - tgt_swing, 2)
        self.state["pending"] = 0.0
        if pending:  # deposits move the high-water mark too: new money isn't a gain
            self.state["hwm"] = max(0.0, float(self.state.get("hwm", 0.0)) + pending)
        self.state["hwm"] = max(float(self.state.get("hwm", 0.0)), total)
        self.state.setdefault("history", []).append(entry)
        jr("D", "alloc", "run", {k: v for k, v in entry.items() if k != "date"})
        self.save()
        self.note = ""
        self.log(f"Allocator ({reason}): ${total:,.2f} -> scalp ${tgt_scalp:,.2f} ({self.targets.get('scalp', 0):.0%}) / "
                 f"swing ${tgt_swing:,.2f} ({self.targets.get('swing', 0):.0%}) / reserve ${self.state['reserve']:,.2f}; was scalp ${before[0]:,.2f}, "
                 f"swing ${before[1]:,.2f}", logging.INFO)

    def snapshot(self) -> dict:
        sc_val = self.scalp.equity()
        sw_val = self.swing.value()
        reserve = float(self.state.get("reserve", 0.0))
        total = sc_val + sw_val + reserve
        hwm = float(self.state.get("hwm", 0.0))
        last = self.state.get("last") or ""
        phase = int(self.state.get("phase", 0))
        nxt = self.phases[phase + 1][0] if self.phases and phase + 1 < len(self.phases) else None
        return {"enabled": True, "targets": self.targets, "last": last, "pending": float(self.state.get("pending", 0.0)),
                "phase": ({"index": phase + 1, "count": len(self.phases), "next_at": nxt,
                           "plan": [{"from": thr, **mix} for thr, mix in self.phases]} if self.phases else None),
                "next": f"first trading day of {int(last[:4]) + 1}" if last and self.yearly else "",
                "current": {"scalp": sc_val, "swing": sw_val, "reserve": reserve, "total": total,
                            "scalp_share": sc_val / total if total else 0.0, "swing_share": sw_val / total if total else 0.0,
                            "reserve_share": reserve / total if total else 0.0},
                "breaker": {"limit": self.halt_dd, "hwm": hwm, "drawdown": total / hwm - 1 if hwm else 0.0,
                            "breached": bool(self.state.get("breached"))},
                "note": self.note, "history": self.state.get("history", [])[-10:]}
