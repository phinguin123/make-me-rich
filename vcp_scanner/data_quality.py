"""
vcp_scanner/data_quality.py

Data-quality guards for OHLCV data used in the VCP scanner.

Key functions
-------------
validate_ohlcv(df, symbol, name) → ValidationResult
    Checks for missing days, stale prices, VI/limit-up events, and
    probable corporate-action splits.

adjust_for_splits(df) → tuple[pd.DataFrame, bool]
    Applies a backward ratio adjustment when an overnight gap > SPLIT_THRESHOLD
    is detected (heuristic — proper adjustment needs corporate-action data).

log_drop(symbol, name, reason) → None
    Appends a structured line to dropped_tickers.log.

detect_vi_events(df) → pd.Series[bool]
    Boolean mask of rows where (High-Low)/prev_close > VI_THRESHOLD_PCT.

Korean-market notes
-------------------
* VI (Volatility Interruption) triggers at a ±10 % move from the reference
  price intraday.  We flag these days because ATR computed over them distorts
  contraction scores.
* Limit-up (+30 %) days are visible as single bars with extreme High/Close
  moves.  Spike-capping in price.py handles them in rolling stats; here we
  just count them.
* Stock splits on KRX are common in growth names.  When |overnight gap| > 40 %
  and the ratio is close to a common ratio (2:1, 3:1, 1:2), we apply a
  backwards multiplicative adjustment so historical ATR stays comparable.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

# ── Thresholds ────────────────────────────────────────────────────────────────
VI_THRESHOLD_PCT: float = 10.0         # intraday (H-L)/prev_close > 10 % → VI flag
LIMIT_UP_PCT: float = 29.0            # close/prev_close > 29 % → limit-up flag
SPLIT_OVERNIGHT_GAP: float = 0.40     # |overnight gap| > 40 % → suspect split
MIN_VALID_DAYS_RATIO: float = 0.70    # ≥ 70 % of expected trading days must exist
STALE_VOLUME_DAYS: int = 3            # > 3 consecutive zero-volume days → warning

_default_drop_log = Path(__file__).resolve().parents[1] / "output" / "vcp" / "dropped_tickers.log"
DROPPED_LOG_FILE: str = os.environ.get("VCP_DROPPED_LOG", str(_default_drop_log))
Path(DROPPED_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

# ── Logging setup ─────────────────────────────────────────────────────────────
_drop_log = logging.getLogger("vcp_scanner.dropped")
if not _drop_log.handlers:
    _fh = logging.FileHandler(DROPPED_LOG_FILE, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s\t%(message)s"))
    _drop_log.addHandler(_fh)
_drop_log.setLevel(logging.INFO)
_drop_log.propagate = False  # don't bubble into root handler


# ── Result container ─────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    symbol: str
    name: str
    ok: bool
    reason: str = ""
    vi_days: int = 0
    limit_up_days: int = 0
    split_adjusted: bool = False
    missing_days_pct: float = 0.0
    warnings: list[str] = field(default_factory=list)


# ── Public helpers ────────────────────────────────────────────────────────────

def log_drop(symbol: str, name: str, reason: str) -> None:
    """Write a structured drop record to the dropped-tickers log."""
    _drop_log.info("DROP\t%s\t%s\t%s", symbol, name, reason)


def detect_vi_events(df: pd.DataFrame) -> pd.Series:
    """
    Boolean mask — True on rows where (High − Low) / prev_close > VI_THRESHOLD_PCT.

    Uses High-Low range relative to previous close, which closely mirrors
    the KRX VI trigger definition.
    """
    if df.empty or len(df) < 2:
        return pd.Series(False, index=df.index)
    prev_close = df["Close"].shift(1).replace(0.0, np.nan)
    intraday_range_pct = (df["High"] - df["Low"]) / prev_close * 100.0
    return intraday_range_pct > VI_THRESHOLD_PCT


def detect_limit_up_events(df: pd.DataFrame) -> pd.Series:
    """
    Boolean mask — True on rows where close/prev_close − 1 > LIMIT_UP_PCT.
    KRX daily limit is ±30 %; we use 29 % to catch near-limit events too.
    """
    if df.empty or len(df) < 2:
        return pd.Series(False, index=df.index)
    prev_close = df["Close"].shift(1).replace(0.0, np.nan)
    daily_ret_pct = (df["Close"] / prev_close - 1.0) * 100.0
    return daily_ret_pct > LIMIT_UP_PCT


# ── Common split ratios on KRX (forward and reverse) ─────────────────────────
_SPLIT_RATIOS: tuple[float, ...] = (2.0, 3.0, 4.0, 5.0, 0.5, 0.333, 0.25, 0.2)
_SPLIT_TOLERANCE: float = 0.06   # ratio must be within 6 % of a known ratio


def _is_plausible_split(ratio: float) -> tuple[bool, float]:
    """Return (is_split, matched_ratio) if ratio is close to a standard split ratio."""
    for r in _SPLIT_RATIOS:
        if abs(ratio - r) / r < _SPLIT_TOLERANCE:
            return True, r
    return False, ratio


def adjust_for_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """
    Scan for large overnight price gaps that suggest stock splits, then apply
    a backward ratio adjustment to make historical OHLCV comparable.

    Returns
    -------
    (adjusted_df, was_adjusted)

    Limitations
    -----------
    * Requires |overnight_gap| > SPLIT_OVERNIGHT_GAP (40 %).
    * Ratio must match a common KRX split ratio (2:1, 3:1, 1:2 …) within 6 %.
    * Adjusts prices only — no dividend or rights-issue correction.
    * When multiple splits exist in the history window the adjustment stacks
      iteratively from most recent to oldest.
    """
    if len(df) < 5:
        return df, False

    overnight_ret = df["Close"].pct_change()
    suspect_idx = overnight_ret[overnight_ret.abs() > SPLIT_OVERNIGHT_GAP].index

    if len(suspect_idx) == 0:
        return df, False

    df = df.copy()
    adjusted = False

    # Process from most-recent split backward so earlier ratios compound correctly
    for idx in reversed(suspect_idx):
        ret = overnight_ret.loc[idx]
        ratio = 1.0 + ret   # e.g. -50 % overnight → ratio 0.5 (2:1 split, price halves)
        is_split, matched = _is_plausible_split(ratio)
        if not is_split:
            continue

        loc = df.index.get_loc(idx)

        # Backward price adjustment: multiply pre-split prices by `matched` so that
        # the historical series aligns with the post-split price scale.
        # Example: 2:1 split → price halves on split day (matched=0.5)
        #          → multiply pre-split bars by 0.5 to match post-split scale.
        price_cols = [c for c in ("Open", "High", "Low", "Close") if c in df.columns]
        df.iloc[:loc, df.columns.get_indexer(price_cols)] = (
            df.iloc[:loc][price_cols].values * matched
        )
        # Volume adjustment: pre-split shares are fewer, scale up by 1/matched
        if "Volume" in df.columns and matched > 0:
            df.iloc[:loc, df.columns.get_loc("Volume")] = (
                df.iloc[:loc]["Volume"].values / matched
            )
        adjusted = True

    return df, adjusted


def validate_ohlcv(
    df: pd.DataFrame,
    symbol: str,
    name: str,
    *,
    min_rows: int = 200,
) -> ValidationResult:
    """
    Run data-quality checks on an OHLCV DataFrame.

    Checks (in order)
    -----------------
    1. Not None/empty
    2. Minimum row count
    3. Zero/negative close prices
    4. Completeness vs expected trading days (70 % floor)
    5. Stale volume (> 3 consecutive zero-volume sessions)
    6. VI/limit-up event count (informational — does not drop)

    Parameters
    ----------
    df       : OHLCV DataFrame indexed by date
    symbol   : KRX 6-digit code
    name     : human-readable ticker name
    min_rows : minimum required rows (default MIN_HISTORY_ROWS = 200)

    Returns
    -------
    ValidationResult — .ok is False iff the symbol must be excluded.
    """
    result = ValidationResult(symbol=symbol, name=name, ok=True)

    if df is None or df.empty:
        result.ok = False
        result.reason = "empty_dataframe"
        return result

    if len(df) < min_rows:
        result.ok = False
        result.reason = f"insufficient_rows:{len(df)}<{min_rows}"
        return result

    if (df["Close"] <= 0).any():
        result.ok = False
        result.reason = "zero_or_negative_close"
        return result

    # Expected trading days over the data's date span (~252/year)
    span_days = max(1, (df.index[-1] - df.index[0]).days)
    expected_trading = max(1, int(span_days * 252 / 365))
    missing_ratio = max(0.0, 1.0 - len(df) / expected_trading)
    result.missing_days_pct = round(missing_ratio * 100.0, 1)

    if missing_ratio > (1.0 - MIN_VALID_DAYS_RATIO):
        result.ok = False
        result.reason = f"too_many_missing_days:{result.missing_days_pct:.1f}%"
        return result

    # Stale volume
    if "Volume" in df.columns:
        zero_vol = (df["Volume"] == 0)
        if zero_vol.any():
            # Longest run of consecutive zeros
            runs = zero_vol.astype(int).groupby((~zero_vol).cumsum()).sum()
            max_run = int(runs.max())
            if max_run > STALE_VOLUME_DAYS:
                result.warnings.append(f"stale_volume:{max_run}_consecutive_zero_vol_days")

    # VI and limit-up event counts (informational)
    vi_mask = detect_vi_events(df)
    result.vi_days = int(vi_mask.sum())
    if result.vi_days > 0:
        result.warnings.append(f"vi_events:{result.vi_days}")

    lu_mask = detect_limit_up_events(df)
    result.limit_up_days = int(lu_mask.sum())
    if result.limit_up_days > 0:
        result.warnings.append(f"limit_up_days:{result.limit_up_days}")

    return result
