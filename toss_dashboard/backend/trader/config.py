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
    # Pre-market edge of the scalp sleeve (SOXL dips below the pre-market VWAP, 07:00-09:25 ET). 0 disables it.
    scalp_pre: bool = field(default_factory=lambda: _env("TRADER_SCALP_PRE", "1") != "0")

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
