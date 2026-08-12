# ============================================================
#  config.py  —  Single source of truth for all tunable knobs
#
#  Change a value here → it propagates everywhere automatically.
#  No need to grep through algorithm code when fine-tuning.
# ============================================================

# ── API Credentials ──────────────────────────────────────────
API_KEY    = "PKQ2XILCAMFJLKF4AM3JIJYXLY"
API_SECRET = "8QDtrLFfY25TKrWedBRkxyXbw4e8sWcpu5wA1PZ2X1y7"

# ── Data fetch window ────────────────────────────────────────
FETCH_DAYS       = 400    # calendar days of history to pull (~280 trading days)
RATE_LIMIT_SLEEP = 0.3    # seconds between API chunk calls (free-tier: ~200 req/min)
CHUNK_SIZE       = 100    # symbols per batch request

# ── Global liquidity pre-filter ──────────────────────────────
MIN_PRICE              = 12.0         # skip penny stocks
MIN_AVG_VOLUME_20D     = 400_000      # shares / day
MIN_AVG_DOLLAR_VOL_20D = 15_000_000   # USD / day

# ── Output ───────────────────────────────────────────────────
TOP_N = 15   # max results per screener section

# ── Algorithm 1: Minervini Leader Pullback ───────────────────
#    Every gate has its own block so you can find/tune each threshold
#    in isolation without scrolling through algorithm code.
MPB_PARAMS: dict = {

    # Minimum history bars (SMA200 needs 200 + buffer)
    "min_bars": 252,

    # G2 — SMA20 slope checkpoints (bars ago)
    "slope_5d_offset":  5,
    "slope_10d_offset": 10,

    # G3 — long-term MA alignment
    "require_sma50_gt_sma200": True,

    # G4 — two-sided SMA50 band + SMA200 ceiling
    #   ceiling: avoid chasing a parabola already far extended
    #   floor  : reject breakdowns below the intermediate MA
    "max_pct_above_sma50":  0.25,   # 25% above SMA50  — ceiling
    "min_pct_below_sma50": -0.04,   # -4% below SMA50  — floor
    "max_pct_above_sma200": 0.60,   # 60% above SMA200 — ceiling

    # G5 — escape velocity
    "escape_lookback":     40,      # bars to search for the peak high
    "min_escape_pct":       6.0,    # ← relaxed from 8.0: peak must be ≥6% above SMA20
    "min_bars_since_peak":  1,      # ← lowered from 5: less time needed to pull back
    "max_bars_since_peak": 38,      # peak can't be stale (> ~8 weeks old)

    # G5c — peak-day close quality (institutional conviction on breakout day)
    #   0 = closed at the high, 1 = closed at the low
    "require_peak_quality": True,
    "peak_close_to_high":   0.85, # change from 0.40

    # Optional 20-bar momentum guard (None = disabled)
    "min_momentum_20d": None,       # e.g. 1.05 means price must be +5% vs 20d ago

    # G6 — touch count (first/early touch filter)
    #   "Touch" = Low grazed SMA20 zone AND Close held above (found support)
    "touch_lookback":   30,         # bars evaluated (excludes today)
    "touch_band":       0.005,      # Low <= SMA20*(1+0.005) counts as touching
    "close_hold_band":  0.015,      # Close >= SMA20*(1-0.015) = didn't collapse
    "max_touches":      6,          # ← relaxed from 2: allow up to 3 prior touches

    # G7 — strike zone (ATR-normalised, OR mode by default)
    "atr_period":           14,
    "max_atr_dist_sma20":   1.0,    # within 1.0 ATR of SMA20
    "max_atr_dist_vwap20":  1.0,    # within 1.0 ATR of VWAP20
    "strike_zone_mode":    "OR",    # "OR" = near either; "AND" = near both
    "max_pct_dist_sma20":  None,    # optional %-cap (None = disabled)
    "max_pct_dist_vwap20": None,

    # G8 — distribution / stall day filter
    "dist_lookback":    15,         # bars to scan (today excluded)
    "dist_min_drop":    0.01,       # return ≤ -1.0% = distribution day
    "max_dist_days":     3,         # combined dist + stall days allowed
    "stall_vol_mult":   1.15,       # stall: vol > 1.15× 50d avg on flat close
    "heavy_vol_mult":   None,       # optional: reject heavy-sell day (None = off)

    # G9 — volume dry-up
    "vol_dry_bars":      3,         # recent average window
    "vol_baseline_bars": 50,        # baseline average window
    "vol_dry_ratio":     1.00,      # recent must be < 100% of baseline

    # F — entry / trigger levels
    "handle_bars":        5,        # bars defining the handle window
    "stop_lookback_bars": 5,        # bars to find the recent pullback low for the stop
    "stop_buffer_pct":    0.005,    # 0.5% below the pullback low (volatility buffer)
    # stop_atr_mult is kept for reference output only; it does NOT override the
    # structure stop.  If the chart is too wide, risk_cap_blocked fires instead.
    "stop_atr_mult":      0.50,     # SMA20 - 0.5*ATR shown as ma_stop in output
    "max_risk_pct":       7.5,      # hard cap: trigger blocked when risk > 7.5%

    # GH — handle tightness gate (applied before entry/trigger are computed)
    # Prevents entries on sloppy, wide handles caused by a single tall wick.
    "require_handle_tightness":   True,
    "max_handle_range_atr":       1.5,   # (max_high - min_low) / ATR14 over handle window
    "max_handle_close_band_pct":  0.03,  # (max_close - min_close) / handle_ceiling ≤ 3%
}

