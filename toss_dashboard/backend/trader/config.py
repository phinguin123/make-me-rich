"""Runtime settings, read once from the repo-root .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parents[1]
DATA_DIR = Path(os.getenv("TRADER_DATA_DIR", BACKEND_DIR / "data"))

load_dotenv(REPO_ROOT / ".env")
load_dotenv(BACKEND_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    toss_client_id: str = field(default_factory=lambda: _env("TOSS_CLIENT_ID"))
    toss_client_secret: str = field(default_factory=lambda: _env("TOSS_CLIENT_SECRET"))
    toss_account_seq: str = field(default_factory=lambda: _env("TOSS_ACCOUNT_SEQ", "1").split()[0])
    alpaca_key: str = field(default_factory=lambda: _env("ALPACA_API_KEY"))
    alpaca_secret: str = field(default_factory=lambda: _env("ALPACA_API_SECRET"))
    # Alpaca data plan: "sip" (Algo Trader Plus: real-time full market + stream), "iex" (free), "off"
    alpaca_feed: str = field(default_factory=lambda: _env("TRADER_ALPACA_FEED", "sip").lower())

    # "paper" simulates fills locally on live Toss data; "live" sends real orders.
    mode: str = field(default_factory=lambda: _env("TRADER_MODE", "paper").lower())
    # Capital the bot may use (USD). It never uses more than Toss cash buying power.
    capital: float = field(default_factory=lambda: _float("TRADER_CAPITAL", 3000.0))
    # Swing sleeve (monthly momentum, weeks-to-months holds). 0 disables it.
    swing_capital: float = field(default_factory=lambda: _float("TRADER_SWING_CAPITAL", 0.0))
    swing_hedge: str = field(default_factory=lambda: _env("TRADER_SWING_HEDGE", "none").lower())  # none | soxs_broad
    # SOXL/SOXS scalp sleeve (regime-gated opening-range breakout). 0 disables it.
    scalp_capital: float = field(default_factory=lambda: _float("TRADER_SCALP_CAPITAL", 0.0))
    # Share of the scalp sleeve per trade: 0.5 validated (-19% max DD); 1.0 doubles return and drawdown.
    scalp_position_frac: float = field(default_factory=lambda: _float("TRADER_SCALP_POSITION_FRAC", 0.5))
    # One-day override (ET date, YYYY-MM-DD): trade that session even when the regime says stand aside.
    scalp_force_day: str = field(default_factory=lambda: _env("TRADER_SCALP_FORCE_DAY"))
    # Pre-market edge of the scalp sleeve (dips below the pre-market VWAP, buys 04:30-09:00 ET, out by 10:30). 0 disables it.
    scalp_pre: bool = field(default_factory=lambda: _env("TRADER_SCALP_PRE", "1") != "0")
    # Names of the pre-market dip book (research/pre_multi.py, pre_slots.py), its slots and the QQQ trend filter
    pre_names: tuple = field(default_factory=lambda: tuple(s.strip().upper() for s in _env(
        "TRADER_PRE_NAMES", "SOXL,SOXS,NVDA,MU,AMD,TSLA,MSTR,PLTR,COIN,META,AAPL,AMZN,TSLL,NVDL").split(",") if s.strip()))
    pre_slots: int = field(default_factory=lambda: int(_float("TRADER_PRE_SLOTS", 3)))
    pre_qqq_filter: bool = field(default_factory=lambda: _env("TRADER_PRE_QQQ_FILTER", "1") != "0")
    # Allocation between the scalp and swing sleeves (shares of their combined money; 0/0 = off).
    # 30/70 is the validated mix (research/allocation_opt.py). The capitals above seed the very first run.
    alloc_scalp: float = field(default_factory=lambda: _float("TRADER_ALLOC_SCALP", 0.0))
    alloc_swing: float = field(default_factory=lambda: _float("TRADER_ALLOC_SWING", 0.0))
    alloc_rebalance: str = field(default_factory=lambda: _env("TRADER_ALLOC_REBALANCE", "yearly").lower())  # yearly | off
    # Glide path (overrides the fixed shares above): "threshold:scalp/swing,..." on bot money, one-way.
    # Live plan (research/goal.py, corrected): 30/70 under $50k, 25/75 to $100k, 20/50 + 30% cash above.
    alloc_phases: str = field(default_factory=lambda: _env("TRADER_ALLOC_PHASES"))
    # Account drawdown breaker: pause new entries when scalp + swing + reserve is this far below its peak (0 = off)
    account_dd_halt: float = field(default_factory=lambda: _float("TRADER_ACCOUNT_DD_HALT", 0.20))

    # Intraday relay: on semis-uptrend days (the SOXL/SOXS breakout stands aside) the stocks-in-play breakout
    # trades the scalp pot's free cash, after the pre-market book (research/intraday_relay.py). It needs no
    # TRADER_CAPITAL of its own: its positions, cash and P&L live inside the scalp pot.
    relay: bool = field(default_factory=lambda: _bool("TRADER_RELAY", False))
    relay_regime: str = field(default_factory=lambda: _env("TRADER_RELAY_REGIME", "risk_on"))  # none | risk_on
    relay_risk_per_trade: float = field(default_factory=lambda: _float("TRADER_RELAY_RISK", 0.01))  # of the scalp pot
    relay_max_position_pct: float = field(default_factory=lambda: _float("TRADER_RELAY_MAX_POSITION_PCT", 0.35))  # of the pot

    # Risk
    risk_per_trade: float = field(default_factory=lambda: _float("TRADER_RISK_PER_TRADE", 0.01))
    max_positions: int = field(default_factory=lambda: _int("TRADER_MAX_POSITIONS", 4))
    max_position_pct: float = field(default_factory=lambda: _float("TRADER_MAX_POSITION_PCT", 0.25))
    daily_loss_limit: float = field(default_factory=lambda: _float("TRADER_DAILY_LOSS_LIMIT", 0.02))
    max_orders_per_10min: int = field(default_factory=lambda: _int("TRADER_MAX_ORDERS_10MIN", 20))
    max_orders_per_day: int = field(default_factory=lambda: _int("TRADER_MAX_ORDERS_DAY", 120))
    hold_overnight: bool = field(default_factory=lambda: _bool("TRADER_HOLD_OVERNIGHT", True))
    use_server_stops: bool = field(default_factory=lambda: _bool("TRADER_SERVER_STOPS", True))

    commission_rate: float = field(default_factory=lambda: _float("TRADER_COMMISSION", 0.001))
    # Entries pause automatically if Toss reports a higher US commission than this.
    max_commission: float = field(default_factory=lambda: _float("TRADER_MAX_COMMISSION", 0.0012))
    port: int = field(default_factory=lambda: _int("TOSS_DASHBOARD_PORT", 5050))

    @property
    def live(self) -> bool:
        return self.mode == "live"


SETTINGS = Settings()
