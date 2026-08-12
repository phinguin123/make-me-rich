"""
diagnostics.py — Gate telemetry for the surviving algorithms.

Run from the command line via main.py:

    python -m screener.main --diagnose-mpb              (Algorithm 1)
    python -m screener.main --diagnose-mpb --sample 1000
    python -m screener.main --diagnose-ptp              (Algorithm 2)
    python -m screener.main --diagnose-ptp --sample 1000

Both diagnostics are non-short-circuiting: every gate is evaluated on
every ticker so the funnel and per-gate medians reveal exactly where
candidates die. The Algorithm 2 mirror decomposes the four user-spec
gates (regime / pullback / trigger / risk) into ten sub-gates so you
can see which conjunct is actually killing tickers, not just "regime
filter".
"""

import time
import random
import logging

import numpy as np
import pandas as pd
from tqdm import tqdm

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus
from datetime import datetime, timedelta, timezone

from .api import trading_client, data_client
from .config import (
    FETCH_DAYS, CHUNK_SIZE, RATE_LIMIT_SLEEP,
    MIN_PRICE, MIN_AVG_VOLUME_20D, MIN_AVG_DOLLAR_VOL_20D,
    MPB_PARAMS, PTP_PARAMS,
)
from .patterns import is_buyout_or_flatline
from .ptp_engine import PowerTrendPullbackEngine


# ── Gate ordering & labels (mirrors find_minervini_pullback gate order) ───────

_MPB_GATE_ORDER = [
    "G1_INDICATOR_NAN",
    "G2_SMA20_SLOPE",
    "G3_MA_ALIGNMENT",
    "G4_EXTENSION_CEIL",
    "G4B_SMA50_FLOOR",
    "G4C_SMA200_CEIL",
    "G5_ESCAPE_VEL",
    "G5B_PEAK_AGE",
    "G5C_PEAK_QUALITY",
    "G6_TOUCH_COUNT",
    "G7_STRIKE_ZONE",
    "G8_DISTRIBUTION",
    "G9_VOLUME_DRY",
    "GH_HANDLE_TIGHTNESS",
]

_MPB_GATE_LABELS = {
    "G1_INDICATOR_NAN":      "G1  Indicators computable       ",
    "G2_SMA20_SLOPE":        "G2  SMA20 slope rising          ",
    "G3_MA_ALIGNMENT":       "G3  SMA50 > SMA200              ",
    "G4_EXTENSION_CEIL":     "G4  Extension ceiling (SMA50)   ",
    "G4B_SMA50_FLOOR":       "G4b Extension floor  (SMA50)    ",
    "G4C_SMA200_CEIL":       "G4c Extension ceiling (SMA200)  ",
    "G5_ESCAPE_VEL":         "G5  Escape velocity             ",
    "G5B_PEAK_AGE":          "G5b Peak age (bars since peak)  ",
    "G5C_PEAK_QUALITY":      "G5c Peak day quality            ",
    "G6_TOUCH_COUNT":        "G6  Touch count                 ",
    "G7_STRIKE_ZONE":        "G7  Strike zone (ATR, OR mode)  ",
    "G8_DISTRIBUTION":       "G8  Distribution / stall days   ",
    "G9_VOLUME_DRY":         "G9  Volume dry-up               ",
    "GH_HANDLE_TIGHTNESS":   "GH  Handle tightness            ",
}


# ── Algorithm 1 diagnostics (Minervini Leader Pullback) ──────────────────────

