"""Order guard: hard limits checked on every order before it reaches the broker.

Unit tests and the replay check catch most mistakes; this catches the rest at run time. Whatever a
strategy (or a bug in one) asks for, an order that breaks one of these rules is refused, logged as
an error and recorded in the journal (`X ... REJECTED guard: ...`):

  1. symbols    scalp only its own names (SOXL/SOXS + the pre-market book); swing/intraday never leveraged or
                inverse ETFs (SOXS hedge excepted)
  2. size       one buy may not exceed a share of the bot's money: scalp 60%, swing 25%, intraday 35%
  3. price      a buy limit more than 10% above the market, or a sell limit more than 10% below it, is refused
                (absurd prices from a bug; a limit a few % past the market is normal after a spike and fills at
                the market price anyway)
  4. windows    scalp buys only 07:00-09:01 ET (pre-market dip) and 09:59-11:31 ET (breakout entries)
  5. counts     at most 30 scalp, 60 swing and 100 intraday orders per day
  6. doubles    no second scalp buy of the same name while one is still working, and at most 3 working scalp buys
Sells for exits are never blocked by rules 2, 4 and 5: getting out must always be possible.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from typing import Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
LEVERAGED = {"SOXL", "SOXS", "TQQQ", "SQQQ", "UPRO", "SPXU", "SPXL", "SPXS", "TECL", "TECS", "LABU", "LABD", "FAS", "FAZ",
             "TNA", "TZA", "NVDL", "NVDU", "TSLL", "TSLQ", "KORU", "YINN", "YANG", "UVXY", "SVXY", "UDOW", "SDOW",
             "FNGU", "FNGD", "BULZ", "BERZ", "USD", "SSG", "QLD", "QID", "SSO", "SDS"}
MAX_SHARE = {"scalp": 0.60, "swing": 0.25, "intraday": 0.35}
MAX_ORDERS = {"scalp": 30, "swing": 60, "intraday": 100}
SCALP_BUY_WINDOWS = ((time(7, 0), time(9, 1)), (time(9, 59), time(11, 31)))


def sleeve_of(tag: str) -> str:
    return "scalp" if tag.startswith("scalp") else "swing" if tag.startswith("swing") else "intraday"


class OrderGuard:
    def __init__(self, bot_money: Callable[[], float], quote: Callable[[str], tuple[float, float]],
                 open_orders: Callable[[], list], now: Callable[[], datetime] = lambda: datetime.now(ET),
                 scalp_symbols: set[str] | frozenset = frozenset({"SOXL", "SOXS"})):
        self.bot_money, self.quote, self.open_orders, self.now = bot_money, quote, open_orders, now
        self.scalp_symbols = set(scalp_symbols)
        self.counts: Counter = Counter()
        self._day: date | None = None

    def check(self, symbol: str, side: str, qty: float, limit: float | None, tag: str, amount: float | None = None) -> str:
        """Empty string = allowed, else the reason it is refused."""
        now = self.now()
        if self._day != now.date():
            self._day, self.counts = now.date(), Counter()
        sl = sleeve_of(tag)
        buy = side == "BUY"
        if sl == "scalp" and symbol not in self.scalp_symbols:
            return f"scalp may only trade {'/'.join(sorted(self.scalp_symbols))}, not {symbol}"
        if sl != "scalp" and symbol in LEVERAGED and not (sl == "swing" and symbol == "SOXS"):
            return f"{sl} may not trade the leveraged/inverse ETF {symbol}"
        bid, ask = self.quote(symbol)
        mid = (bid + ask) / 2 if bid and ask else 0.0
        if limit and mid:
            if buy and limit > mid * 1.10:
                return f"buy limit {limit} is {limit / mid - 1:+.1%} above the market {mid:.2f}"
            if not buy and limit < mid * 0.90:
                return f"sell limit {limit} is {limit / mid - 1:+.1%} below the market {mid:.2f}"
        if buy:
            px = limit or ask or mid
            notional = amount if amount is not None else qty * (px or 0.0)
            money = self.bot_money()
            if money > 0 and notional > money * MAX_SHARE[sl] + 1:
                return f"{sl} buy of ${notional:,.0f} exceeds {MAX_SHARE[sl]:.0%} of the bot's ${money:,.0f}"
            if sl == "scalp":
                t = now.astimezone(ET).time()
                if not any(a <= t < b for a, b in SCALP_BUY_WINDOWS):
                    return f"scalp buys are only allowed 07:00-09:01 and 09:59-11:31 ET (now {t:%H:%M})"
                working = [o for o in self.open_orders() if o.side == "BUY" and o.tag.startswith("scalp")]
                if any(o.symbol == symbol for o in working):
                    return f"a scalp buy of {symbol} is already working"
                if len(working) >= 3:
                    return "3 scalp buys are already working"
            if self.counts[sl] >= MAX_ORDERS[sl]:
                return f"{sl} already sent {self.counts[sl]} orders today (limit {MAX_ORDERS[sl]})"
        self.counts[sl] += 1
        return ""
