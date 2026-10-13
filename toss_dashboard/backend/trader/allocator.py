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
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class Allocator:
    def __init__(self, targets: dict[str, float], scalp, swing, broker, path: Path, log: Callable[[str, int], None],
                 yearly: bool = True):
        self.targets = {k: round(float(v), 4) for k, v in targets.items()}
        self.scalp, self.swing, self.broker, self.path, self.log, self.yearly = scalp, swing, broker, path, log, yearly
        self.state: dict = {"targets": {}, "last": "", "history": []}
        if path.exists():
            self.state = json.loads(path.read_text())
        self.busy = False
        self.note = ""

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.path)

    # ── scheduling ────────────────────────────────────────────────────────
    def due(self, today: date) -> str:
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
        if sc.pre and (sc.pre.st.qty > 0 or sc.pre.busy or sc.pre.st.status == "entry_open"):
            return "pre-market trade open"
        if self.swing.busy:
            return "swing is trading"
        if any(o.tag.startswith(("scalp", "swing")) for o in self.broker.open_orders()):
            return "orders working"
        return ""

    def tick(self, now: datetime, regular_open: datetime, spawn: Callable) -> None:
        if self.busy:
            return
        reason = self.due(now.date())
        if not reason:
            self.note = ""
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
        owned = {"scalp": 0.0 if sc.fresh else sc.equity(), "swing": 0.0 if sw.fresh else sw.value()}
        total = sum(owned.values())
        if total <= 0:  # very first run of both sleeves: start from the configured pots
            total = sc.cash + sw.cash
        try:  # never allocate more than the account can actually pay for
            ceiling = await self.broker.cash() + sum(q * sw.last_prices.get(s, 0.0) for s, q in sw.holdings.items())
            if self.broker.name == "live" and total > ceiling * 1.01:
                self.log(f"Allocator: sleeves claim ${total:,.0f} but the account has ${ceiling:,.0f}; using the account value", logging.WARNING)
                total = ceiling
        except Exception:  # noqa: BLE001 — keep the ledger total
            pass
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
                 "scalp": tgt_scalp, "swing": tgt_swing, "targets": self.targets}
        self.state["targets"], self.state["last"] = dict(self.targets), now.date().isoformat()
        self.state.setdefault("history", []).append(entry)
        self.save()
        self.note = ""
        self.log(f"Allocator ({reason}): ${total:,.2f} -> scalp ${tgt_scalp:,.2f} ({self.targets.get('scalp', 0):.0%}) / "
                 f"swing ${tgt_swing:,.2f} ({self.targets.get('swing', 0):.0%}); was scalp ${before[0]:,.2f}, "
                 f"swing ${before[1]:,.2f}", logging.INFO)

    def snapshot(self) -> dict:
        sc_val = self.scalp.equity()
        sw_val = self.swing.value()
        total = sc_val + sw_val
        last = self.state.get("last") or ""
        return {"enabled": True, "targets": self.targets, "last": last,
                "next": f"first trading day of {int(last[:4]) + 1}" if last and self.yearly else "",
                "current": {"scalp": sc_val, "swing": sw_val, "total": total,
                            "scalp_share": sc_val / total if total else 0.0, "swing_share": sw_val / total if total else 0.0},
                "note": self.note, "history": self.state.get("history", [])[-10:]}
