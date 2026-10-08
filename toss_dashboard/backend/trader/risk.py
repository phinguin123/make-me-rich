"""Position sizing, loss limits and order throttling.

Toss flags accounts that fire many small orders in a short time ("hundreds of
small orders within tens of minutes") and restricts trading. Every order action
(create / modify / cancel) passes through `allow_order`.
"""
from __future__ import annotations

import logging
import math
import time
from collections import deque
from dataclasses import dataclass

from .config import Settings

LOG = logging.getLogger(__name__)


@dataclass
class RiskStatus:
    halted: bool = False
    reason: str = ""


class RiskManager:
    def __init__(self, settings: Settings):
        self.s = settings
        self._orders_10m: deque[float] = deque()
        self.orders_today = 0
        self.day_start_equity = settings.capital
        self.status = RiskStatus()

    def new_day(self, equity: float) -> None:
        self.day_start_equity = equity
        self.orders_today = 0
        self._orders_10m.clear()
        if self.status.reason.startswith("daily"):
            self.status = RiskStatus()

    def allow_order(self, urgent: bool = False) -> bool:
        """Exits pass `urgent=True`: they are throttled only by the hard daily cap."""
        now = time.monotonic()
        while self._orders_10m and now - self._orders_10m[0] > 600:
            self._orders_10m.popleft()
        if self.orders_today >= self.s.max_orders_per_day * (1.5 if urgent else 1):
            LOG.error("Daily order cap reached (%d)", self.orders_today)
            return False
        if not urgent and len(self._orders_10m) >= self.s.max_orders_per_10min:
            LOG.warning("Order throttle: %d orders in the last 10 minutes", len(self._orders_10m))
            return False
        self._orders_10m.append(now)
        self.orders_today += 1
        return True

    def check_daily_loss(self, equity: float) -> None:
        drawdown = equity / self.day_start_equity - 1 if self.day_start_equity else 0.0
        if not self.status.halted and drawdown <= -self.s.daily_loss_limit:
            self.status = RiskStatus(True, f"daily loss limit hit ({drawdown:.2%})")
            LOG.error("RISK HALT: %s — no new entries today", self.status.reason)

    def shares_for(self, equity: float, cash: float, entry: float, stop: float, commission: float) -> int:
        risk = entry - stop
        if risk <= 0 or entry <= 0:
            return 0
        by_risk = self.s.risk_per_trade * equity / risk
        by_size = self.s.max_position_pct * equity / entry
        by_cash = cash / (entry * (1 + commission))
        return max(0, math.floor(min(by_risk, by_size, by_cash)))