def _diagnose_minervini_pullback(df: pd.DataFrame, params: dict | None = None) -> dict:
    """
    Non-short-circuiting diagnostic mirror of find_minervini_pullback.
    Runs every gate regardless of earlier failures.
    """
    P = dict(MPB_PARAMS)
    if params:
        P.update(params)

    out: dict = {
        "passed": False, "failed_at": "INSUFFICIENT_DATA",
        "sma20_today": np.nan, "sma50_today": np.nan, "sma200_today": np.nan,
        "vwap20_today": np.nan, "atr14_today": np.nan, "price": np.nan,
        "sma20_5d_ago": np.nan, "sma20_10d_ago": np.nan, "sma20_slope_ok": False,
        "sma50_gt_sma200": False,
        "stretch_from_sma50_pct": np.nan, "stretch_from_sma200_pct": np.nan,
        "stretch_pct": np.nan, "bars_since_peak": np.nan,
        "close_to_high_ratio": np.nan,
        "touch_count": np.nan,
        "dist_sma20_atr": np.nan, "dist_vwap20_atr": np.nan,
        "dist_sma20_pct": np.nan, "dist_vwap20_pct": np.nan,
        "sma20_vwap20_gap_atr": np.nan,
        "dist_count": np.nan, "stall_count": np.nan, "flagged_count": np.nan,
        "vol_ratio_3_50": np.nan,
        # GH
        "handle_range_atr": np.nan, "handle_close_band_pct": np.nan,
    }

    if len(df) < P["min_bars"]:
        return out

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    sma20  = close.rolling(20).mean()
    sma50  = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    vwap20 = (close * volume).rolling(20).sum() / volume.rolling(20).sum()

    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr14 = tr.rolling(P["atr_period"]).mean()

    price        = close.iloc[-1]
    sma20_today  = sma20.iloc[-1]
    sma50_today  = sma50.iloc[-1]
    sma200_today = sma200.iloc[-1]
    vwap20_today = vwap20.iloc[-1]
    atr14_today  = atr14.iloc[-1]

    out.update({
        "price":        round(price, 2),
        "sma20_today":  round(float(sma20_today),  2) if not pd.isna(sma20_today)  else np.nan,
        "sma50_today":  round(float(sma50_today),  2) if not pd.isna(sma50_today)  else np.nan,
        "sma200_today": round(float(sma200_today), 2) if not pd.isna(sma200_today) else np.nan,
        "vwap20_today": round(float(vwap20_today), 2) if not pd.isna(vwap20_today) else np.nan,
        "atr14_today":  round(float(atr14_today),  4) if not pd.isna(atr14_today)  else np.nan,
    })

    g1_ok = not any(pd.isna(v) or v == 0
                    for v in [sma20_today, sma50_today, sma200_today, vwap20_today, atr14_today])
    if not g1_ok:
        out["failed_at"] = "G1_INDICATOR_NAN"
        return out

    # G2
    sma20_5d_ago  = sma20.iloc[-(P["slope_5d_offset"]  + 1)]
    sma20_10d_ago = sma20.iloc[-(P["slope_10d_offset"] + 1)]
    slope_ok = bool(sma20_today > sma20_5d_ago > sma20_10d_ago)
    out.update({
        "sma20_5d_ago":   round(float(sma20_5d_ago),  2),
        "sma20_10d_ago":  round(float(sma20_10d_ago), 2),
        "sma20_slope_ok": slope_ok,
    })

    # G3
    sma50_gt_sma200 = bool(sma50_today > sma200_today)
    out["sma50_gt_sma200"] = sma50_gt_sma200

    # G4
    stretch_from_sma50  = (price - sma50_today)  / sma50_today
    stretch_from_sma200 = (price - sma200_today) / sma200_today
    out["stretch_from_sma50_pct"]  = round(stretch_from_sma50  * 100, 2)
    out["stretch_from_sma200_pct"] = round(stretch_from_sma200 * 100, 2)

    # G5
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
    bars_since_peak = (ev_lb - 1) - peak_offset

    if not (pd.isna(peak_sma_val) or peak_sma_val == 0):
        stretch_pct = (peak_high - peak_sma_val) / peak_sma_val * 100
        peak_range  = peak_high - peak_low
        c2h = (peak_high - peak_close) / peak_range if peak_range > 0 else np.nan
        out.update({
            "stretch_pct":         round(stretch_pct, 2),
            "bars_since_peak":     bars_since_peak,
            "close_to_high_ratio": round(c2h, 3) if not np.isnan(c2h) else np.nan,
        })
    else:
        stretch_pct = np.nan
        c2h         = np.nan

    # G6
    tc_lb    = P["touch_lookback"]
    low_tc   = low.iloc[-(tc_lb + 1):-1].values
    close_tc = close.iloc[-(tc_lb + 1):-1].values
    sma20_tc = sma20.iloc[-(tc_lb + 1):-1].values
    is_touch = (
        (low_tc   <= sma20_tc * (1.0 + P["touch_band"]))
        & (close_tc >= sma20_tc * (1.0 - P["close_hold_band"]))
    )
    touch_count = int(is_touch.sum())
    out["touch_count"] = touch_count

    # G7
    dist_sma20_raw   = abs(price - sma20_today)
    dist_vwap20_raw  = abs(price - vwap20_today)
    dist_sma20_atr   = dist_sma20_raw  / atr14_today
    dist_vwap20_atr  = dist_vwap20_raw / atr14_today
    sma20_vwap20_gap = abs(sma20_today - vwap20_today) / atr14_today
    out.update({
        "dist_sma20_atr":       round(dist_sma20_atr,  3),
        "dist_vwap20_atr":      round(dist_vwap20_atr, 3),
        "dist_sma20_pct":       round(dist_sma20_raw / sma20_today  * 100, 3),
        "dist_vwap20_pct":      round(dist_vwap20_raw / vwap20_today * 100, 3),
        "sma20_vwap20_gap_atr": round(sma20_vwap20_gap, 3),
    })

    # G8
    dist_lb        = P["dist_lookback"]
    full_returns   = close.pct_change()
    full_prev_vol  = volume.shift(1)
    returns_w      = full_returns.iloc[-(dist_lb + 1):-1].values
    volume_w       = volume.iloc[-(dist_lb + 1):-1].values
    prev_vol_w     = full_prev_vol.iloc[-(dist_lb + 1):-1].values
    vol_50d_avg    = volume.tail(50).mean()
    stall_thresh   = P["stall_vol_mult"] * vol_50d_avg
    is_dist_day    = (returns_w <= -P["dist_min_drop"]) & (volume_w > prev_vol_w)
    is_stall_day   = (np.abs(returns_w) < P["dist_min_drop"]) & (volume_w > stall_thresh)
    d_cnt = int(is_dist_day.sum())
    s_cnt = int(is_stall_day.sum())
    out.update({"dist_count": d_cnt, "stall_count": s_cnt, "flagged_count": d_cnt + s_cnt})

    # G9
    vol_recent_avg   = volume.iloc[-P["vol_dry_bars"]:].mean()
    vol_baseline_avg = volume.tail(P["vol_baseline_bars"]).mean()
    vol_ratio_3_50   = vol_recent_avg / vol_baseline_avg if vol_baseline_avg > 0 else np.nan
    out["vol_ratio_3_50"] = round(float(vol_ratio_3_50), 3) if not np.isnan(vol_ratio_3_50) else np.nan

    # ── GH: Handle tightness ──────────────────────────────────────────────────
    hb = P["handle_bars"]
    handle_high_arr  = high.iloc[-hb:].values
    handle_low_arr   = low.iloc[-hb:].values
    handle_close_arr = close.iloc[-hb:].values
    handle_ceiling_val       = float(handle_close_arr.max())
    handle_range_atr_val     = (handle_high_arr.max() - handle_low_arr.min()) / atr14_today
    handle_close_band_pct_val = (handle_close_arr.max() - handle_close_arr.min()) / handle_ceiling_val
    out["handle_range_atr"]      = round(handle_range_atr_val, 3)
    out["handle_close_band_pct"] = round(handle_close_band_pct_val * 100, 3)

    # Determine first failing gate
    sma20_in_zone  = dist_sma20_atr  <= P["max_atr_dist_sma20"]
    vwap20_in_zone = dist_vwap20_atr <= P["max_atr_dist_vwap20"]
    g7_ok = (sma20_in_zone or vwap20_in_zone) if P["strike_zone_mode"] == "OR" \
            else (sma20_in_zone and vwap20_in_zone)

    flagged_count = d_cnt + s_cnt

    if   not slope_ok:
        out["failed_at"] = "G2_SMA20_SLOPE"
    elif P["require_sma50_gt_sma200"] and not sma50_gt_sma200:
        out["failed_at"] = "G3_MA_ALIGNMENT"
    elif stretch_from_sma50 > P["max_pct_above_sma50"]:
        out["failed_at"] = "G4_EXTENSION_CEIL"
    elif stretch_from_sma50 < P["min_pct_below_sma50"]:
        out["failed_at"] = "G4B_SMA50_FLOOR"
    elif stretch_from_sma200 > P["max_pct_above_sma200"]:
        out["failed_at"] = "G4C_SMA200_CEIL"
    elif np.isnan(stretch_pct) or stretch_pct < P["min_escape_pct"]:
        out["failed_at"] = "G5_ESCAPE_VEL"
    elif not (P["min_bars_since_peak"] <= bars_since_peak <= P["max_bars_since_peak"]):
        out["failed_at"] = "G5B_PEAK_AGE"
    elif P["require_peak_quality"] and not np.isnan(c2h) and c2h > P["peak_close_to_high"]:
        out["failed_at"] = "G5C_PEAK_QUALITY"
    elif touch_count > P["max_touches"]:
        out["failed_at"] = "G6_TOUCH_COUNT"
    elif not g7_ok:
        out["failed_at"] = "G7_STRIKE_ZONE"
    elif flagged_count > P["max_dist_days"]:
        out["failed_at"] = "G8_DISTRIBUTION"
    elif np.isnan(vol_ratio_3_50) or vol_ratio_3_50 >= P["vol_dry_ratio"]:
        out["failed_at"] = "G9_VOLUME_DRY"
    elif P["require_handle_tightness"] and (
        handle_range_atr_val     > P["max_handle_range_atr"]
        or handle_close_band_pct_val > P["max_handle_close_band_pct"]
    ):
        out["failed_at"] = "GH_HANDLE_TIGHTNESS"
    else:
        out["failed_at"] = "PASS"
        out["passed"]    = True

    return out


