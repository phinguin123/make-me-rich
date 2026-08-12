"""
algorithms.py — Trading strategy detection algorithms.

Algorithm 1: Minervini Leader Pullback   (find_minervini_pullback)
Algorithm 2: Power Trend Pullback (PTP)  (find_power_trend_pullback)

All gate thresholds are read from config.py so editing that module is
enough to fine-tune any algorithm. Algorithm 2's heavy lifting lives in
:mod:`screener.ptp_engine` (the vectorized PowerTrendPullbackEngine
class); this module simply re-exports the per-symbol convenience wrapper
so engine.py can call both algorithms through a single import surface.
"""

import numpy as np
import pandas as pd

from .config import MPB_PARAMS
from .ptp_engine import find_power_trend_pullback  # re-exported (Algorithm 2)

__all__ = ["find_minervini_pullback", "find_power_trend_pullback"]


# ── Algorithm 1: Minervini Leader Pullback ───────────────────────────────────

def find_minervini_pullback(df: pd.DataFrame, params: dict | None = None) -> dict | None:
    """
    Minervini-style "Leader Pullback" scanner.

    All default thresholds come from config.MPB_PARAMS. Pass `params` to
    override specific keys without touching the config (useful for diagnostics).

    Gate summary
    ------------
    G1  All required indicators computable (non-NaN, non-zero).
    G2  SMA20 slope rising: today > 5d ago > 10d ago.
    G3  SMA50 > SMA200 (Stage-2 long-term alignment).
    G4  Two-sided SMA50 band + SMA200 ceiling (not parabolic, not breaking down).
    G5  Escape velocity: peak High ≥ min_escape_pct above its SMA20.
    G5b Peak recency: min_bars_since_peak ≤ bars since peak ≤ max_bars_since_peak.
    G5c Peak-day close quality (institutional conviction).
    G6  Touch count ≤ max_touches in prior touch_lookback bars (first/early touch).
    G7  Strike zone: within max_atr_dist of SMA20 or VWAP20 (OR mode by default).
    G8  Distribution + stall day count ≤ max_dist_days in prior dist_lookback bars.
    G9  Volume dry-up: recent avg < vol_dry_ratio × baseline avg.
    F   Entry / stop / trigger levels.
    """

    P = dict(MPB_PARAMS)   # start from config defaults
    if params:
        P.update(params)   # caller overrides specific keys

    if len(df) < P["min_bars"]:
        return None

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    # ── G1: Build all indicators ──────────────────────────────────────────────
    sma20  = close.rolling(20).mean()
    sma50  = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    vwap20 = (close * volume).rolling(20).sum() / volume.rolling(20).sum()

    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low,
         (high - prev_close).abs(),
         (low  - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr14 = tr.rolling(P["atr_period"]).mean()

    price        = close.iloc[-1]
    sma20_today  = sma20.iloc[-1]
    sma50_today  = sma50.iloc[-1]
    sma200_today = sma200.iloc[-1]
    vwap20_today = vwap20.iloc[-1]
    atr14_today  = atr14.iloc[-1]

    if any(pd.isna(v) or v == 0
           for v in [sma20_today, sma50_today, sma200_today, vwap20_today, atr14_today]):
        return None

    # ── G2: SMA20 slope ───────────────────────────────────────────────────────
    sma20_5d_ago  = sma20.iloc[-(P["slope_5d_offset"]  + 1)]
    sma20_10d_ago = sma20.iloc[-(P["slope_10d_offset"] + 1)]
    if not (sma20_today > sma20_5d_ago > sma20_10d_ago):
        return None

    # ── G3: Long-term MA alignment ────────────────────────────────────────────
    if P["require_sma50_gt_sma200"] and sma50_today <= sma200_today:
        return None

    # ── G4: Two-sided SMA50 band + SMA200 ceiling ────────────────────────────
    stretch_from_sma50  = (price - sma50_today)  / sma50_today
    stretch_from_sma200 = (price - sma200_today) / sma200_today
    if stretch_from_sma50  > P["max_pct_above_sma50"]:
        return None
    if stretch_from_sma50  < P["min_pct_below_sma50"]:
        return None
    if stretch_from_sma200 > P["max_pct_above_sma200"]:
        return None

    # ── G5: Escape velocity ───────────────────────────────────────────────────
    ev_lb    = P["escape_lookback"]
    high_ev  = high.iloc[-ev_lb:].values
    low_ev   = low.iloc[-ev_lb:].values
    close_ev = close.iloc[-ev_lb:].values
    sma20_ev = sma20.iloc[-ev_lb:].values

    peak_offset  = int(high_ev.argmax())
    peak_high    = high_ev[peak_offset]
    peak_low     = low_ev[peak_offset]
    peak_close   = close_ev[peak_offset]
    peak_sma_val = sma20_ev[peak_offset]

    if pd.isna(peak_sma_val) or peak_sma_val == 0:
        return None

    stretch_pct = (peak_high - peak_sma_val) / peak_sma_val * 100
    if stretch_pct < P["min_escape_pct"]:
        return None

    # ── G5b: Peak recency ─────────────────────────────────────────────────────
    bars_since_peak = (ev_lb - 1) - peak_offset
    if not (P["min_bars_since_peak"] <= bars_since_peak <= P["max_bars_since_peak"]):
        return None

    # ── G5c: Peak-day close quality ───────────────────────────────────────────
    peak_range = peak_high - peak_low
    if P["require_peak_quality"] and peak_range > 0:
        close_to_high_ratio = (peak_high - peak_close) / peak_range
        if close_to_high_ratio > P["peak_close_to_high"]:
            return None
    else:
        close_to_high_ratio = np.nan

    # Optional 20-bar momentum guard
    if P["min_momentum_20d"] is not None:
        close_20d_ago = close.iloc[-21]
        if not pd.isna(close_20d_ago) and close_20d_ago > 0:
            momentum_20d = price / close_20d_ago
            if momentum_20d < P["min_momentum_20d"]:
                return None
        else:
            momentum_20d = np.nan
    else:
        momentum_20d = np.nan

    # ── G6: Touch count (first/early touch) ───────────────────────────────────
    tc_lb    = P["touch_lookback"]
    low_tc   = low.iloc[-(tc_lb + 1):-1].values
    close_tc = close.iloc[-(tc_lb + 1):-1].values
    sma20_tc = sma20.iloc[-(tc_lb + 1):-1].values

    touch_upper       = sma20_tc * (1.0 + P["touch_band"])
    touch_close_floor = sma20_tc * (1.0 - P["close_hold_band"])
    is_touch          = (low_tc <= touch_upper) & (close_tc >= touch_close_floor)
    touch_count       = int(is_touch.sum())

    if touch_count > P["max_touches"]:
        return None

    # ── G7: Strike zone (ATR-normalised, OR mode default) ────────────────────
    dist_sma20_raw  = abs(price - sma20_today)
    dist_vwap20_raw = abs(price - vwap20_today)
    dist_sma20_atr  = dist_sma20_raw  / atr14_today
    dist_vwap20_atr = dist_vwap20_raw / atr14_today
    dist_sma20_pct  = dist_sma20_raw  / sma20_today
    dist_vwap20_pct = dist_vwap20_raw / vwap20_today

    sma20_in_zone  = dist_sma20_atr  <= P["max_atr_dist_sma20"]
    vwap20_in_zone = dist_vwap20_atr <= P["max_atr_dist_vwap20"]
    if P["strike_zone_mode"] == "AND":
        if not (sma20_in_zone and vwap20_in_zone):
            return None
    else:
        if not (sma20_in_zone or vwap20_in_zone):
            return None

    if P["max_pct_dist_sma20"]  is not None and dist_sma20_pct  > P["max_pct_dist_sma20"]:
        return None
    if P["max_pct_dist_vwap20"] is not None and dist_vwap20_pct > P["max_pct_dist_vwap20"]:
        return None

    # ── G8: Distribution + stall day filter ──────────────────────────────────
    dist_lb = P["dist_lookback"]

    full_returns  = close.pct_change()
    full_prev_vol = volume.shift(1)

    returns_w  = full_returns.iloc[-(dist_lb + 1):-1].values
    volume_w   = volume.iloc[-(dist_lb + 1):-1].values
    prev_vol_w = full_prev_vol.iloc[-(dist_lb + 1):-1].values

    vol_50d_avg         = volume.tail(50).mean()
    stall_vol_threshold = P["stall_vol_mult"] * vol_50d_avg

    is_dist_day  = (returns_w <= -P["dist_min_drop"]) & (volume_w > prev_vol_w)
    is_stall_day = (
        (np.abs(returns_w) < P["dist_min_drop"])
        & (volume_w > stall_vol_threshold)
    )

    dist_count    = int(is_dist_day.sum())
    stall_count   = int(is_stall_day.sum())
    flagged_count = dist_count + stall_count

    if flagged_count > P["max_dist_days"]:
        return None

    if P["heavy_vol_mult"] is not None:
        if price < close.iloc[-2] and volume.iloc[-1] > P["heavy_vol_mult"] * vol_50d_avg:
            return None

    # ── G9: Volume dry-up ─────────────────────────────────────────────────────
    vol_recent_avg   = volume.iloc[-P["vol_dry_bars"]:].mean()
    vol_baseline_avg = volume.tail(P["vol_baseline_bars"]).mean()

    if vol_baseline_avg == 0:
        return None

    vol_ratio_3_50 = vol_recent_avg / vol_baseline_avg
    if vol_ratio_3_50 >= P["vol_dry_ratio"]:
        return None

    # ── GH: Handle tightness gate ─────────────────────────────────────────────
    # Evaluated before computing entry so a wide/wick-driven handle returns None
    # immediately rather than producing an entry level that looks valid but isn't.
    hb = P["handle_bars"]
    handle_high_arr  = high.iloc[-hb:].values
    handle_low_arr   = low.iloc[-hb:].values
    handle_close_arr = close.iloc[-hb:].values

    # Close-based ceiling: avoids single-wick spikes setting the entry level.
    # Raw high is still used for intraday trigger detection below.
    handle_ceiling = float(handle_close_arr.max())

    handle_range_atr       = (handle_high_arr.max() - handle_low_arr.min()) / atr14_today
    handle_close_band_pct  = (handle_close_arr.max() - handle_close_arr.min()) / handle_ceiling

    if P["require_handle_tightness"]:
        if handle_range_atr > P["max_handle_range_atr"]:
            return None
        if handle_close_band_pct > P["max_handle_close_band_pct"]:
            return None

    # ── F: Entry / trigger ────────────────────────────────────────────────────
    prior_day_high = float(high.iloc[-2])

    # Entry: buy a tick above the close-based ceiling (wick-insensitive).
    # The intraday high is still used to detect whether today's bar has already
    # cleared the level — that's deliberate (we want price to prove itself live).
    entry_price = round(handle_ceiling * 1.001, 2)

    # Stop: structure-first.  The pullback low anchors the stop; ATR is shown
    # as a reference only.  We do NOT mechanically move the stop up to the MA
    # floor — if the chart is too wide the risk_cap fires and blocks the trigger.
    pullback_low    = float(low.iloc[-P["stop_lookback_bars"]:].min())
    buffered_pb_low = pullback_low * (1.0 - P["stop_buffer_pct"])
    ma_stop         = sma20_today - P["stop_atr_mult"] * atr14_today   # reference only
    stop_price      = round(buffered_pb_low, 2)

    if entry_price > stop_price > 0:
        risk_pct = round((entry_price - stop_price) / entry_price * 100, 2)
    else:
        risk_pct = np.nan

    # Trigger: intraday high clears yesterday's high, OR price is already within
    # half an ATR of the entry level (imminent breakout) — AND risk fits the cap.
    price_trigger_condition = (
        high.iloc[-1] > prior_day_high
        or abs(price - handle_ceiling) <= 0.5 * atr14_today
    )
    risk_within_cap = (not np.isnan(risk_pct)) and (risk_pct <= P["max_risk_pct"])
    is_trigger = bool(price_trigger_condition and risk_within_cap)

    return {
        "sma20_today":              round(sma20_today,  2),
        "sma50_today":              round(sma50_today,  2),
        "sma200_today":             round(sma200_today, 2),
        "vwap20_today":             round(vwap20_today, 2),
        "atr14_today":              round(atr14_today,  4),
        "price":                    round(price,        2),
        "dist_sma20_atr":           round(dist_sma20_atr,  3),
        "dist_vwap20_atr":          round(dist_vwap20_atr, 3),
        "dist_sma20_pct":           round(dist_sma20_pct  * 100, 3),
        "dist_vwap20_pct":          round(dist_vwap20_pct * 100, 3),
        "stretch_pct":              round(stretch_pct,          2),
        "peak_offset":              peak_offset,
        "bars_since_peak":          bars_since_peak,
        "close_to_high_ratio":      round(float(close_to_high_ratio), 3) if not np.isnan(close_to_high_ratio) else None,
        "momentum_20d":             round(float(momentum_20d), 3) if not np.isnan(momentum_20d) else None,
        "touch_count_window":       touch_count,
        "touch_lookback":           tc_lb,
        "distribution_count_window": dist_count,
        "stall_count_window":        stall_count,
        "flagged_count_window":      flagged_count,
        "dist_lookback":             dist_lb,
        "vol_ratio_3_50":           round(vol_ratio_3_50, 3),
        "stretch_from_sma50_pct":   round(stretch_from_sma50  * 100, 2),
        "stretch_from_sma200_pct":  round(stretch_from_sma200 * 100, 2),
        # GH — handle tightness metrics (exposed for debugging / ranking)
        "handle_ceiling":           round(handle_ceiling,         2),
        "handle_range_atr":         round(handle_range_atr,       3),
        "handle_close_band_pct":    round(handle_close_band_pct * 100, 3),
        # F — entry / trigger
        "is_setup":                 True,
        "is_trigger":               is_trigger,
        "risk_cap_blocked":         bool(price_trigger_condition and not risk_within_cap),
        "entry_price":              entry_price,
        "stop_price":               stop_price,     # structure-anchored
        "pullback_low":             round(pullback_low,  2),
        "ma_stop":                  round(ma_stop,       2),   # reference only
        "risk_pct":                 risk_pct,
        "max_risk_pct":             P["max_risk_pct"],
        "prior_day_high":           round(prior_day_high, 2),
        "params":                   P,
    }
