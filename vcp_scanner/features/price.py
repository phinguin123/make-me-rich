"""
vcp_scanner/features/price.py

Price-structure features for the VCP scanner:
  - ATR contraction sequence across multiple windows
  - Volatility percentile vs 1-year history
  - Distance to volume-weighted Point of Control (POC)
  - Breakout proximity (gap from 52-week high)
  - Mansfield Relative Strength vs KOSPI / KOSDAQ benchmark

Korean-market robustness changes (vs original SMA-ATR version)
--------------------------------------------------------------
1. Wilder EMA ATR replaces simple rolling-mean ATR.
   Wilder's method (exponential smoothing with α=1/n) is the industry standard
   (TradingView, Bloomberg) and gives less weight to the single most-recent bar.

2. Daily-range spike-capping before rolling calculations.
   VI (Volatility Interruption) and limit-up/down days can produce intraday
   ranges of 25–60 % that would otherwise dominate the short ATR windows.
   _cap_daily_range() clips each bar's True Range to
       max(TR, SPIKE_CAP_MULTIPLIER × median(TR, last 20 bars))
   before computing ATR, preserving the structural trend.

3. Vol-percentile window uses a 5-day rolling median of ATR-20 to avoid
   single-day spikes making the current period appear artificially volatile.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import (
    ATR_LOOKBACK_PERIODS,
    BREAKOUT_PROXIMITY_PCT,
    POC_LOOKBACK_DAYS,
    POC_MAX_DISTANCE_PCT,
    RS_LOOKBACK_DAYS,
    VOL_PERCENTILE_WINDOW,
)

# ── Spike-cap configuration ───────────────────────────────────────────────────
# True Range is capped at this multiple of the rolling 20-bar median TR.
# A VI day with TR = 25 % is common; the 20-day median TR for a normal mid-cap
# is ~1.5 %, so the cap kicks in at 3 × 1.5 % = 4.5 %.
SPIKE_CAP_MULTIPLIER: float = 4.0
SPIKE_CAP_WINDOW: int = 20


# ── Helpers ───────────────────────────────────────────────────────────────────

def _true_range(df: pd.DataFrame) -> pd.Series:
    h, l, pc = df["High"], df["Low"], df["Close"].shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def _cap_daily_range(tr: pd.Series) -> pd.Series:
    """
    Cap each bar's True Range to SPIKE_CAP_MULTIPLIER × rolling median TR.

    This removes the outsized ATR contribution of VI/limit-up sessions while
    keeping the smoothed level of volatility intact for the contraction check.
    """
    rolling_median = tr.rolling(SPIKE_CAP_WINDOW, min_periods=5).median()
    cap = rolling_median * SPIKE_CAP_MULTIPLIER
    # cap.fillna avoids clipping on the first few bars where we have no median
    return tr.where(cap.isna() | (tr <= cap), cap)


def _wilder_atr(df: pd.DataFrame, period: int) -> pd.Series:
    """
    Compute ATR using Wilder's exponential smoothing (α = 1/period).

    Algorithm
    ---------
    1. Compute True Range.
    2. Cap spikes (VI/limit-up days) at SPIKE_CAP_MULTIPLIER × 20-bar median TR.
    3. Seed the first value with a simple mean over 'period' bars.
    4. Apply Wilder EMA: ATR_t = ATR_{t-1} × (period-1)/period + TR_t / period.

    Returns a Series aligned with df.index; NaN for the first (period-1) bars.
    """
    tr = _true_range(df)
    tr_capped = _cap_daily_range(tr)

    alpha = 1.0 / period
    atr_vals = np.full(len(tr_capped), np.nan)

    # Find the first index where we have enough data for the seed SMA
    seed_start: int | None = None
    valid_mask = tr_capped.notna().values
    for i in range(period - 1, len(tr_capped)):
        window = tr_capped.values[i - period + 1 : i + 1]
        if np.all(~np.isnan(window)):
            seed_start = i
            break

    if seed_start is None:
        return pd.Series(atr_vals, index=df.index)

    atr_vals[seed_start] = np.nanmean(tr_capped.values[seed_start - period + 1 : seed_start + 1])

    for i in range(seed_start + 1, len(tr_capped)):
        tr_i = tr_capped.values[i]
        if np.isnan(tr_i):
            atr_vals[i] = atr_vals[i - 1]
        else:
            atr_vals[i] = atr_vals[i - 1] * (1.0 - alpha) + tr_i * alpha

    return pd.Series(atr_vals, index=df.index)


def _sigmoid(x: float, k: float = 1.0) -> float:
    """Scalar sigmoid clamped to [0, 1]."""
    return float(1.0 / (1.0 + np.exp(-k * x)))


# ── ATR contraction sequence ──────────────────────────────────────────────────

def atr_contraction_sequence(df: pd.DataFrame) -> dict[str, float | None]:
    """
    Compute Wilder ATR at each window in ATR_LOOKBACK_PERIODS and derive
    contraction ratios between short and long windows.

    VCP pattern requires volatility contraction: each successive base should
    show narrowing daily ranges.  We quantify this as:

        atr_5_20_ratio  = ATR(5)  / ATR(20)   — lower ⟹ more contracted
        atr_10_40_ratio = ATR(10) / ATR(40)   — second confirmation

    Returns
    -------
    atr_{p}               — absolute Wilder ATR value for period p
    atr_{p}_pct           — ATR / close × 100  (comparable across price levels)
    atr_5_20_ratio        — short/long ratio; <1 signals contraction
    atr_10_40_ratio       — second contraction check
    atr_contraction_score — [0, 1]; 1 = maximally contracted
    vi_days_in_window     — count of VI-flagged days in last 20 bars (informational)
    """
    feats: dict[str, float | None] = {}
    atrs: dict[int, float | None] = {}
    last_close = float(df["Close"].iloc[-1])

    for p in ATR_LOOKBACK_PERIODS:
        series = _wilder_atr(df, p)
        raw = series.iloc[-1] if not series.empty else np.nan
        val = float(raw) if not np.isnan(raw) else None
        atrs[p] = val
        feats[f"atr_{p}"] = val
        feats[f"atr_{p}_pct"] = (val / last_close * 100.0) if (val and last_close > 0) else None

    # VI day count in recent 20 bars (informational, surfaced in scorecard)
    from ..data_quality import detect_vi_events
    vi_mask = detect_vi_events(df.tail(20))
    feats["vi_days_in_window"] = int(vi_mask.sum())

    # Contraction ratios
    r1 = (atrs[5]  / atrs[20]) if (atrs[5]  and atrs[20] and atrs[20] > 0) else None
    r2 = (atrs[10] / atrs[40]) if (atrs[10] and atrs[40] and atrs[40] > 0) else None
    feats["atr_5_20_ratio"]  = r1
    feats["atr_10_40_ratio"] = r2

    # Score: ratio→0 = fully contracted (score 1); ratio→1 = no contraction (score 0)
    scores = [max(0.0, min(1.0, 1.0 - r)) for r in (r1, r2) if r is not None]
    feats["atr_contraction_score"] = float(np.mean(scores)) if scores else 0.0

    return feats


# ── Volatility percentile ─────────────────────────────────────────────────────

def volatility_percentile(df: pd.DataFrame) -> dict[str, float | None]:
    """
    Current smoothed ATR-20 expressed as a percentile of its VOL_PERCENTILE_WINDOW
    history.  Lower percentile → calmer → more VCP-friendly.

    Time-consistency improvement
    ----------------------------
    Instead of the raw single-day ATR(20), we use a 5-day trailing median of
    the ATR-20 series as the "current" value.  This prevents a single VI/limit-up
    session from making the current period look artificially volatile and dropping
    an otherwise valid VCP candidate.

    Returns
    -------
    vol_percentile       — 0–100; lower is calmer
    vol_percentile_score — [0, 1]; higher score for lower percentile
    """
    atr20 = _wilder_atr(df, 20)
    if atr20.dropna().empty:
        return {"vol_percentile": None, "vol_percentile_score": 0.5}

    # 5-day trailing median to smooth away VI-day spikes
    smoothed = atr20.rolling(5, min_periods=1).median()
    current = smoothed.iloc[-1]
    if np.isnan(current):
        return {"vol_percentile": None, "vol_percentile_score": 0.5}

    history = atr20.dropna().tail(VOL_PERCENTILE_WINDOW)
    if len(history) < 20:
        return {"vol_percentile": None, "vol_percentile_score": 0.5}

    pct   = float((history < current).mean() * 100.0)
    score = 1.0 - pct / 100.0
    return {"vol_percentile": pct, "vol_percentile_score": score}


# ── Point of Control ──────────────────────────────────────────────────────────

def _dynamic_poc(df: pd.DataFrame, lookback: int = POC_LOOKBACK_DAYS) -> float:
    """
    Volume-profile Point of Control via dynamic binning.
    Bin count scales with price-range percentage to adapt to volatility regime.
    """
    sub = df.tail(lookback).copy()
    sub["tp"] = (sub["High"] + sub["Low"] + sub["Close"]) / 3.0
    lo, hi = sub["tp"].min(), sub["tp"].max()
    if lo == hi:
        return float(lo)
    pct_range = (hi - lo) / lo
    n_bins    = max(20, min(int(pct_range * 100), 100))
    sub["bucket"] = pd.cut(sub["tp"], bins=n_bins)
    poc_bucket = sub.groupby("bucket", observed=False)["Volume"].sum().idxmax()
    return float(poc_bucket.mid)


def poc_features(df: pd.DataFrame) -> dict[str, float | None]:
    """
    Distance from last close to volume-weighted Point of Control, plus a score.

    Optimal zone: 0 %–10 % above POC (price has lifted off the support shelf).
    Outside [0 %, POC_MAX_DISTANCE_PCT] → score 0.

    Scoring formula
    ---------------
    Parabolic peak at 5 % above POC:
        score = max(0, min(1, 1 − ((dist − 5) / 15)²))

    Returns
    -------
    poc_price           — POC price level
    poc_distance_pct    — (close − poc) / poc × 100
    poc_distance_score  — [0, 1]
    """
    poc        = _dynamic_poc(df)
    last_close = float(df["Close"].iloc[-1])

    if poc <= 0:
        return {"poc_price": poc, "poc_distance_pct": None, "poc_distance_score": 0.0}

    dist_pct = (last_close - poc) / poc * 100.0

    if dist_pct < 0 or dist_pct > POC_MAX_DISTANCE_PCT:
        score = 0.0
    else:
        score = max(0.0, min(1.0, 1.0 - ((dist_pct - 5.0) / 15.0) ** 2))

    return {"poc_price": poc, "poc_distance_pct": dist_pct, "poc_distance_score": score}


# ── Breakout proximity ────────────────────────────────────────────────────────

def breakout_proximity(df: pd.DataFrame) -> dict[str, float | None]:
    """
    Gap between current price and 52-week high.

    Stocks within BREAKOUT_PROXIMITY_PCT (30 %) of their 52-week high are
    near a potential breakout pivot.

    Returns
    -------
    high_52w                — 52-week high (last 252 bars' High max)
    breakout_proximity_pct  — (high − close) / high × 100  (0 = at high)
    breakout_prox_score     — [0, 1]; 1 = at the high, 0 = BREAKOUT_PROXIMITY_PCT below
    """
    high_52w   = float(df["High"].tail(252).max())
    last_close = float(df["Close"].iloc[-1])

    if high_52w <= 0:
        return {"high_52w": high_52w, "breakout_proximity_pct": None, "breakout_prox_score": 0.0}

    gap_pct = (high_52w - last_close) / high_52w * 100.0
    score   = max(0.0, min(1.0, 1.0 - gap_pct / BREAKOUT_PROXIMITY_PCT))

    return {
        "high_52w":               high_52w,
        "breakout_proximity_pct": gap_pct,
        "breakout_prox_score":    score,
    }


# ── Relative Strength ─────────────────────────────────────────────────────────

def relative_strength(
    df: pd.DataFrame,
    benchmark_df: pd.DataFrame,
) -> dict[str, float | None]:
    """
    Mansfield-style relative strength vs benchmark.

    RS = (S_now / S_base) / (B_now / B_base) − 1

    Positive RS means the stock has outperformed the benchmark over the window.

    Spike-resistance
    ----------------
    We compute RS over RS_LOOKBACK_DAYS (63 ≈ 3 months) and 2× (6 months).
    Using multi-month windows inherently dilutes single VI-day moves.

    Returns
    -------
    rs_3m        — 3-month RS (positive = outperforms KOSPI/KOSDAQ)
    rs_6m        — 6-month RS
    rs_score     — [0, 1] via sigmoid(rs_3m, k=4)
                   RS ≈ 0 → 0.50 (neutral), RS ≈ +0.25 → 0.73, RS ≈ −0.25 → 0.27
    """
    feats: dict[str, float | None] = {}

    for label, n in (("rs_3m", RS_LOOKBACK_DAYS), ("rs_6m", RS_LOOKBACK_DAYS * 2)):
        try:
            s_now  = float(df["Close"].iloc[-1])
            s_base = float(df["Close"].iloc[-(n + 1)])
            b_now  = float(benchmark_df["Close"].iloc[-1])
            b_base = float(benchmark_df["Close"].iloc[-(n + 1)])
            if s_base <= 0 or b_base <= 0 or b_now <= 0:
                rs = None
            else:
                rs = float((s_now / s_base) / (b_now / b_base) - 1.0)
        except (IndexError, ZeroDivisionError, KeyError):
            rs = None
        feats[label] = rs

    rs3 = feats.get("rs_3m")
    feats["rs_score"] = _sigmoid(rs3, k=4.0) if rs3 is not None else 0.5

    return feats