def run_minervini_diagnostics(sample_size: int = 500, params: dict | None = None) -> None:
    """
    Full telemetry run for find_minervini_pullback (Algorithm 1).

    Prints:
    1. Gate funnel (kill count, survivor count, pass-rate per gate).
    2. SMA20/VWAP20 divergence analysis (G7 geometry check).
    3. Per-gate metric analysis with "try relaxing to X" hints.
    """
    W = 72

    print("\n" + "=" * W)
    print("  MINERVINI LEADER PULLBACK — GATE TELEMETRY  (Algorithm 1)")
    print(f"  Params: {params if params else 'defaults (from config.py)'}")
    print("=" * W)

    req    = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets = trading_client.get_all_assets(req)
    all_symbols = [
        a.symbol for a in assets
        if a.tradable and a.fractionable and len(a.symbol) <= 4
    ]
    sample = random.sample(all_symbols, min(sample_size, len(all_symbols)))
    print(f"\n  Universe: {len(all_symbols)} symbols  |  Sample: {len(sample)}\n")

    end   = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=FETCH_DAYS)

    rows = []
    for i in tqdm(range(0, len(sample), CHUNK_SIZE), desc="Fetching"):
        chunk = sample[i:i + CHUNK_SIZE]
        try:
            bars = data_client.get_stock_bars(
                StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start, end=end)
            ).df
            if bars.empty:
                continue
            bars.rename(columns={"close": "Close", "high": "High", "low": "Low", "volume": "Volume"}, inplace=True)
            for sym in chunk:
                if sym not in bars.index:
                    continue
                df = bars.loc[sym].copy()
                if len(df) < 252:
                    continue
                price             = df["Close"].iloc[-1]
                avg_vol_20        = df["Volume"].tail(20).mean()
                avg_dollar_vol_20 = (df["Close"] * df["Volume"]).tail(20).mean()
                if price < MIN_PRICE or avg_vol_20 < MIN_AVG_VOLUME_20D or avg_dollar_vol_20 < MIN_AVG_DOLLAR_VOL_20D:
                    continue
                if is_buyout_or_flatline(df):
                    continue
                row            = _diagnose_minervini_pullback(df, params)
                row["symbol"]  = sym
                row["price_at_scan"] = price
                rows.append(row)
        except Exception as exc:
            logging.error(f"MPB diag chunk {chunk[0]}: {exc}")
        time.sleep(RATE_LIMIT_SLEEP)

    if not rows:
        print("  No data collected — check API credentials / network.")
        return

    diag  = pd.DataFrame(rows)
    total = len(diag)
    print(f"\n  Qualified tickers after pre-filters: {total}\n")

    # Resolve effective params for threshold display
    eff = dict(MPB_PARAMS)
    if params:
        eff.update(params)

    # 1. Gate funnel
    print(f"{'─' * W}")
    print(f"  {'Gate':<38}  {'Threshold':<20}  {'Killed':>6}  {'Alive':>6}  {'Pass%':>5}")
    print(f"{'─' * W}")

    threshold_labels = {
        "G1_INDICATOR_NAN":  "all non-NaN / non-zero",
        "G2_SMA20_SLOPE":    "today > 5d > 10d",
        "G3_MA_ALIGNMENT":   "SMA50 > SMA200",
        "G4_EXTENSION_CEIL": f"≤{eff['max_pct_above_sma50']*100:.0f}% above SMA50",
        "G4B_SMA50_FLOOR":   f"≥{abs(eff['min_pct_below_sma50'])*100:.0f}% below SMA50",
        "G4C_SMA200_CEIL":   f"≤{eff['max_pct_above_sma200']*100:.0f}% above SMA200",
        "G5_ESCAPE_VEL":     f"peak ≥{eff['min_escape_pct']:.0f}% above SMA20",
        "G5B_PEAK_AGE":      f"{eff['min_bars_since_peak']}–{eff['max_bars_since_peak']} bars ago",
        "G5C_PEAK_QUALITY":  f"(H-C)/(H-L) ≤{eff['peak_close_to_high']:.2f}",
        "G6_TOUCH_COUNT":    f"≤{eff['max_touches']} touches in {eff['touch_lookback']}b",
        "G7_STRIKE_ZONE":    f"≤{eff['max_atr_dist_sma20']:.1f} ATR (OR mode)",
        "G8_DISTRIBUTION":   f"≤{eff['max_dist_days']} flagged in {eff['dist_lookback']}b",
        "G9_VOLUME_DRY":     f"3d < {eff['vol_dry_ratio']:.2f}× 50d avg",
        "GH_HANDLE_TIGHTNESS": f"range≤{eff['max_handle_range_atr']}ATR, band≤{eff['max_handle_close_band_pct']*100:.0f}%",
    }

    alive = total
    for gate in _MPB_GATE_ORDER:
        killed = int((diag["failed_at"] == gate).sum())
        alive -= killed
        pct    = alive / total * 100
        lbl    = _MPB_GATE_LABELS.get(gate, gate)
        thresh = threshold_labels.get(gate, "")
        print(f"  {lbl}  {thresh:<20}  {killed:>6}  {alive:>6}  {pct:>4.1f}%")

    passed = int(diag["passed"].sum())
    print(f"{'─' * W}")
    print(f"  {'FINAL PASS':<60}  {passed:>6}  {passed/total*100:>4.1f}%")
    print(f"{'─' * W}\n")

    # 2. SMA20 / VWAP20 divergence
    print("  G7 GEOMETRY ANALYSIS  (reveals impossible confluence zones)")
    print(f"  {'Metric':<45}  {'Value':>8}")
    print(f"  {'─'*55}")
    gap_col = "sma20_vwap20_gap_atr"
    if gap_col in diag.columns:
        gap_vals = diag[gap_col].dropna()
        p50, p75, p90 = gap_vals.quantile([0.50, 0.75, 0.90])
        print(f"  {'Median  |SMA20 - VWAP20| in ATR units':<45}  {p50:>7.3f}")
        print(f"  {'75th percentile':<45}  {p75:>7.3f}")
        print(f"  {'90th percentile':<45}  {p90:>7.3f}")
        threshold = eff["max_atr_dist_sma20"]
        impossible = int((gap_vals > 2 * threshold).sum())
        print(f"  {'Tickers where gap > 2×ATR threshold (impossible zone)':<45}  {impossible:>7d}")
    print()

    # 3. Per-gate bottleneck analysis
    print("  PER-GATE MEDIAN  (failing tickers: actual value vs threshold)")
    print(f"{'─' * W}")

    gate_metric_map = {
        "G2_SMA20_SLOPE":    ("sma20_slope_ok",        None,  "bool",  None),
        "G3_MA_ALIGNMENT":   ("sma50_gt_sma200",        None,  "bool",  None),
        "G4_EXTENSION_CEIL": ("stretch_from_sma50_pct", 25.0,  "le",    0.30),
        "G4B_SMA50_FLOOR":   ("stretch_from_sma50_pct", -2.0,  "ge",   -0.04),
        "G4C_SMA200_CEIL":   ("stretch_from_sma200_pct",60.0,  "le",    0.70),
        "G5_ESCAPE_VEL":     ("stretch_pct",             eff["min_escape_pct"], "ge", eff["min_escape_pct"] - 2),
        "G5B_PEAK_AGE":      ("bars_since_peak",         None,  "range", None),
        "G5C_PEAK_QUALITY":  ("close_to_high_ratio",     0.40,  "le",    0.50),
        "G6_TOUCH_COUNT":    ("touch_count",             eff["max_touches"], "le", eff["max_touches"] + 1),
        "G7_STRIKE_ZONE":    ("dist_sma20_atr",          1.0,   "le",    1.5),
        "G8_DISTRIBUTION":   ("flagged_count",           3,     "le",    4),
        "G9_VOLUME_DRY":        ("vol_ratio_3_50",          1.00,  "le",    1.10),
        "GH_HANDLE_TIGHTNESS":  ("handle_range_atr",        eff["max_handle_range_atr"], "le", eff["max_handle_range_atr"] + 0.5),
    }

    kill_series = diag["failed_at"].value_counts()

    for gate in _MPB_GATE_ORDER:
        n_killed = int(kill_series.get(gate, 0))
        if n_killed == 0 or gate not in gate_metric_map:
            continue

        metric, threshold, mode, hint = gate_metric_map[gate]
        lbl = _MPB_GATE_LABELS.get(gate, gate).strip()
        failing_rows = diag[diag["failed_at"] == gate]

        print(f"\n  {lbl}  —  {n_killed} tickers killed ({n_killed/total*100:.1f}%)")

        if mode == "bool":
            print(f"    Threshold : must be True")
            print(f"    All {n_killed} failing tickers had False")
            continue

        col_data = failing_rows[metric].dropna()
        if col_data.empty:
            print(f"    No data for metric '{metric}'")
            continue

        med = col_data.median()
        p25 = col_data.quantile(0.25)
        p75 = col_data.quantile(0.75)

        print(f"    Metric    : {metric}")
        if mode in ("le", "ge") and threshold is not None:
            direction = "≤" if mode == "le" else "≥"
            print(f"    Threshold : {direction} {threshold}")
            print(f"    Median    : {med:.3f}   [p25={p25:.3f}, p75={p75:.3f}]")
            gap  = abs(med - threshold)
            side = "above" if med > threshold else "below"
            print(f"    Gap       : {gap:.3f} {side} threshold")
            if hint is not None:
                print(f"    Try       : relax threshold to {hint}")
        elif mode == "range":
            p_min = eff["min_bars_since_peak"]
            p_max = eff["max_bars_since_peak"]
            print(f"    Threshold : between {p_min} and {p_max} bars")
            print(f"    Median    : {med:.1f}   [p25={p25:.1f}, p75={p75:.1f}]")
            too_young = int((failing_rows[metric] < p_min).sum())
            too_old   = int((failing_rows[metric] > p_max).sum())
            print(f"    Too young (<{p_min} bars): {too_young}  |  Too old (>{p_max} bars): {too_old}")
            if too_old > too_young:
                print(f"    Try       : widen max_bars_since_peak to {p_max + 10}")
            else:
                print(f"    Try       : lower min_bars_since_peak to {max(2, p_min - 2)}")

    print(f"\n{'=' * W}\n")


