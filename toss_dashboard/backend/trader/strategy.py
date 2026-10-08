"""Stocks-in-play opening-range breakout, long only.

Rules (identical to trader/research/backtest.py, which validated them):
  - Universe: today's top stocks by opening relative volume (see scanner.py).
  - Setup: the first `or_minutes` candle must be green (close > open).
  - Entry: price trades above the opening-range high (+1c) before `entry_cutoff`,
    and QQQ is above its session VWAP when `regime="qqq_vwap"`.
  - Stop: entry - stop_atr * ATR14 (or OR low). Trailing: highest completed-bar
    high - trail_atr * ATR14, never lowered.
  - Exit: stop / trail; otherwise at the close, unless `hold_strong` and the
    trade is up >= 1R closing in the top 20% of its day range — then it is held
    overnight with the stop raised to breakeven and exited by next day's close.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, time

from .config import DATA_DIR

CONFIG_PATH = DATA_DIR / "strategy.json"


@dataclass
class StrategyConfig:
    # "Safe" profile, validated 2024-01..2026-10 at Toss's 0.1% commission
    # (trader/research/backtest.py): +26% CAGR, max drawdown -4.8%, profitable every year.
    # Bigger-swing profile: regime="none", min_atr_pct=0.05, TRADER_MAX_POSITION_PCT=0.35.
    or_minutes: int = 5
    top_n: int = 30
    require_green: bool = True
    min_atr_pct: float = 0.06  # only volatile names: fees are a small fraction of their moves
    require_trend: bool = True  # previous close above its 50-day average
    min_gap: float = -1.0
    stop_mode: str = "atr"
    stop_atr: float = 0.10
    trail_atr: float = 0.75
    target_r: float = 0.0
    exit_mode: str = "hold_strong"
    regime: str = "risk_on"  # trade only while QQQ > VWAP and VIXY is below its open
    entry_cutoff: str = "11:30"
    max_chase: float = 0.004  # never pay more than 0.4% above the trigger
    max_spread: float = 0.005  # skip entries when the quoted spread is wider than this

    @property
    def cutoff(self) -> time:
        return time.fromisoformat(self.entry_cutoff)

    @classmethod
    def load(cls) -> "StrategyConfig":
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text())
            return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        return cls()

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))


@dataclass
class Setup:
    """Per-candidate intraday state."""

    symbol: str
    atr: float
    rvol: float
    prev_close: float = 0.0
    sma50: float = 0.0
    state: str = "WAIT_OR"  # WAIT_OR -> ARMED -> ENTERING -> DONE | SKIPPED
    or_high: float = 0.0
    or_low: float = 0.0
    trigger: float = 0.0
    note: str = ""

    def as_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass
class Position:
    symbol: str
    qty: float
    entry: float
    stop: float
    initial_stop: float
    atr: float
    opened: str
    highest: float = 0.0
    overnight: bool = False
    server_stop_id: str | None = None
    exiting: bool = False
    last_bar_end: str = ""
    realized_on: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def risk(self) -> float:
        return self.entry - self.initial_stop

    def as_dict(self) -> dict:
        return asdict(self)


def arm(cfg: StrategyConfig, setup: Setup, opening_range: tuple[float, float, float, float]) -> None:
    o, h, l, c = opening_range
    setup.or_high, setup.or_low = h, l
    skip = ""
    if cfg.require_green and not c > o:
        skip = "opening range red"
    elif setup.prev_close and setup.atr / setup.prev_close < cfg.min_atr_pct:
        skip = f"ATR {setup.atr / setup.prev_close:.1%} < {cfg.min_atr_pct:.0%}"
    elif cfg.require_trend and not setup.prev_close > setup.sma50:
        skip = "below 50-day average"
    elif setup.prev_close and o / setup.prev_close - 1 < cfg.min_gap:
        skip = f"gap {o / setup.prev_close - 1:.1%} < {cfg.min_gap:.0%}"
    if skip:
        setup.state, setup.note = "SKIPPED", skip
        return
    setup.trigger = round(h + 0.01, 2)
    setup.state = "ARMED"


def initial_stop(cfg: StrategyConfig, setup: Setup, entry: float) -> float:
    stop = entry - cfg.stop_atr * setup.atr if cfg.stop_mode == "atr" else setup.or_low - 0.01
    return round(min(stop, entry - 0.01), 2)


def update_trail(cfg: StrategyConfig, pos: Position, bar_high: float) -> None:
    pos.highest = max(pos.highest, bar_high)
    if cfg.trail_atr:
        pos.stop = max(pos.stop, round(pos.highest - cfg.trail_atr * pos.atr, 2))


def hold_overnight(cfg: StrategyConfig, pos: Position, close: float, day_high: float, day_low: float) -> bool:
    if cfg.exit_mode != "hold_strong" or pos.overnight:
        return False
    rng = max(day_high - day_low, 1e-9)
    return close - pos.entry >= pos.risk and (close - day_low) / rng >= 0.8


def now_after(now: datetime, t: time) -> bool:
    return now.time() >= t