# ── Algorithm 2: Power Trend Pullback (PTP)  — v2.0 retune ───
#    Captures secular momentum stocks that never pause long enough to
#    form a multi-week base. The v1 thresholds were too rigid (0% pass
#    on a 6.6k-symbol universe); v2 is the data-driven retune that
#    keeps the strict risk architecture but widens the regime / pullback
#    nets to where real leaders actually live.
PTP_PARAMS: dict = {

    # Minimum history bars (SMA200 + buffer for indicator warm-up)
    "min_bars": 252,

    # ── G1: regime filter ────────────────────────────────────
    # Moving-average hierarchy is enforced structurally inside
    # PowerTrendPullbackEngine._detect_power_regime as
    #     EMA10 > EMA21 > SMA50.
    # `Close > EMA10` is intentionally NOT required — by construction
    # the price must be allowed to slide below EMA10 to reach EMA21 for
    # a tradable pullback. Removing that conjunct is the single largest
    # functional change in v2.0.
    "sma50_slope_window":   20,     # SMA50 must rise for ≥20 consecutive bars
                                    # (alternative path when SMA50 ≤ SMA200)
    "momentum_window":      40,     # 40 trading days = 8 weeks
    "min_momentum_pct":     0.15,   # v2.0: relaxed from 0.20 → ≥ 15% in 40b
    "min_adx":              25.0,   # v2.0: relaxed from 30 → ADX(14) > 25
    "adx_period":           14,
    "atr_period":           14,

    # ── G2: pullback mechanics ───────────────────────────────
    "stretch_lookback":         5,      # bars to look for the pre-pullback stretch
    "min_pullback_stretch_pct": 0.07,   # v2.0: relaxed from 0.10 → ≥ 7% above EMA21
    "close_band_pct":           0.03,   # v2.0: widened from 0.015 → within 3.0%
    "min_pullback_days":        1,      # v2.0: 1-3 (was 2-3) — capture 1-day hooks
    "max_pullback_days":        3,
    "vol_baseline_bars":        50,     # 50-bar SMA(Volume) baseline for volume dry-up
                                        # (volume dry-up is intentionally NOT relaxed)

    # ── G3: execution trigger ────────────────────────────────
    # Price-cross trigger uses the high of the lowest day in the LH/LL
    # streak (= today's High by construction). The intraday RVOL gate
    # fires in the live execution layer; only the threshold is
    # advertised here so the trader knows what bar it must clear.
    "intraday_rvol_min":    1.3,        # same-time-of-day RVOL ≥ 1.3 over 10d

    # ── G4: risk management ──────────────────────────────────
    "stop_atr_mult":         1.0,       # 1·ATR14 below the pullback low
    "stop_ema_buffer_pct":   0.001,     # EMA21 stop sits 10 bps below the EMA21
    "max_risk_pct":          7.0,       # hard cap — reject above 7% risk
}