# ── Algorithm 2 diagnostics (Power Trend Pullback) ────────────────────────────
#
# The four user-spec gates are split into ten testable sub-gates so the
# funnel reveals exactly which conjunct kills each ticker. The mirror
# reuses PowerTrendPullbackEngine's vectorized indicator stack, then
# evaluates every sub-gate on the latest bar regardless of earlier
# failures (non-short-circuiting).

_PTP_GATE_ORDER = [
    "G1_INDICATOR_NAN",
    "G2A_MA_HIERARCHY",
    "G2B_LONG_TERM_TREND",
    "G2C_MOMENTUM_VELOCITY",
    "G2D_TREND_STRENGTH",
    "G3A_STRETCH",
    "G3B_RESTING_ON_EMA",
    "G3C_LH_LL_STREAK",
    "G3D_VOLUME_DRY",
    "G4_RISK_CAP",
]

_PTP_GATE_LABELS = {
    "G1_INDICATOR_NAN":     "G1   Indicators computable       ",
    "G2A_MA_HIERARCHY":     "G2a  MA hierarchy (EMA10>21>50)  ",
    "G2B_LONG_TERM_TREND":  "G2b  Long-term trend (50>200/up) ",
    "G2C_MOMENTUM_VELOCITY":"G2c  40d return ≥ momentum_pct   ",
    "G2D_TREND_STRENGTH":   "G2d  ADX(14) > min_adx           ",
    "G3A_STRETCH":          "G3a  5d high ≥ stretch above EMA ",
    "G3B_RESTING_ON_EMA":   "G3b  Close within close_band     ",
    "G3C_LH_LL_STREAK":     "G3c  Down streak in [min,max]    ",
    "G3D_VOLUME_DRY":       "G3d  Volume < 50d SMA(Vol)       ",
    "G4_RISK_CAP":          "G4   Risk_pct ≤ cap              ",
}


def _diagnose_power_trend_pullback(df: pd.DataFrame, params: dict | None = None) -> dict:
    """
    Non-short-circuiting diagnostic mirror of PowerTrendPullbackEngine.

    Runs every PTP sub-gate on the latest bar regardless of earlier
    failures and returns a flat metric dict. The first failing gate is
    recorded under ``failed_at``; ``passed`` is True only when every
    sub-gate clears.

    Sub-gates  (v2.0 contract)
    --------------------------
    G1   Indicators non-NaN
    G2a  EMA10 > EMA21 > SMA50                                 (regime)
         Note: ``Close > EMA10`` is NOT part of the v2.0 gate; it's
         recorded as an advisory metric only.
    G2b  SMA50 > SMA200 OR SMA50 trended up for ≥ N bars       (regime)
    G2c  40-bar return ≥ min_momentum_pct                      (regime)
    G2d  ADX(14) > min_adx                                     (regime)
    G3a  Max(High/EMA21 − 1) over last 5 bars ≥ stretch %      (pullback)
    G3b  min(|C−EMA10|/EMA10, |C−EMA21|/EMA21) ≤ close_band    (pullback)
    G3c  down_streak ∈ [min_pullback_days, max_pullback_days]  (pullback)
    G3d  Volume < 50-bar SMA(Volume)                           (pullback)
    G4   risk_pct ≤ max_risk_pct                               (risk)
    """
    P = dict(PTP_PARAMS)
    if params:
        P.update(params)

    out: dict = {
        "passed": False, "failed_at": "INSUFFICIENT_DATA",
        # Indicators
        "price": np.nan, "ema10": np.nan, "ema21": np.nan,
        "sma50": np.nan, "sma200": np.nan, "atr14": np.nan,
        "vol_sma50": np.nan,
        # Regime (G2)
        "close_gt_ema10": False, "ema10_gt_ema21": False, "ema21_gt_sma50": False,
        "ma_hierarchy_ok": False,
        "sma50_gt_sma200": False, "sma50_slope_up_20d": False,
        "long_term_trend_ok": False,
        "ret_40d_pct": np.nan,
        "adx14": np.nan,
        # Pullback (G3)
        "stretch_5d_pct": np.nan,
        "close_band_ema10_pct": np.nan, "close_band_ema21_pct": np.nan,
        "best_close_band_pct": np.nan,
        "down_streak": np.nan,
        "vol_drying": False,
        # Execution / risk (G4)
        "trigger_price": np.nan, "stop_price": np.nan,
        "pullback_low": np.nan, "risk_pct": np.nan,
    }

    if len(df) < P["min_bars"]:
        return out

    eng = PowerTrendPullbackEngine(P)
    try:
        with_ind  = eng._calculate_moving_averages(df)
        with_pb   = eng._identify_pullback(with_ind)
        with_exec = eng._calculate_execution_levels(with_pb)
    except Exception:
        return out

    last = with_exec.iloc[-1]

    # ── Indicator snapshot ────────────────────────────────────────────────────
    out.update({
        "price":     round(float(last["Close"]), 2),
        "ema10":     round(float(last["ema10"]),  4) if pd.notna(last["ema10"])  else np.nan,
        "ema21":     round(float(last["ema21"]),  4) if pd.notna(last["ema21"])  else np.nan,
        "sma50":     round(float(last["sma50"]),  4) if pd.notna(last["sma50"])  else np.nan,
        "sma200":    round(float(last["sma200"]), 4) if pd.notna(last["sma200"]) else np.nan,
        "atr14":     round(float(last["atr14"]),  4) if pd.notna(last["atr14"])  else np.nan,
        "vol_sma50": round(float(last["vol_sma50"]), 0) if pd.notna(last["vol_sma50"]) else np.nan,
    })

    # ── G1: indicator computability ──────────────────────────────────────────
    g1_ok = not any(
        pd.isna(last[c]) or last[c] == 0
        for c in ("ema10", "ema21", "sma50", "sma200", "atr14", "vol_sma50")
    )
    if not g1_ok:
        out["failed_at"] = "G1_INDICATOR_NAN"
        return out

    # ── G2a: MA hierarchy (v2.0 — Close > EMA10 is advisory only) ────────────
    # The engine intentionally does NOT require Close > EMA10. We still
    # record the leg so the decomposition section can show how often the
    # close was below EMA10 — useful for sanity-checking the relaxation
    # didn't unintentionally let in stage-4 breakdowns.
    close_gt_ema10  = bool(last["Close"] > last["ema10"])
    ema10_gt_ema21  = bool(last["ema10"] > last["ema21"])
    ema21_gt_sma50  = bool(last["ema21"] > last["sma50"])
    ma_hierarchy_ok = ema10_gt_ema21 and ema21_gt_sma50
    out.update({
        "close_gt_ema10":  close_gt_ema10,    # advisory only (NOT in gate)
        "ema10_gt_ema21":  ema10_gt_ema21,
        "ema21_gt_sma50":  ema21_gt_sma50,
        "ma_hierarchy_ok": ma_hierarchy_ok,
    })

    # ── G2b: long-term trend (SMA50>SMA200 OR SMA50 rose for N bars) ─────────
    sma50_gt_sma200    = bool(last["sma50"] > last["sma200"])
    sma50_slope_up_20d = bool(last["sma50_slope_up_20d"])
    long_term_trend_ok = sma50_gt_sma200 or sma50_slope_up_20d
    out.update({
        "sma50_gt_sma200":    sma50_gt_sma200,
        "sma50_slope_up_20d": sma50_slope_up_20d,
        "long_term_trend_ok": long_term_trend_ok,
    })

    # ── G2c: momentum velocity (40-bar return ≥ +20%) ────────────────────────
    ret_40d_val = float(last["ret_40d"]) if pd.notna(last["ret_40d"]) else np.nan
    out["ret_40d_pct"] = round(ret_40d_val * 100, 2) if not np.isnan(ret_40d_val) else np.nan
    velocity_ok = (not np.isnan(ret_40d_val)) and (ret_40d_val >= P["min_momentum_pct"])

    # ── G2d: trend strength (ADX > 30) ───────────────────────────────────────
    adx_val = float(last["adx14"]) if pd.notna(last["adx14"]) else np.nan
    out["adx14"] = round(adx_val, 2) if not np.isnan(adx_val) else np.nan
    trend_strength_ok = (not np.isnan(adx_val)) and (adx_val > P["min_adx"])

    # ── G3a: 5-bar stretch ≥ pullback_stretch_pct ────────────────────────────
    stretch_val = float(last["stretch_5d"]) if pd.notna(last["stretch_5d"]) else np.nan
    out["stretch_5d_pct"] = round(stretch_val * 100, 2) if not np.isnan(stretch_val) else np.nan
    stretch_ok = (not np.isnan(stretch_val)) and (stretch_val >= P["min_pullback_stretch_pct"])

    # ── G3b: resting on EMA10 OR EMA21 within close_band_pct ─────────────────
    cb10 = float(last["close_band_ema10"]) if pd.notna(last["close_band_ema10"]) else np.nan
    cb21 = float(last["close_band_ema21"]) if pd.notna(last["close_band_ema21"]) else np.nan
    best_cb = np.nan if (np.isnan(cb10) and np.isnan(cb21)) else np.nanmin([cb10, cb21])
    out.update({
        "close_band_ema10_pct": round(cb10 * 100, 3) if not np.isnan(cb10) else np.nan,
        "close_band_ema21_pct": round(cb21 * 100, 3) if not np.isnan(cb21) else np.nan,
        "best_close_band_pct":  round(float(best_cb) * 100, 3) if not np.isnan(best_cb) else np.nan,
    })
    resting_ok = (not np.isnan(best_cb)) and (best_cb <= P["close_band_pct"])

    # ── G3c: 2-3 day LH+LL streak ending today ───────────────────────────────
    streak_val = int(last["down_streak"]) if pd.notna(last["down_streak"]) else 0
    out["down_streak"] = streak_val
    streak_ok = P["min_pullback_days"] <= streak_val <= P["max_pullback_days"]

    # ── G3d: volume dry-up ───────────────────────────────────────────────────
    vol_drying = bool(last["vol_drying"])
    out["vol_drying"] = vol_drying

    # ── G4: risk cap ─────────────────────────────────────────────────────────
    risk_pct_val = float(last["risk_pct"]) if pd.notna(last["risk_pct"]) else np.nan
    out.update({
        "trigger_price": round(float(last["trigger_price"]), 4) if pd.notna(last["trigger_price"]) else np.nan,
        "stop_price":    round(float(last["stop_price"]),    4) if pd.notna(last["stop_price"])    else np.nan,
        "pullback_low":  round(float(last["pullback_low"]),  4) if pd.notna(last["pullback_low"])  else np.nan,
        "risk_pct":      round(risk_pct_val, 3) if not np.isnan(risk_pct_val) else np.nan,
    })
    risk_ok = (not np.isnan(risk_pct_val)) and (risk_pct_val <= P["max_risk_pct"])

    # ── First failing gate (linear order) ────────────────────────────────────
    if   not ma_hierarchy_ok:    out["failed_at"] = "G2A_MA_HIERARCHY"
    elif not long_term_trend_ok: out["failed_at"] = "G2B_LONG_TERM_TREND"
    elif not velocity_ok:        out["failed_at"] = "G2C_MOMENTUM_VELOCITY"
    elif not trend_strength_ok:  out["failed_at"] = "G2D_TREND_STRENGTH"
    elif not stretch_ok:         out["failed_at"] = "G3A_STRETCH"
    elif not resting_ok:         out["failed_at"] = "G3B_RESTING_ON_EMA"
    elif not streak_ok:          out["failed_at"] = "G3C_LH_LL_STREAK"
    elif not vol_drying:         out["failed_at"] = "G3D_VOLUME_DRY"
    elif not risk_ok:            out["failed_at"] = "G4_RISK_CAP"
    else:
        out["failed_at"] = "PASS"
        out["passed"]    = True

    return out


def run_ptp_diagnostics(sample_size: int = 500, params: dict | None = None) -> None:
    """
    Full telemetry run for the Power Trend Pullback engine (Algorithm 2).

    Prints
    ------
    1. Gate funnel — per-sub-gate kill count, surviving count, pass-rate.
    2. MA-hierarchy decomposition — which leg of the four-way conjunction
       (Close>EMA10, EMA10>EMA21, EMA21>SMA50) is the most common
       offender among G2a failures.
    3. Long-term-trend decomposition — share of tickers that fail BOTH
       SMA50>SMA200 and the 20-bar slope alternative.
    4. Per-gate medians for every numeric gate, with "try relaxing to X"
       hints when the failing-population median is close to the threshold.
    """
    W = 76

    print("\n" + "=" * W)
    print("  POWER TREND PULLBACK — GATE TELEMETRY  (Algorithm 2)")
    print(f"  Params: {params if params else 'defaults (from config.py)'}")
    print("=" * W)

    req    = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets = trading_client.get_all_assets(req)
    all_symbols = [
        a.symbol for a in assets
        if a.tradable and a.fractionable and len(a.symbol) <= 4
    ]
    sample = random.sample(all_symbols, min(sample_size, len(all_symbols)))
    print(f"\n  Universe: {len(all_symbols)} symbols  |  Sample: {len(sample)}\n")

    end   = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=FETCH_DAYS)

    rows: list[dict] = []
    for i in tqdm(range(0, len(sample), CHUNK_SIZE), desc="Fetching"):
        chunk = sample[i:i + CHUNK_SIZE]
        try:
            bars = data_client.get_stock_bars(
                StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start, end=end)
            ).df
            if bars.empty:
                continue
            bars.rename(columns={"close": "Close", "high": "High", "low": "Low", "volume": "Volume"}, inplace=True)
            for sym in chunk:
                if sym not in bars.index:
                    continue
                df = bars.loc[sym].copy()
                if len(df) < 252:
                    continue
                price             = df["Close"].iloc[-1]
                avg_vol_20        = df["Volume"].tail(20).mean()
                avg_dollar_vol_20 = (df["Close"] * df["Volume"]).tail(20).mean()
                if price < MIN_PRICE or avg_vol_20 < MIN_AVG_VOLUME_20D or avg_dollar_vol_20 < MIN_AVG_DOLLAR_VOL_20D:
                    continue
                if is_buyout_or_flatline(df):
                    continue
                row = _diagnose_power_trend_pullback(df, params)
                row["symbol"]        = sym
                row["price_at_scan"] = price
                rows.append(row)
        except Exception as exc:
            logging.error(f"PTP diag chunk {chunk[0]}: {exc}")
        time.sleep(RATE_LIMIT_SLEEP)

    if not rows:
        print("  No data collected — check API credentials / network.")
        return

    diag  = pd.DataFrame(rows)
    total = len(diag)
    print(f"\n  Qualified tickers after pre-filters: {total}\n")

    eff = dict(PTP_PARAMS)
    if params:
        eff.update(params)

    # ── 1. Gate funnel ────────────────────────────────────────────────────────
    threshold_labels = {
        "G1_INDICATOR_NAN":      "non-NaN/non-zero",
        "G2A_MA_HIERARCHY":      "structural",
        "G2B_LONG_TERM_TREND":   f"50>200 OR up{eff['sma50_slope_window']}b",
        "G2C_MOMENTUM_VELOCITY": f"≥{eff['min_momentum_pct']*100:.0f}% in {eff['momentum_window']}b",
        "G2D_TREND_STRENGTH":    f"ADX > {eff['min_adx']:.0f}",
        "G3A_STRETCH":           f"≥{eff['min_pullback_stretch_pct']*100:.0f}% above EMA21",
        "G3B_RESTING_ON_EMA":    f"≤{eff['close_band_pct']*100:.1f}% from EMA",
        "G3C_LH_LL_STREAK":      f"streak in [{eff['min_pullback_days']},{eff['max_pullback_days']}]",
        "G3D_VOLUME_DRY":        "Vol < 50d SMA(Vol)",
        "G4_RISK_CAP":           f"≤ {eff['max_risk_pct']:.1f}%",
    }

    print(f"{'─' * W}")
    print(f"  {'Gate':<36}  {'Threshold':<22}  {'Killed':>6}  {'Alive':>6}  {'Pass%':>5}")
    print(f"{'─' * W}")

    alive = total
    for gate in _PTP_GATE_ORDER:
        killed = int((diag["failed_at"] == gate).sum())
        alive -= killed
        pct    = alive / total * 100
        lbl    = _PTP_GATE_LABELS.get(gate, gate)
        thresh = threshold_labels.get(gate, "")
        print(f"  {lbl}  {thresh:<22}  {killed:>6}  {alive:>6}  {pct:>4.1f}%")

    passed = int(diag["passed"].sum())
    print(f"{'─' * W}")
    print(f"  {'FINAL PASS':<60}  {passed:>6}  {passed/total*100:>4.1f}%")
    print(f"{'─' * W}\n")

    # ── 2. MA-hierarchy decomposition (v2.0 — only EMA10>EMA21 and EMA21>SMA50
    #       gate the regime; Close>EMA10 is reported as advisory.) ─────────────
    g2a_kills = diag[diag["failed_at"] == "G2A_MA_HIERARCHY"]
    if len(g2a_kills) > 0:
        n = len(g2a_kills)
        leg_fail_e1021 = int((~g2a_kills["ema10_gt_ema21"]).sum())
        leg_fail_e2150 = int((~g2a_kills["ema21_gt_sma50"]).sum())
        print("  G2a MA-HIERARCHY DECOMPOSITION  (which gating leg breaks)")
        print(f"  {'─'*60}")
        print(f"  {'EMA10 > EMA21':<32}  failed in {leg_fail_e1021:>4} / {n} ({leg_fail_e1021/n*100:.1f}%)")
        print(f"  {'EMA21 > SMA50':<32}  failed in {leg_fail_e2150:>4} / {n} ({leg_fail_e2150/n*100:.1f}%)")
        print()

    # Universe-level advisory: how often is Close ≤ EMA10 across the whole
    # qualified sample? In v2.0 this is NOT a kill — it's the very condition
    # we expect during the dip — but a >70% reading would be a smell test
    # signal that the trend has broken globally.
    if "close_gt_ema10" in diag.columns:
        n_total      = len(diag)
        n_below_ema10 = int((~diag["close_gt_ema10"]).sum())
        print("  ADVISORY  (Close > EMA10 is NOT a v2.0 gate — informational)")
        print(f"  {'─'*60}")
        print(f"  {'Close ≤ EMA10 across qualified sample':<44}  "
              f"{n_below_ema10:>5} / {n_total} ({n_below_ema10/n_total*100:.1f}%)")
        print()

    # ── 3. Long-term-trend decomposition ─────────────────────────────────────
    g2b_kills = diag[diag["failed_at"] == "G2B_LONG_TERM_TREND"]
    if len(g2b_kills) > 0:
        n = len(g2b_kills)
        leg_fail_50_200 = int((~g2b_kills["sma50_gt_sma200"]).sum())
        leg_fail_slope  = int((~g2b_kills["sma50_slope_up_20d"]).sum())
        both_failed     = int(((~g2b_kills["sma50_gt_sma200"]) & (~g2b_kills["sma50_slope_up_20d"])).sum())
        print("  G2b LONG-TERM TREND DECOMPOSITION")
        print(f"  {'─'*60}")
        print(f"  {'SMA50 > SMA200':<32}  failed in {leg_fail_50_200:>4} / {n}")
        print(f"  {'SMA50 rose for 20 bars':<32}  failed in {leg_fail_slope:>4} / {n}")
        print(f"  {'BOTH legs failed (= G2b kill)':<32}  {both_failed:>4} / {n} ({both_failed/n*100:.1f}%)")
        print()

    # ── 4. Per-gate medians + relax hints ────────────────────────────────────
    print("  PER-GATE MEDIAN  (failing tickers: actual value vs threshold)")
    print(f"{'─' * W}")

    # (metric_col, threshold, comparison, suggested_relaxed_threshold)
    gate_metric_map: dict[str, tuple[str, float, str, float]] = {
        "G2C_MOMENTUM_VELOCITY": ("ret_40d_pct",        eff["min_momentum_pct"]*100,  "ge", max(0.0, eff["min_momentum_pct"]*100 - 5)),
        "G2D_TREND_STRENGTH":    ("adx14",              eff["min_adx"],               "gt", max(15.0, eff["min_adx"] - 5)),
        "G3A_STRETCH":           ("stretch_5d_pct",     eff["min_pullback_stretch_pct"]*100, "ge", max(0.0, eff["min_pullback_stretch_pct"]*100 - 3)),
        "G3B_RESTING_ON_EMA":    ("best_close_band_pct", eff["close_band_pct"]*100,   "le", eff["close_band_pct"]*100 + 1.0),
        "G3C_LH_LL_STREAK":      ("down_streak",        float(eff["max_pullback_days"]), "range", 0.0),
        "G4_RISK_CAP":           ("risk_pct",           eff["max_risk_pct"],          "le", eff["max_risk_pct"] + 1.5),
    }

    kill_series = diag["failed_at"].value_counts()

    for gate in _PTP_GATE_ORDER:
        if gate not in gate_metric_map:
            continue
        n_killed = int(kill_series.get(gate, 0))
        if n_killed == 0:
            continue

        metric, threshold, mode, hint = gate_metric_map[gate]
        lbl = _PTP_GATE_LABELS.get(gate, gate).strip()
        failing_rows = diag[diag["failed_at"] == gate]
        col_data = failing_rows[metric].dropna()

        print(f"\n  {lbl}  —  {n_killed} tickers killed ({n_killed/total*100:.1f}%)")
        if col_data.empty:
            print(f"    No data for metric '{metric}'")
            continue

        med = col_data.median()
        p25 = col_data.quantile(0.25)
        p75 = col_data.quantile(0.75)
        print(f"    Metric    : {metric}")

        if mode in ("le", "ge", "gt"):
            direction = {"le": "≤", "ge": "≥", "gt": ">"}[mode]
            print(f"    Threshold : {direction} {threshold:.3f}")
            print(f"    Median    : {med:.3f}   [p25={p25:.3f}, p75={p75:.3f}]")
            gap  = abs(med - threshold)
            side = "above" if med > threshold else "below"
            print(f"    Gap       : {gap:.3f} {side} threshold")
            print(f"    Try       : relax threshold to {hint:.3f}")
        elif mode == "range":
            p_min = eff["min_pullback_days"]
            p_max = eff["max_pullback_days"]
            print(f"    Threshold : streak ∈ [{p_min}, {p_max}]")
            print(f"    Median    : {med:.1f}   [p25={p25:.1f}, p75={p75:.1f}]")
            too_short = int((failing_rows[metric] < p_min).sum())
            too_long  = int((failing_rows[metric] > p_max).sum())
            print(f"    Too short (<{p_min}): {too_short}  |  Too long (>{p_max}): {too_long}")
            if too_long > too_short:
                print(f"    Try       : widen max_pullback_days to {p_max + 1}")
            else:
                print(f"    Try       : lower min_pullback_days to {max(1, p_min - 1)}")

    print(f"\n{'=' * W}\n")

