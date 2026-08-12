import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import time
from tqdm import tqdm
import logging
import warnings

# Alpaca Imports
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus

warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)
logging.basicConfig(filename='alpaca_scanner_errors.log', level=logging.WARNING)

# ================= API CREDENTIALS =================
API_KEY    = "PKQ2XILCAMFJLKF4AM3JIJYXLY"
API_SECRET = "8QDtrLFfY25TKrWedBRkxyXbw4e8sWcpu5wA1PZ2X1y7"

trading_client = TradingClient(API_KEY, API_SECRET, paper=True)
data_client    = StockHistoricalDataClient(API_KEY, API_SECRET)


# ================= INDICATORS & HELPERS =================

def calculate_rs(stock_df, index_df, period=60):
    """Relative Strength vs benchmark over `period` trading days."""
    try:
        aligned = pd.merge(
            stock_df['Close'], index_df['Close'],
            left_index=True, right_index=True,
            suffixes=('_stock', '_idx')
        )
        if len(aligned) < period:
            return None
        return (
            (aligned['Close_stock'].iloc[-1] / aligned['Close_stock'].iloc[-period]) /
            (aligned['Close_idx'].iloc[-1]   / aligned['Close_idx'].iloc[-period])
        )
    except Exception:
        return None


def is_stage_2(df):
    """
    Minervini Trend Template — all 7 conditions must hold on the latest bar.

    1. Price > 150-day SMA and 200-day SMA
    2. 150-day SMA > 200-day SMA
    3. 200-day SMA trending upward for at least 1 month (20 trading days)
    4. 50-day SMA > 150-day SMA and 200-day SMA
    5. Price > 50-day SMA
    6. Price >= 30% above its 52-week low  (Minervini's actual threshold)
    7. Price >= 75% of its 52-week high    (within 25% of the high)
    """
    if len(df) < 252:
        return False

    close = df['Close']
    price = close.iloc[-1]

    # All MAs computed with vectorized rolling — single-pass over the series
    sma50        = close.rolling(50).mean().iloc[-1]
    sma150       = close.rolling(150).mean().iloc[-1]
    sma200_series = close.rolling(200).mean()
    sma200       = sma200_series.iloc[-1]
    # Compare today's 200-day SMA to 20 trading days ago to confirm upslope
    sma200_20d_ago = sma200_series.iloc[-21]

    high_52w = df['High'].tail(252).max()
    low_52w  = df['Low'].tail(252).min()

    return (
        price > sma150                 and   # 1a
        price > sma200                 and   # 1b
        sma150 > sma200                and   # 2
        sma200 > sma200_20d_ago        and   # 3  200-day SMA must be rising
        sma50  > sma150                and   # 4a
        sma50  > sma200                and   # 4b
        price  > sma50                 and   # 5
        price  >= low_52w  * 1.30      and   # 6  ≥30% above 52-week low
        price  >= high_52w * 0.75            # 7  within 25% of 52-week high
    )


def is_buyout_or_flatline(df):
    """
    Pulse-check filter — returns True if the ticker looks like an acquisition
    target or a halted/broken chart that has stopped trading normally.

    Acquisition targets peg to the offer price, which causes:
      (a) multiple days of zero price movement (Close diff == 0.00 exactly), and
      (b) intraday spread to collapse toward zero as the bid/ask converges.

    Condition 1 — Flat close: 3 or more days in the last 10 where the daily
    Close change is exactly 0.00.  Using `.eq(0)` on the diff Series is fully
    vectorized and avoids any Python-level loop.

    Condition 2 — Dead spread: mean intraday range  (High − Low) / Low  over
    the last 5 days falls below 0.25% (0.0025).  A live, tradeable stock will
    almost always exceed this threshold; an acquisition peg will not.

    Returns True  → reject this ticker (do not pass to VCP / RB / PB logic).
    Returns False → ticker looks alive, continue screening.
    """
    tail10 = df.tail(10)
    tail5  = df.tail(5)

    # Condition 1: count days where Close did not move at all
    flat_days = tail10['Close'].diff().eq(0).sum()   # vectorized; NaT-safe
    if flat_days >= 3:
        return True

    # Condition 2: average intraday spread over the last 5 sessions
    avg_spread = ((tail5['High'] - tail5['Low']) / tail5['Low']).mean()
    if avg_spread < 0.0025:
        return True

    return False


def find_swings(df, base_bars=60, w_default=5, w_recent=3, recent_cutoff=10):
    """
    Detect swing highs and lows within the most recent `base_bars` of price action.

    Dynamic-window strategy:
    - For bars older than `recent_cutoff` days from the right edge, use `w_default=5`
      (enough context to avoid noise in the body of the base).
    - For the final `recent_cutoff` bars, tighten to `w_recent=3` so we don't miss
      the last, tight VCP pivot that a wider window would smooth away.

    A loop over base_bars (~60 iterations) is intentional here — rolling functions
    cannot natively detect conditional local extrema with a variable half-window.
    At ~60 iterations per stock this is O(1) relative to the API latency.

    Returns two sorted lists of (integer_position, price) tuples.
    """
    data = df.tail(base_bars).reset_index(drop=True)
    n    = len(data)

    # Use numpy arrays for fast element-wise access inside the loop
    high_arr     = data['High'].to_numpy()
    low_arr      = data['Low'].to_numpy()
    recent_start = n - recent_cutoff

    highs_map, lows_map = {}, {}

    # Start at w_default so there are always ≥ w_default bars of left context
    for i in range(w_default, n):
        w     = w_recent if i >= recent_start else w_default
        left  = i - w
        right = min(n, i + w + 1)   # min() prevents out-of-bounds on the right edge

        if high_arr[i] == high_arr[left:right].max():
            highs_map[i] = high_arr[i]
        if low_arr[i] == low_arr[left:right].min():
            lows_map[i] = low_arr[i]

    return sorted(highs_map.items()), sorted(lows_map.items())


def get_contractions(highs, lows):
    """
    Merge swing highs and lows into a single chronological sequence and compute
    the percentage drawdown between each consecutive pair where the later swing
    is lower — these are the VCP contraction magnitudes.
    """
    swings = sorted(highs + lows, key=lambda x: x[0])
    return [
        (swings[i - 1][1] - swings[i][1]) / swings[i - 1][1] * 100
        for i in range(1, len(swings))
        if swings[i - 1][1] > swings[i][1]
    ]


def valid_vcp(contractions):
    """
    Relaxed VCP validation based on Minervini's principles.

    Accepts a pattern if ALL of the following hold:
    - Between 2 and 6 contractions (avoids both noise and over-complex bases)
    - Final contraction < 8%  (the base has coiled into a tight range)
    - Final contraction < first contraction (overall tightening arc is present,
      even when middle contractions are slightly irregular)

    The old `all(c[i] < c[i-1])` check required perfect monotonic tightening,
    which almost never occurs in real data and caused near-zero hit rates.
    """
    if not 2 <= len(contractions) <= 6:
        return False
    if contractions[-1] >= 8.0:             # Final pivot must be tight
        return False
    if contractions[-1] >= contractions[0]: # Must show net tightening from first to last
        return False
    return True


def volume_dry_last_contraction(df, contraction_bars=5):
    """
    Measure volume dry-up specifically during the *last* contraction of the base
    (the tightest part, typically the final 2–5 days before a breakout).

    Compares the average volume over `contraction_bars` days to the 50-day average.
    A ratio well below 1.0 confirms institutions are not distributing into the coil.
    """
    vol_50d_avg = df['Volume'].tail(50).mean()
    if vol_50d_avg == 0:
        return 1.0
    return df['Volume'].tail(contraction_bars).mean() / vol_50d_avg


def breakout_volume(df):
    """Latest bar's volume relative to 50-day average."""
    return df['Volume'].iloc[-1] / df['Volume'].tail(50).mean()


def pivot_distance(df):
    """How far below the 30-day high (the pivot) the current price sits."""
    pivot = df['High'].tail(30).max()
    return (pivot - df['Close'].iloc[-1]) / pivot


def get_poc(df):
    """Point of Control: price level with the highest traded volume over 120 days."""
    d  = df.tail(120).copy()
    tp = (d['High'] + d['Low'] + d['Close']) / 3
    min_tp = tp.min() if tp.min() > 0 else 1
    bins   = max(20, min(int((tp.max() - min_tp) / min_tp * 100), 100))
    d['b'] = pd.cut(tp, bins=bins)
    return d.groupby('b', observed=False)['Volume'].sum().idxmax().mid


def vcp_score(rs, contractions, vol_dry_ratio, breakout_ratio, pivot_dist, poc_dist):
    """Composite quality score for ranking VCP candidates."""
    s  = min(rs * 20, 30)                       # RS strength, capped at 30
    if len(contractions) >= 2:
        s += 20                                  # Pattern confirmation bonus
    s += max(0, (1 - vol_dry_ratio) * 15)        # Reward stronger volume drying
    s += min(breakout_ratio * 10, 20)            # Breakout volume urgency
    s += max(0, (1 - pivot_dist) * 10)           # Proximity to pivot
    if 0 <= poc_dist <= 20:
        s += 5                                   # Price sitting near value area
    return round(s, 2)


def calculate_rsi(series, period=14):
    """Wilder RSI — fully vectorized with pandas rolling."""
    delta = series.diff()
    gain  = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss  = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    return 100 - (100 / (1 + gain / loss))


def calculate_bollinger_bands(series, period=20, std_dev=2):
    """Returns the lower Bollinger Band — vectorized."""
    sma         = series.rolling(window=period).mean()
    rolling_std = series.rolling(window=period).std()
    return sma - (rolling_std * std_dev)


def find_momentum_pullbacks(df):
    """
    Escape Velocity & First Touch — identifies stocks pulling back to the
    confluence of their 20-day SMA and 20-day rolling VWAP after a strong
    momentum surge.  The six conditions form a strict sequential filter;
    the first failure short-circuits immediately so no unnecessary work runs.

    All indicator series are built with vectorized pandas rolling operations.
    No iterrows, no Python-level date loops.

    Returns None if any condition fails, or a metrics dict if all six pass.
    The caller is responsible for attaching the symbol key.

    Conditions
    ----------
    1. Indicators  : SMA_20 and Rolling_VWAP_20 must be computable (non-NaN).
    2. Slope       : SMA_20[today] > SMA_20[5d ago] > SMA_20[10d ago]  →  rising floor.
    3. Escape Vel. : highest High in the last 15 days was ≥ 12% above its
                     same-day SMA_20  →  stock genuinely launched away from the MA.
    4. Clear Air   : every daily Low in the 14 sessions *prior* to today was
                     strictly above SMA_20  →  today is the true first touch, not a
                     stock that has been chopping around the MA for weeks.
    5. Strike Zone : today's Close is within ±2.5% of BOTH SMA_20 and VWAP_20
                     simultaneously  →  price is resting at the confluence.
    6. Volume      : 3-day avg volume < 70% of 50-day avg volume  →  quiet pullback,
                     no distribution.
    """
    # Minimum bars: 50 (vol baseline) + 20 (SMA warmup) + 15 (lookback) = 65
    if len(df) < 65:
        return None

    close  = df['Close']
    volume = df['Volume']

    # ---- 1. Build indicators (fully vectorized) --------------------------------
    sma20  = close.rolling(20).mean()
    # Rolling VWAP_20: sum(Close * Volume) / sum(Volume) over 20 sessions
    vwap20 = (close * volume).rolling(20).sum() / volume.rolling(20).sum()

    sma20_today  = sma20.iloc[-1]
    vwap20_today = vwap20.iloc[-1]
    price        = close.iloc[-1]

    if pd.isna(sma20_today) or pd.isna(vwap20_today) or sma20_today == 0 or vwap20_today == 0:
        return None

    # ---- 2. Slope verification --------------------------------------------------
    # .iloc[-6] is 5 sessions ago; .iloc[-11] is 10 sessions ago
    sma20_5d_ago  = sma20.iloc[-6]
    sma20_10d_ago = sma20.iloc[-11]

    if not (sma20_today > sma20_5d_ago > sma20_10d_ago):
        return None

    # ---- 3. Escape Velocity -----------------------------------------------------
    # Look back 40 days (not 15) for the escape-velocity peak.
    #
    # The timing conflict: a stock typically takes 20-35 days to launch, top out,
    # and pull back to the SMA.  A 15-bar window forces the launch AND the landing
    # to coexist in the same narrow slice, which is nearly impossible.  Extending
    # to 40 bars means we correctly catch stocks that launched ~1-2 months ago and
    # are now at the SMA for the first time.
    #
    # Using .values on both arrays pins the offset to integer position so argmax()
    # indexes the SMA array identically, immune to DatetimeIndex gaps or tz shifts.
    high40  = df['High'].iloc[-40:].values
    sma40   = sma20.iloc[-40:].values

    peak_offset  = int(high40.argmax())
    peak_sma_val = sma40[peak_offset]

    if pd.isna(peak_sma_val) or peak_sma_val == 0:
        return None

    stretch_pct = (high40[peak_offset] - peak_sma_val) / peak_sma_val * 100
    if stretch_pct < 8.0:
        return None

    # ---- 4. Clear Air / Chop Filter --------------------------------------------
    # .iloc[-15:-1] gives exactly 14 rows: the 14 sessions prior to today.
    # Using .values on both sides avoids any index-alignment ambiguity.
    prior14_low   = df['Low'].iloc[-15:-1].values
    prior14_sma20 = sma20.iloc[-15:-1].values

    days_above_sma = int((prior14_low > prior14_sma20).sum())
    # Require 10/14 days above SMA: rejects sideways choppers while allowing
    # the normal 1-2 intraday dips that occur during a healthy pullback.
    if days_above_sma < 10:
        return None

    # ---- 5. Strike Zone --------------------------------------------------------
    dist_to_sma20  = abs(price - sma20_today)  / sma20_today
    dist_to_vwap20 = abs(price - vwap20_today) / vwap20_today

    # Widened from 2.5% to 3.5%: the final 3 near-misses were within this band
    if dist_to_sma20 > 0.035 or dist_to_vwap20 > 0.035:
        return None

    # ---- 6. Volume Dry-Up -------------------------------------------------------
    vol_50d_avg = volume.tail(50).mean()
    if vol_50d_avg == 0:
        return None

    vol_ratio = volume.tail(3).mean() / vol_50d_avg
    # Threshold relaxed from 0.70 to 0.85: diagnostics showed 2 valid setups
    # failing at 0.70 with ratios just above it.  0.85 still enforces a
    # meaningful 15% volume contraction vs the 50-day baseline.
    if vol_ratio >= 0.85:
        return None

    # All six conditions satisfied — build the signal record
    # Dist_to_VWAP_SMA: mean of the two individual distances, expressed as a
    # percentage.  Lower = tighter confluence between price, SMA, and VWAP.
    dist_to_vwap_sma = round((dist_to_sma20 + dist_to_vwap20) / 2 * 100, 3)

    return {
        "Stretch_%":       round(stretch_pct, 2),
        "Dist_VWAP_SMA_%": dist_to_vwap_sma,
        "Vol_Ratio":       round(vol_ratio, 2),
        "SMA20":           round(sma20_today, 2),
        "VWAP20":          round(vwap20_today, 2),
    }


# ================= MINERVINI LEADER PULLBACK =================

def find_minervini_pullback(df, params=None):
    """
    Minervini-style "Leader Pullback" scanner.

    Philosophy: escape velocity → first/early touch of the rising 20 SMA +
    rolling VWAP20 confluence → quiet, low-volume pullback.  This is NOT
    classic VCP detection — no multi-contraction logic.

    Parameters
    ----------
    df : pd.DataFrame
        OHLCV sorted ascending.  Required columns: Open, High, Low, Close, Volume.
    params : dict or None
        Override any default threshold.  Unknown keys are silently ignored.

    Returns
    -------
    None   — if any gate fails.
    dict   — rich metrics dict for every gate so callers can debug passes/fails.

    Gate summary
    ------------
    G1  All required indicators are computable (non-NaN, non-zero).
    G2  SMA20 slope rising: today > 5d ago > 10d ago  (floor lifting under price).
    G3  SMA50 > SMA200  (Stage-2 long-term alignment; skip with require_sma50_gt_sma200=False).
    G4  Intermediate trend alignment (two-sided):
        - Ceiling: Close <= max_pct_above_sma50 above SMA50 (not chasing a parabola).
        - Floor:   Close >= min_pct_below_sma50 of SMA50 (not failing the intermediate trend).
        - Ceiling: Close <= max_pct_above_sma200 above SMA200.
    G5  Escape velocity: highest High in prior `escape_lookback` bars is
        >= min_escape_pct above its same-day SMA20, with peak recency guard
        and optional peak-day close-quality check.
    G6  Touch count: Low "touches" of SMA20 in the prior `touch_lookback` bars
        must be <= max_touches — enforces first/early touch, rejects choppy
        stocks that have tested the MA repeatedly.  Evaluated EXCLUDING today
        (today's touch is the setup trigger we want to capture, not disqualify).
    G7  Strike zone (ATR-primary, OR mode by default): Close is within
        max_atr_dist_sma20 ATR units of SMA20 OR within max_atr_dist_vwap20 ATR
        units of VWAP20.  Set strike_zone_mode="AND" to require both simultaneously
        (more restrictive; can create an impossible constraint when the gap between
        SMA20 and VWAP20 exceeds 2× the ATR threshold).
        Percent caps are optional secondary guards.
    G8  Distribution filter: at most max_dist_days flagged days in the prior
        dist_lookback bars.  A "flagged day" is EITHER:
          - Distribution day: close down ≥ dist_min_drop% AND volume > prior day volume.
          - Stall day: abs(return) < dist_min_drop (barely moved) AND volume >
            stall_vol_mult × 50-day avg volume (institutions absorbing supply quietly).
        Returns are computed on the full series before slicing so the first bar
        of the window is never silently dropped.
    G9  Volume dry-up: 3-day avg volume < vol_dry_ratio × 50-day avg volume.
    """

    # ── Default parameters ────────────────────────────────────────────────────
    P: dict = {
        # Minimum data bars required (SMA200 needs 200 + some buffer)
        "min_bars": 252,

        # G2 – slope checkpoints (bars ago)
        "slope_5d_offset":  5,
        "slope_10d_offset": 10,

        # G3 – long-term trend alignment
        "require_sma50_gt_sma200": True,

        # G4 – two-sided SMA50 band (ceiling: avoid parabola; floor: avoid breakdown)
        "max_pct_above_sma50":  0.25,   # 25% above SMA50  — ceiling
        "min_pct_below_sma50": -0.04,   # -4% below SMA50  — floor (G4b)
        "max_pct_above_sma200": 0.60,   # 60% above SMA200 — ceiling

        # G5 – escape velocity
        "escape_lookback":     40,      # bars to search for the peak
        "min_escape_pct":       8.0,    # peak must be >= 8% above same-day SMA20
        "min_bars_since_peak":  5,      # peak can't be today (needs time to pull back)
        "max_bars_since_peak": 38,      # peak can't be too stale (> ~8 weeks old)
        # Peak-day close quality: (High-Close)/(High-Low) <= threshold
        # means the stock closed strong on the breakout day — institutional conviction
        "require_peak_quality": True,
        "peak_close_to_high":   0.40,   # 0 = closed at high, 1 = closed at low
        # Optional: price must be >= X× the close from 20 bars ago (momentum guard)
        "min_momentum_20d": None,       # e.g. 1.05 means +5% over 20 bars

        # G6 – touch count (first/early touch — replaces the 10/14 chop filter)
        # "Touch" = Low tagged SMA20 zone BUT Close held above it (found support)
        "touch_lookback":   30,         # bars to evaluate (excludes today)
        "touch_band":       0.005,      # Low <= SMA20*(1+0.005) = within 0.5% band
        "close_hold_band":  0.015,      # Close >= SMA20*(1-0.015) = didn't collapse
        "max_touches":      2,          # allow at most 2 prior touches

        # G7 – strike zone (ATR-based primary)
        "atr_period":           14,
        "max_atr_dist_sma20":   1.0,    # within 1.0 ATR of SMA20
        "max_atr_dist_vwap20":  1.0,    # within 1.0 ATR of VWAP20
        # "OR"  → pass if close to EITHER SMA20 or VWAP20 (default, avoids impossible
        #         geometric constraint when SMA20 and VWAP20 are far apart)
        # "AND" → must be close to BOTH (stricter, use only in tight-range markets)
        "strike_zone_mode":    "OR",
        # Optional percent caps (belt-and-suspenders for low-ATR regimes)
        "max_pct_dist_sma20":  None,    # e.g. 0.04 = 4%
        "max_pct_dist_vwap20": None,

        # G8 – distribution / stall day filter
        "dist_lookback":    15,         # bars to scan (today excluded)
        "dist_min_drop":    0.01,       # return <= -1.0% qualifies as a distribution day
        "max_dist_days":     3,         # combined dist + stall days allowed (relaxed from 2)
        "stall_vol_mult":   1.15,       # stall day: volume > 1.15× 50d avg on near-flat close
        # Optional: reject if today itself is a heavy-selling day (vol > X × 50d avg)
        "heavy_vol_mult": None,         # e.g. 1.5

        # G9 – volume dry-up
        "vol_dry_bars":      3,         # recent average window
        "vol_baseline_bars": 50,        # baseline average window
        "vol_dry_ratio":     1.00,      # recent must be < 100% of baseline (relaxed from 0.85)

        # Entry / trigger (Section F)
        "handle_bars":        5,        # bars defining the "handle" high for trigger
        "stop_lookback_bars": 5,        # bars to find the recent pullback low for stop
        "stop_buffer_pct":    0.005,    # 0.5% below recent low as a small volatility buffer
        "stop_atr_mult":      0.50,     # MA-derived floor = SMA20 - 0.5*ATR (tightened from 0.75)
        "max_risk_pct":       7.5,      # hard cap: is_trigger blocked when risk > 7.5%
    }

    if params:
        P.update(params)

    # ── Minimum data guard ────────────────────────────────────────────────────
    if len(df) < P["min_bars"]:
        return None

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    # ── G1: Build all indicators (fully vectorized, single pass each) ─────────
    sma20  = close.rolling(20).mean()
    sma50  = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()

    # Rolling VWAP20: price×volume denominator normalized by volume
    vwap20 = (close * volume).rolling(20).sum() / volume.rolling(20).sum()

    # ATR14: True Range is the widest of the three intraday/overnight ranges
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low,
         (high - prev_close).abs(),
         (low  - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr14 = tr.rolling(P["atr_period"]).mean()

    # Today's scalar values (last row = most recent bar, no lookahead)
    price        = close.iloc[-1]
    sma20_today  = sma20.iloc[-1]
    sma50_today  = sma50.iloc[-1]
    sma200_today = sma200.iloc[-1]
    vwap20_today = vwap20.iloc[-1]
    atr14_today  = atr14.iloc[-1]

    if any(pd.isna(v) or v == 0
           for v in [sma20_today, sma50_today, sma200_today, vwap20_today, atr14_today]):
        return None

    # ── G2: SMA20 slope (rising floor is the backbone of every Minervini setup) ─
    sma20_5d_ago  = sma20.iloc[-(P["slope_5d_offset"]  + 1)]
    sma20_10d_ago = sma20.iloc[-(P["slope_10d_offset"] + 1)]
    if not (sma20_today > sma20_5d_ago > sma20_10d_ago):
        return None

    # ── G3: SMA50 > SMA200 (Stage-2 long-term alignment) ─────────────────────
    # Minervini only buys leaders in confirmed uptrends; both MAs must confirm.
    if P["require_sma50_gt_sma200"] and sma50_today <= sma200_today:
        return None

    # ── G4: Two-sided SMA50 band + SMA200 ceiling ────────────────────────────
    # Ceiling: don't chase a stock already 25%+ above the intermediate MA.
    # Floor (G4b): if price has broken materially below SMA50, the intermediate
    # trend is failing — this is no longer a healthy pullback, it is a breakdown.
    # Floor is -4%; tighten to -0.02 in calm, low-ATR markets.
    stretch_from_sma50  = (price - sma50_today)  / sma50_today
    stretch_from_sma200 = (price - sma200_today) / sma200_today
    if stretch_from_sma50 > P["max_pct_above_sma50"]:
        return None
    if stretch_from_sma50 < P["min_pct_below_sma50"]:     # G4b — SMA50 floor
        return None
    if stretch_from_sma200 > P["max_pct_above_sma200"]:
        return None

    # ── G5: Escape velocity — stock genuinely launched from its MA ────────────
    # Slice lookback as numpy arrays so argmax() uses integer indexing,
    # immune to DatetimeIndex timezone or calendar-gap misalignment.
    ev_lb    = P["escape_lookback"]
    high_ev  = high.iloc[-ev_lb:].values
    low_ev   = low.iloc[-ev_lb:].values
    close_ev = close.iloc[-ev_lb:].values
    sma20_ev = sma20.iloc[-ev_lb:].values

    peak_offset  = int(high_ev.argmax())      # 0 = oldest bar in window
    peak_high    = high_ev[peak_offset]
    peak_low     = low_ev[peak_offset]
    peak_close   = close_ev[peak_offset]
    peak_sma_val = sma20_ev[peak_offset]

    if pd.isna(peak_sma_val) or peak_sma_val == 0:
        return None

    stretch_pct = (peak_high - peak_sma_val) / peak_sma_val * 100
    if stretch_pct < P["min_escape_pct"]:
        return None

    # bars_since_peak: 0 would mean the peak is today itself (nonsensical for a pullback)
    bars_since_peak = (ev_lb - 1) - peak_offset
    if not (P["min_bars_since_peak"] <= bars_since_peak <= P["max_bars_since_peak"]):
        return None

    # Optional peak-day close quality: strong close = institutions buying the breakout
    peak_range = peak_high - peak_low
    if P["require_peak_quality"] and peak_range > 0:
        close_to_high_ratio = (peak_high - peak_close) / peak_range
        if close_to_high_ratio > P["peak_close_to_high"]:
            return None
    else:
        close_to_high_ratio = np.nan

    # Optional 20-bar momentum guard: stock must still carry positive drift
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

    # ── G6: Touch count — first/early touch filter ───────────────────────────
    # Design choice: we evaluate the `touch_lookback` bars EXCLUDING today.
    # Today's touch of the SMA is the setup signal we want to flag; counting it
    # would penalise the very event we are hunting.
    # "Touch" requires BOTH: Low grazed the SMA zone (found the level) AND
    # Close held above it (no breakdown) — the hallmark of a healthy pullback.
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

    # ── G7: Strike zone (ATR-normalised, OR mode by default) ─────────────────
    # ATR adjusts for the stock's own volatility regime: a 1-ATR move for a
    # low-vol $20 stock vs a high-vol $200 stock reflects equivalent risk.
    dist_sma20_raw  = abs(price - sma20_today)
    dist_vwap20_raw = abs(price - vwap20_today)
    dist_sma20_atr  = dist_sma20_raw  / atr14_today
    dist_vwap20_atr = dist_vwap20_raw / atr14_today
    dist_sma20_pct  = dist_sma20_raw  / sma20_today
    dist_vwap20_pct = dist_vwap20_raw / vwap20_today

    # OR mode: price must be close to at least one anchor (SMA20 or VWAP20).
    # AND mode: price must be close to both simultaneously (stricter, can be
    # geometrically impossible when |SMA20 - VWAP20| > 2 × ATR threshold).
    sma20_in_zone  = dist_sma20_atr  <= P["max_atr_dist_sma20"]
    vwap20_in_zone = dist_vwap20_atr <= P["max_atr_dist_vwap20"]
    if P["strike_zone_mode"] == "AND":
        if not (sma20_in_zone and vwap20_in_zone):
            return None
    else:  # default: "OR"
        if not (sma20_in_zone or vwap20_in_zone):
            return None

    # Optional percent caps apply independently of the mode above
    if P["max_pct_dist_sma20"]  is not None and dist_sma20_pct  > P["max_pct_dist_sma20"]:
        return None
    if P["max_pct_dist_vwap20"] is not None and dist_vwap20_pct > P["max_pct_dist_vwap20"]:
        return None

    # ── G8: Distribution + stall day filter ──────────────────────────────────
    # KEY FIX: compute pct_change() and volume shift on the FULL series before
    # slicing.  Slicing first and then calling pct_change() drops the first bar
    # of the window (NaN return) — one distribution day would be silently missed
    # every single scan.  Computing on the full series and then slicing gives
    # every bar in the window a correctly-anchored return.
    dist_lb = P["dist_lookback"]

    # Full-series returns and prior-day volumes (no lookahead — shift looks back)
    full_returns  = close.pct_change()
    full_prev_vol = volume.shift(1)

    # Slice the evaluation window: last dist_lb bars, excluding today
    # .iloc[-(dist_lb + 1):-1] gives exactly dist_lb rows ending at yesterday
    returns_w  = full_returns.iloc[-(dist_lb + 1):-1].values
    volume_w   = volume.iloc[-(dist_lb + 1):-1].values
    prev_vol_w = full_prev_vol.iloc[-(dist_lb + 1):-1].values

    # Distribution day: meaningful down close on rising volume
    is_dist_day = (returns_w <= -P["dist_min_drop"]) & (volume_w > prev_vol_w)

    # Stall day: barely moved (flat/tiny up) but volume surged well above average
    # — institutions quietly absorbing supply without tipping their hand in price.
    vol_50d_avg  = volume.tail(50).mean()
    stall_vol_threshold = P["stall_vol_mult"] * vol_50d_avg
    is_stall_day = (
        (np.abs(returns_w) < P["dist_min_drop"])   # price went almost nowhere
        & (volume_w > stall_vol_threshold)          # but volume was elevated
    )

    dist_count  = int(is_dist_day.sum())
    stall_count = int(is_stall_day.sum())
    flagged_count = dist_count + stall_count        # combined pressure count

    if flagged_count > P["max_dist_days"]:
        return None

    # Optional: reject if today itself is a heavy-selling down day
    if P["heavy_vol_mult"] is not None:
        if price < close.iloc[-2] and volume.iloc[-1] > P["heavy_vol_mult"] * vol_50d_avg:
            return None

    # ── G9: Volume dry-up ────────────────────────────────────────────────────
    # Quiet pullback = no institutional supply flooding the ask.
    vol_recent_avg   = volume.iloc[-P["vol_dry_bars"]:].mean()
    vol_baseline_avg = volume.tail(P["vol_baseline_bars"]).mean()

    if vol_baseline_avg == 0:
        return None

    vol_ratio_3_50 = vol_recent_avg / vol_baseline_avg
    if vol_ratio_3_50 >= P["vol_dry_ratio"]:
        return None

    # ── F: Entry / trigger logic ──────────────────────────────────────────────
    # is_setup   = True  (all gates passed; stock is in the zone today)
    # is_trigger = True  (price is actively breaking above recent resistance AND
    #                     the resulting risk is within the hard cap)
    prior_day_high = high.iloc[-2]
    handle_high    = high.iloc[-P["handle_bars"]:].max()

    # Entry: buy on a print above the handle high (1-tick buffer avoids fake-outs)
    entry_price = round(handle_high * 1.001, 2)

    # ── Stop-loss: anchor to the TIGHTER of two references ───────────────────
    # Reference A — recent pullback low minus a small volatility buffer.
    #   Using max() (higher price = tighter stop) is intentional: we want the
    #   stop closest to the current price that is still technically valid.
    pullback_low    = low.iloc[-P["stop_lookback_bars"]:].min()
    buffered_pb_low = pullback_low * (1.0 - P["stop_buffer_pct"])

    # Reference B — SMA20 minus a fractional ATR (0.5× by default).
    #   This prevents the stop from sitting above the MA on very flat-range days.
    ma_stop = sma20_today - P["stop_atr_mult"] * atr14_today

    # Take the HIGHER of the two (tighter stop, lower risk-per-share).
    # Previously this used min(), which produced the widest possible stop — the
    # source of the 8-12% risk readings.
    stop_price = round(max(buffered_pb_low, ma_stop), 2)

    # Risk-per-share as % of entry
    if entry_price > stop_price > 0:
        risk_pct = round((entry_price - stop_price) / entry_price * 100, 2)
    else:
        risk_pct = np.nan

    # Trigger fires if today's high clears yesterday's high OR price is within
    # half an ATR of the handle high (imminent breakout), BUT only when the
    # resulting risk is within the hard cap — wide stops block the trigger signal
    # while preserving the setup for monitoring.
    price_trigger_condition = (
        high.iloc[-1] > prior_day_high
        or abs(price - handle_high) <= 0.5 * atr14_today
    )
    risk_within_cap = (not np.isnan(risk_pct)) and (risk_pct <= P["max_risk_pct"])
    is_trigger = bool(price_trigger_condition and risk_within_cap)

    # ── Return rich metrics dict ──────────────────────────────────────────────
    return {
        # Core moving averages (absolute levels)
        "sma20_today":              round(sma20_today,  2),
        "sma50_today":              round(sma50_today,  2),
        "sma200_today":             round(sma200_today, 2),
        "vwap20_today":             round(vwap20_today, 2),
        "atr14_today":              round(atr14_today,  4),
        "price":                    round(price,        2),

        # G7 – strike zone distances (ATR-units and percent)
        "dist_sma20_atr":           round(dist_sma20_atr,  3),
        "dist_vwap20_atr":          round(dist_vwap20_atr, 3),
        "dist_sma20_pct":           round(dist_sma20_pct  * 100, 3),
        "dist_vwap20_pct":          round(dist_vwap20_pct * 100, 3),

        # G5 – escape velocity
        "stretch_pct":              round(stretch_pct,          2),
        "peak_offset":              peak_offset,                     # index within escape_lookback window
        "bars_since_peak":          bars_since_peak,
        "close_to_high_ratio":      round(float(close_to_high_ratio), 3) if not np.isnan(close_to_high_ratio) else None,
        "momentum_20d":             round(float(momentum_20d), 3) if not np.isnan(momentum_20d) else None,

        # G6 – touch / chop filter
        "touch_count_window":       touch_count,
        "touch_lookback":           tc_lb,

        # G8 – distribution / stall filter (all three counts exposed for debugging)
        "distribution_count_window": dist_count,
        "stall_count_window":        stall_count,
        "flagged_count_window":      flagged_count,
        "dist_lookback":             dist_lb,

        # G9 – volume
        "vol_ratio_3_50":           round(vol_ratio_3_50, 3),

        # G4 – extension from long-term MAs (both ceiling and floor visible)
        "stretch_from_sma50_pct":   round(stretch_from_sma50  * 100, 2),
        "stretch_from_sma200_pct":  round(stretch_from_sma200 * 100, 2),

        # F – entry / trigger
        "is_setup":                 True,
        "is_trigger":               is_trigger,
        "risk_cap_blocked":         bool(price_trigger_condition and not risk_within_cap),
        "entry_price":              entry_price,
        "stop_price":               stop_price,
        "pullback_low":             round(pullback_low,     2),
        "ma_stop":                  round(ma_stop,          2),
        "risk_pct":                 risk_pct,
        "max_risk_pct":             P["max_risk_pct"],
        "handle_high":              round(handle_high,    2),
        "prior_day_high":           round(prior_day_high, 2),

        # Full params snapshot — critical for reproducibility / debugging
        "params":                   P,
    }


# ================= PULLBACK DIAGNOSTICS =================

def _pb_diagnose(df):
    """
    Non-short-circuiting version of find_momentum_pullbacks.

    Runs every gate regardless of failure and returns a flat dict of all
    computed metric values.  Used exclusively by run_pb_diagnostics() to
    identify which gate is the bottleneck and by how much.

    Keys
    ----
    passed        : bool  - True only if all 6 gates pass
    failed_at     : str   - name of the first failing gate, or 'PASS'
    stretch_pct   : float - % the peak High extended above its same-day SMA_20
    days_above_sma: int   - how many of the 14 prior sessions had Low > SMA_20 (max 14)
    dist_sma_pct  : float - today's Close distance from SMA_20 as %
    dist_vwap_pct : float - today's Close distance from VWAP_20 as %
    vol_ratio     : float - 3-day avg vol / 50-day avg vol
    sma_slope_ok  : bool  - True if SMA is rising across both 5d and 10d checkpoints
    """
    result = {
        "passed": False, "failed_at": "INSUFFICIENT_DATA",
        "stretch_pct": np.nan, "days_above_sma": np.nan,
        "dist_sma_pct": np.nan, "dist_vwap_pct": np.nan,
        "vol_ratio": np.nan,    "sma_slope_ok": False,
    }

    if len(df) < 65:
        return result

    close  = df['Close']
    volume = df['Volume']

    sma20  = close.rolling(20).mean()
    vwap20 = (close * volume).rolling(20).sum() / volume.rolling(20).sum()

    sma20_today  = sma20.iloc[-1]
    vwap20_today = vwap20.iloc[-1]
    price        = close.iloc[-1]

    # Gate 1 — indicators computable
    if pd.isna(sma20_today) or pd.isna(vwap20_today) or sma20_today == 0 or vwap20_today == 0:
        result["failed_at"] = "G1_INDICATOR_NAN"
        return result

    # Gate 2 — slope  (compute and record regardless)
    sma20_5d_ago  = sma20.iloc[-6]
    sma20_10d_ago = sma20.iloc[-11]
    slope_ok      = bool(sma20_today > sma20_5d_ago > sma20_10d_ago)
    result["sma_slope_ok"] = slope_ok

    # Gate 3 — escape velocity  (compute regardless; 40-bar window matches live filter)
    high40       = df['High'].iloc[-40:].values
    sma40        = sma20.iloc[-40:].values
    peak_offset  = int(high40.argmax())
    peak_sma_val = sma40[peak_offset]
    stretch_pct  = np.nan
    if not (pd.isna(peak_sma_val) or peak_sma_val == 0):
        stretch_pct = (high40[peak_offset] - peak_sma_val) / peak_sma_val * 100
    result["stretch_pct"] = round(float(stretch_pct), 2) if not np.isnan(stretch_pct) else np.nan

    # Gate 4 — clear air  (count how many of the 14 prior sessions had Low > SMA)
    prior14_low   = df['Low'].iloc[-15:-1].values
    prior14_sma20 = sma20.iloc[-15:-1].values
    days_above    = int((prior14_low > prior14_sma20).sum())
    result["days_above_sma"] = days_above

    # Gate 5 — strike zone distances
    dist_sma_pct  = abs(price - sma20_today)  / sma20_today * 100
    dist_vwap_pct = abs(price - vwap20_today) / vwap20_today * 100
    result["dist_sma_pct"]  = round(dist_sma_pct,  3)
    result["dist_vwap_pct"] = round(dist_vwap_pct, 3)

    # Gate 6 — volume dry-up
    vol_50d_avg = volume.tail(50).mean()
    vol_ratio   = volume.tail(3).mean() / vol_50d_avg if vol_50d_avg > 0 else np.nan
    result["vol_ratio"] = round(float(vol_ratio), 3) if vol_ratio is not None else np.nan

    # Determine the FIRST failing gate — thresholds must stay in sync with
    # find_momentum_pullbacks so the funnel report reflects the live filter.
    if not slope_ok:
        result["failed_at"] = "G2_SLOPE"
    elif np.isnan(stretch_pct) or stretch_pct < 8.0:
        result["failed_at"] = "G3_ESCAPE_VEL"
    elif days_above < 10:
        result["failed_at"] = "G4_CLEAR_AIR"
    elif dist_sma_pct > 3.5 or dist_vwap_pct > 3.5:
        result["failed_at"] = "G5_STRIKE_ZONE"
    elif pd.isna(vol_ratio) or vol_ratio >= 0.85:
        result["failed_at"] = "G6_VOLUME"
    else:
        result["failed_at"] = "PASS"
        result["passed"]    = True

    return result


def run_pb_diagnostics(sample_size=300):
    """
    Funnel diagnostic for find_momentum_pullbacks.

    Fetches `sample_size` stocks from the universe, runs every stock through
    all 6 gates (without short-circuiting), then prints:
      1. A gate-by-gate funnel — how many stocks survive each successive gate.
      2. For the top-3 bottleneck gates: the median actual value vs the threshold,
         so you can immediately judge whether a threshold needs relaxing.

    Run this standalone — it does NOT affect the main screener output.

    Usage:
        python alpaca_master_screener.py --diagnose
    """
    import random

    print("\n" + "="*60)
    print("  PULLBACK FILTER DIAGNOSTICS")
    print("="*60)

    # Fetch universe
    req     = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets  = trading_client.get_all_assets(req)
    symbols = [a.symbol for a in assets if a.tradable and a.fractionable and len(a.symbol) <= 4]

    sample  = random.sample(symbols, min(sample_size, len(symbols)))
    end     = datetime.now(timezone.utc) - timedelta(minutes=20)
    start   = end - timedelta(days=400)

    diag_rows = []
    chunk_size = 100

    print(f"Sampling {len(sample)} symbols ...\n")
    for i in tqdm(range(0, len(sample), chunk_size)):
        chunk = sample[i:i + chunk_size]
        try:
            request = StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start, end=end
            )
            bars = data_client.get_stock_bars(request).df
            if bars.empty:
                continue
            bars.rename(
                columns={'close': 'Close', 'high': 'High', 'low': 'Low', 'volume': 'Volume'},
                inplace=True
            )
            for symbol in chunk:
                if symbol not in bars.index:
                    continue
                df = bars.loc[symbol].copy()
                if len(df) < 252:
                    continue
                price             = df['Close'].iloc[-1]
                avg_vol_20        = df['Volume'].tail(20).mean()
                avg_dollar_vol_20 = (df['Close'] * df['Volume']).tail(20).mean()
                # Apply same pre-filters as main pipeline so diagnostics are apples-to-apples
                if price < 12.0 or avg_vol_20 < 400_000 or avg_dollar_vol_20 < 15_000_000:
                    continue
                if is_buyout_or_flatline(df):
                    continue
                row = _pb_diagnose(df)
                row["symbol"] = symbol
                diag_rows.append(row)
        except Exception as e:
            logging.error(f"Diagnostics chunk error: {e}")
        time.sleep(0.3)

    if not diag_rows:
        print("No data collected. Check API credentials or network.")
        return

    diag_df = pd.DataFrame(diag_rows)
    total   = len(diag_df)

    # ---- Funnel table -----------------------------------------------------------
    gate_order = ["G1_INDICATOR_NAN", "G2_SLOPE", "G3_ESCAPE_VEL",
                  "G4_CLEAR_AIR", "G5_STRIKE_ZONE", "G6_VOLUME"]
    gate_labels = {
        "G1_INDICATOR_NAN": "Gate 1 - Indicator computable",
        "G2_SLOPE":         "Gate 2 - SMA_20 rising (slope)",
        "G3_ESCAPE_VEL":    "Gate 3 - Escape Velocity >= 8%",
        "G4_CLEAR_AIR":     "Gate 4 - Clear Air (10/14 days low > SMA)",
        "G5_STRIKE_ZONE":   "Gate 5 - Strike Zone +/-3.5%",
        "G6_VOLUME":        "Gate 6 - Volume dry-up < 85%",
    }

    print(f"{'Gate':<42} {'Killed':>7}  {'Survivors':>9}  {'Pass%':>6}")
    print("-"*68)

    survivors = total
    for gate in gate_order:
        killed    = int((diag_df["failed_at"] == gate).sum())
        survivors = survivors - killed
        pct       = survivors / total * 100
        print(f"{gate_labels[gate]:<42} {killed:>7}  {survivors:>9}  {pct:>5.1f}%")

    passed = int(diag_df["passed"].sum())
    print("-"*68)
    print(f"{'FINAL PASS':42} {'':>7}  {passed:>9}  {passed/total*100:>5.1f}%")

    # ---- Per-gate median actual vs threshold ------------------------------------
    print("\n--- Median Metric Values by Top Bottleneck Gates ---\n")

    bottlenecks = (
        diag_df[diag_df["failed_at"].isin(gate_order)]
        .groupby("failed_at")
        .size()
        .sort_values(ascending=False)
        .head(3)
        .index.tolist()
    )

    details = {
        "G2_SLOPE":       ("sma_slope_ok",   "bool",  None,   "need True"),
        "G3_ESCAPE_VEL":  ("stretch_pct",    "float", 8.0,    "need >= 8.0%"),
        "G4_CLEAR_AIR":   ("days_above_sma", "int",   10,     "need >= 10 of 14"),
        "G5_STRIKE_ZONE": ("dist_sma_pct",   "float", 3.5,    "need <= 3.5% (SMA)"),
        "G6_VOLUME":      ("vol_ratio",      "float", 0.85,   "need < 0.85"),
    }

    for gate in bottlenecks:
        if gate not in details:
            continue
        col, dtype, threshold, note = details[gate]
        failing = diag_df[diag_df["failed_at"] == gate][col].dropna()
        if failing.empty:
            continue
        med = failing.median()
        print(f"  {gate_labels[gate]}")
        print(f"    Threshold : {threshold}  ({note})")
        print(f"    Median    : {med:.2f}   (across {len(failing)} failing stocks)")
        if dtype == "float" and threshold is not None:
            gap = med - threshold if gate not in ("G5_STRIKE_ZONE",) else med - threshold
            direction = "above" if med > threshold else "below"
            print(f"    Gap       : {abs(gap):.2f} {direction} threshold\n")
        else:
            print()

    print("="*60 + "\n")


# ================= MINERVINI PULLBACK TELEMETRY =================

# Gate labels — must match the order of gates in find_minervini_pullback.
# Each label maps to a specific short-circuit point in the live function.
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
]

_MPB_GATE_LABELS = {
    "G1_INDICATOR_NAN":   "G1  Indicators computable       ",
    "G2_SMA20_SLOPE":     "G2  SMA20 slope rising          ",
    "G3_MA_ALIGNMENT":    "G3  SMA50 > SMA200              ",
    "G4_EXTENSION_CEIL":  "G4  Extension ceiling (SMA50)   ",
    "G4B_SMA50_FLOOR":    "G4b Extension floor  (SMA50)    ",
    "G4C_SMA200_CEIL":    "G4c Extension ceiling (SMA200)  ",
    "G5_ESCAPE_VEL":      "G5  Escape velocity             ",
    "G5B_PEAK_AGE":       "G5b Peak age (bars since peak)  ",
    "G5C_PEAK_QUALITY":   "G5c Peak day quality            ",
    "G6_TOUCH_COUNT":     "G6  Touch count                 ",
    "G7_STRIKE_ZONE":     "G7  Strike zone (ATR, OR mode)  ",
    "G8_DISTRIBUTION":    "G8  Distribution / stall days   ",
    "G9_VOLUME_DRY":      "G9  Volume dry-up               ",
}


def _diagnose_minervini_pullback(df, params=None):
    """
    Non-short-circuiting diagnostic mirror of find_minervini_pullback.

    Computes EVERY gate metric regardless of whether earlier gates fail.
    Returns a flat dict containing all raw values and the label of the first
    gate that would cause the live function to return None.

    This is the telemetry engine.  It never modifies trading logic — only
    observes.  Called by run_minervini_diagnostics(); never used in production.

    Returns
    -------
    dict with keys:
        passed      : bool — True only if all gates pass
        failed_at   : str  — gate label (e.g. "G7_STRIKE_ZONE") or "PASS"
        + one key per gate metric (always populated, NaN when not computable)
    """
    # ── Resolve params (same defaults as the live function, must stay in sync) ─
    P: dict = {
        "min_bars": 252,
        "slope_5d_offset": 5, "slope_10d_offset": 10,
        "require_sma50_gt_sma200": True,
        "max_pct_above_sma50": 0.25, "min_pct_below_sma50": -0.04,
        "max_pct_above_sma200": 0.60,
        "escape_lookback": 40, "min_escape_pct": 8.0,
        "min_bars_since_peak": 5, "max_bars_since_peak": 38,
        "require_peak_quality": True, "peak_close_to_high": 0.40,
        "touch_lookback": 30, "touch_band": 0.005,
        "close_hold_band": 0.015, "max_touches": 2,
        "atr_period": 14,
        "max_atr_dist_sma20": 1.0, "max_atr_dist_vwap20": 1.0,
        "strike_zone_mode": "OR",
        "max_pct_dist_sma20": None, "max_pct_dist_vwap20": None,
        "dist_lookback": 15, "dist_min_drop": 0.01,
        "max_dist_days": 3, "stall_vol_mult": 1.15,
        "vol_dry_bars": 3, "vol_baseline_bars": 50, "vol_dry_ratio": 1.00,
    }
    if params:
        P.update(params)

    # ── Initialise result with sentinel NaNs ──────────────────────────────────
    out: dict = {
        "passed": False, "failed_at": "INSUFFICIENT_DATA",
        # G1
        "sma20_today": np.nan, "sma50_today": np.nan, "sma200_today": np.nan,
        "vwap20_today": np.nan, "atr14_today": np.nan, "price": np.nan,
        # G2
        "sma20_5d_ago": np.nan, "sma20_10d_ago": np.nan, "sma20_slope_ok": False,
        # G3
        "sma50_gt_sma200": False,
        # G4
        "stretch_from_sma50_pct": np.nan, "stretch_from_sma200_pct": np.nan,
        # G5
        "stretch_pct": np.nan, "bars_since_peak": np.nan,
        "close_to_high_ratio": np.nan,
        # G6
        "touch_count": np.nan,
        # G7
        "dist_sma20_atr": np.nan, "dist_vwap20_atr": np.nan,
        "dist_sma20_pct": np.nan, "dist_vwap20_pct": np.nan,
        "sma20_vwap20_gap_atr": np.nan,
        # G8
        "dist_count": np.nan, "stall_count": np.nan, "flagged_count": np.nan,
        # G9
        "vol_ratio_3_50": np.nan,
    }

    if len(df) < P["min_bars"]:
        return out

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    # ── Compute all series up front ───────────────────────────────────────────
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
        "price": round(price, 2),
        "sma20_today":  round(float(sma20_today),  2) if not pd.isna(sma20_today)  else np.nan,
        "sma50_today":  round(float(sma50_today),  2) if not pd.isna(sma50_today)  else np.nan,
        "sma200_today": round(float(sma200_today), 2) if not pd.isna(sma200_today) else np.nan,
        "vwap20_today": round(float(vwap20_today), 2) if not pd.isna(vwap20_today) else np.nan,
        "atr14_today":  round(float(atr14_today),  4) if not pd.isna(atr14_today)  else np.nan,
    })

    # ── G1 check (must pass for later metrics to be meaningful) ──────────────
    g1_ok = not any(pd.isna(v) or v == 0
                    for v in [sma20_today, sma50_today, sma200_today, vwap20_today, atr14_today])

    if not g1_ok:
        out["failed_at"] = "G1_INDICATOR_NAN"
        return out

    # ── G2: SMA20 slope ───────────────────────────────────────────────────────
    sma20_5d_ago  = sma20.iloc[-(P["slope_5d_offset"]  + 1)]
    sma20_10d_ago = sma20.iloc[-(P["slope_10d_offset"] + 1)]
    slope_ok = bool(sma20_today > sma20_5d_ago > sma20_10d_ago)
    out.update({
        "sma20_5d_ago":   round(float(sma20_5d_ago),  2),
        "sma20_10d_ago":  round(float(sma20_10d_ago), 2),
        "sma20_slope_ok": slope_ok,
    })

    # ── G3: MA alignment ─────────────────────────────────────────────────────
    sma50_gt_sma200 = bool(sma50_today > sma200_today)
    out["sma50_gt_sma200"] = sma50_gt_sma200

    # ── G4: Extension bands ───────────────────────────────────────────────────
    stretch_from_sma50  = (price - sma50_today)  / sma50_today
    stretch_from_sma200 = (price - sma200_today) / sma200_today
    out["stretch_from_sma50_pct"]  = round(stretch_from_sma50  * 100, 2)
    out["stretch_from_sma200_pct"] = round(stretch_from_sma200 * 100, 2)

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
    bars_since_peak = (ev_lb - 1) - peak_offset

    if not (pd.isna(peak_sma_val) or peak_sma_val == 0):
        stretch_pct = (peak_high - peak_sma_val) / peak_sma_val * 100
        peak_range  = peak_high - peak_low
        c2h = (peak_high - peak_close) / peak_range if peak_range > 0 else np.nan
        out.update({
            "stretch_pct":        round(stretch_pct, 2),
            "bars_since_peak":    bars_since_peak,
            "close_to_high_ratio": round(c2h, 3) if not np.isnan(c2h) else np.nan,
        })
    else:
        stretch_pct = np.nan
        c2h         = np.nan

    # ── G6: Touch count ───────────────────────────────────────────────────────
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

    # ── G7: Strike zone ───────────────────────────────────────────────────────
    dist_sma20_raw   = abs(price - sma20_today)
    dist_vwap20_raw  = abs(price - vwap20_today)
    dist_sma20_atr   = dist_sma20_raw  / atr14_today
    dist_vwap20_atr  = dist_vwap20_raw / atr14_today
    sma20_vwap20_gap = abs(sma20_today - vwap20_today) / atr14_today
    out.update({
        "dist_sma20_atr":      round(dist_sma20_atr,  3),
        "dist_vwap20_atr":     round(dist_vwap20_atr, 3),
        "dist_sma20_pct":      round(dist_sma20_raw / sma20_today  * 100, 3),
        "dist_vwap20_pct":     round(dist_vwap20_raw / vwap20_today * 100, 3),
        "sma20_vwap20_gap_atr": round(sma20_vwap20_gap, 3),
    })

    # ── G8: Distribution + stall ─────────────────────────────────────────────
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
    dist_count_val  = int(is_dist_day.sum())
    stall_count_val = int(is_stall_day.sum())
    flagged_val     = dist_count_val + stall_count_val
    out.update({
        "dist_count":    dist_count_val,
        "stall_count":   stall_count_val,
        "flagged_count": flagged_val,
    })

    # ── G9: Volume dry-up ─────────────────────────────────────────────────────
    vol_recent_avg   = volume.iloc[-P["vol_dry_bars"]:].mean()
    vol_baseline_avg = volume.tail(P["vol_baseline_bars"]).mean()
    vol_ratio_3_50   = vol_recent_avg / vol_baseline_avg if vol_baseline_avg > 0 else np.nan
    out["vol_ratio_3_50"] = round(float(vol_ratio_3_50), 3) if not np.isnan(vol_ratio_3_50) else np.nan

    # ── Determine first failing gate (mirrors live function gate order) ────────
    sma20_in_zone  = dist_sma20_atr  <= P["max_atr_dist_sma20"]
    vwap20_in_zone = dist_vwap20_atr <= P["max_atr_dist_vwap20"]
    g7_ok = (sma20_in_zone or vwap20_in_zone) if P["strike_zone_mode"] == "OR" \
            else (sma20_in_zone and vwap20_in_zone)

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
    elif flagged_val > P["max_dist_days"]:
        out["failed_at"] = "G8_DISTRIBUTION"
    elif np.isnan(vol_ratio_3_50) or vol_ratio_3_50 >= P["vol_dry_ratio"]:
        out["failed_at"] = "G9_VOLUME_DRY"
    else:
        out["failed_at"] = "PASS"
        out["passed"]    = True

    return out


def run_minervini_diagnostics(sample_size: int = 500, params: dict | None = None):
    """
    Full telemetry run for find_minervini_pullback.

    Scans a random sample of the US equity universe, applies the same
    pre-filters as the master screener, and runs every ticker through
    _diagnose_minervini_pullback() without short-circuiting.

    Output (printed to stdout)
    --------------------------
    1. Gate funnel — kill count, survivor count, and pass-rate per gate.
    2. SMA20/VWAP20 divergence check — reveals the G7 geometric choke.
    3. Per-gate metric analysis — median actual value vs threshold for each
       bottleneck gate, with gap size and a concrete "try relaxing to X" hint.

    Usage
    -----
        python alpaca_master_screener.py --diagnose-mpb
        python alpaca_master_screener.py --diagnose-mpb --sample 1000
    """
    import random

    W = 72   # print width constant

    print("\n" + "=" * W)
    print("  MINERVINI LEADER PULLBACK — GATE TELEMETRY")
    if params:
        print(f"  Custom params: {params}")
    else:
        print("  Params: defaults")
    print("=" * W)

    # ── Fetch universe ────────────────────────────────────────────────────────
    req    = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets = trading_client.get_all_assets(req)
    all_symbols = [
        a.symbol for a in assets
        if a.tradable and a.fractionable and len(a.symbol) <= 4
    ]
    sample = random.sample(all_symbols, min(sample_size, len(all_symbols)))
    print(f"\n  Universe: {len(all_symbols)} symbols  |  Sample: {len(sample)}\n")

    end   = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=400)

    rows       = []
    chunk_size = 100

    for i in tqdm(range(0, len(sample), chunk_size), desc="Fetching"):
        chunk = sample[i:i + chunk_size]
        try:
            req_bars = StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                start=start, end=end,
            )
            bars = data_client.get_stock_bars(req_bars).df
            if bars.empty:
                continue
            bars.rename(
                columns={"close": "Close", "high": "High",
                         "low": "Low", "volume": "Volume"},
                inplace=True,
            )
            for sym in chunk:
                if sym not in bars.index:
                    continue
                df = bars.loc[sym].copy()
                if len(df) < 252:
                    continue
                price             = df["Close"].iloc[-1]
                avg_vol_20        = df["Volume"].tail(20).mean()
                avg_dollar_vol_20 = (df["Close"] * df["Volume"]).tail(20).mean()
                if price < 12.0 or avg_vol_20 < 400_000 or avg_dollar_vol_20 < 15_000_000:
                    continue
                if is_buyout_or_flatline(df):
                    continue
                row            = _diagnose_minervini_pullback(df, params)
                row["symbol"]  = sym
                row["price_at_scan"] = price
                rows.append(row)
        except Exception as exc:
            logging.error(f"MPB diag chunk {chunk[0]}: {exc}")
        time.sleep(0.3)

    if not rows:
        print("  No data collected — check API credentials / network.")
        return

    diag = pd.DataFrame(rows)
    total = len(diag)
    print(f"\n  Qualified tickers after pre-filters: {total}\n")

    # ── 1. Gate funnel ────────────────────────────────────────────────────────
    print(f"{'─' * W}")
    print(f"  {'Gate':<38}  {'Threshold':<20}  {'Killed':>6}  {'Alive':>6}  {'Pass%':>5}")
    print(f"{'─' * W}")

    # Per-gate threshold label for the funnel display
    threshold_labels = {
        "G1_INDICATOR_NAN":  "all non-NaN / non-zero",
        "G2_SMA20_SLOPE":    "today > 5d > 10d",
        "G3_MA_ALIGNMENT":   "SMA50 > SMA200",
        "G4_EXTENSION_CEIL": f"≤{(params or {}).get('max_pct_above_sma50', 0.25)*100:.0f}% above SMA50",
        "G4B_SMA50_FLOOR":   f"≥{abs((params or {}).get('min_pct_below_sma50', -0.04))*100:.0f}% below SMA50",
        "G4C_SMA200_CEIL":   f"≤{(params or {}).get('max_pct_above_sma200', 0.60)*100:.0f}% above SMA200",
        "G5_ESCAPE_VEL":     f"peak ≥{(params or {}).get('min_escape_pct', 8.0):.0f}% above SMA20",
        "G5B_PEAK_AGE":      f"{(params or {}).get('min_bars_since_peak',5)}–{(params or {}).get('max_bars_since_peak',38)} bars ago",
        "G5C_PEAK_QUALITY":  f"(H-C)/(H-L) ≤{(params or {}).get('peak_close_to_high', 0.40):.2f}",
        "G6_TOUCH_COUNT":    f"≤{(params or {}).get('max_touches', 2)} touches in {(params or {}).get('touch_lookback', 30)}b",
        "G7_STRIKE_ZONE":    f"≤{(params or {}).get('max_atr_dist_sma20', 1.0):.1f} ATR (OR mode)",
        "G8_DISTRIBUTION":   f"≤{(params or {}).get('max_dist_days', 3)} flagged in {(params or {}).get('dist_lookback', 15)}b",
        "G9_VOLUME_DRY":     f"3d < {(params or {}).get('vol_dry_ratio', 1.00):.2f}× 50d avg",
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

    # ── 2. SMA20 / VWAP20 divergence analysis (G7 geometry check) ────────────
    print("  G7 GEOMETRY ANALYSIS  (reveals impossible confluence zones)")
    print(f"  {'Metric':<45}  {'Value':>8}")
    print(f"  {'─'*55}")
    gap_col = "sma20_vwap20_gap_atr"
    if gap_col in diag.columns:
        gap_vals = diag[gap_col].dropna()
        p50  = gap_vals.quantile(0.50)
        p75  = gap_vals.quantile(0.75)
        p90  = gap_vals.quantile(0.90)
        print(f"  {'Median  |SMA20 - VWAP20| in ATR units':<45}  {p50:>7.3f}")
        print(f"  {'75th percentile':<45}  {p75:>7.3f}")
        print(f"  {'90th percentile':<45}  {p90:>7.3f}")
        threshold = (params or {}).get("max_atr_dist_sma20", 1.0)
        impossible = int((gap_vals > 2 * threshold).sum())
        print(f"  {'Tickers where gap > 2×ATR threshold (impossible zone)':<45}  {impossible:>7d}")
    print()

    # ── 3. Per-gate bottleneck analysis ───────────────────────────────────────
    print("  PER-GATE MEDIAN  (failing tickers: actual value vs threshold)")
    print(f"{'─' * W}")

    # For each gate: which metric to inspect, threshold value, direction, hint multiplier
    gate_metric_map = {
        "G2_SMA20_SLOPE":    ("sma20_slope_ok",        None,   "bool",  None),
        "G3_MA_ALIGNMENT":   ("sma50_gt_sma200",        None,   "bool",  None),
        "G4_EXTENSION_CEIL": ("stretch_from_sma50_pct", 25.0,   "le",    0.30),
        "G4B_SMA50_FLOOR":   ("stretch_from_sma50_pct", -2.0,   "ge",   -0.04),
        "G4C_SMA200_CEIL":   ("stretch_from_sma200_pct",60.0,   "le",    0.70),
        "G5_ESCAPE_VEL":     ("stretch_pct",             8.0,   "ge",    6.0),
        "G5B_PEAK_AGE":      ("bars_since_peak",         None,  "range", None),
        "G5C_PEAK_QUALITY":  ("close_to_high_ratio",     0.40,  "le",    0.50),
        "G6_TOUCH_COUNT":    ("touch_count",             2,     "le",    3),
        "G7_STRIKE_ZONE":    ("dist_sma20_atr",          1.0,   "le",    1.5),
        "G8_DISTRIBUTION":   ("flagged_count",           3,     "le",    4),
        "G9_VOLUME_DRY":     ("vol_ratio_3_50",          1.00,  "le",    1.10),
    }

    # Sort gates by number of tickers killed (descending)
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
            gap = abs(med - threshold)
            side = "above" if med > threshold else "below"
            print(f"    Gap       : {gap:.3f} {side} threshold")
            if hint is not None:
                print(f"    Try       : relax threshold to {hint}")
        elif mode == "range":
            p_min = (params or {}).get("min_bars_since_peak", 5)
            p_max = (params or {}).get("max_bars_since_peak", 38)
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


# ================= MASTER ENGINE =================

def run_master_scanner(target=15):

    # --- 1. Universe ---
    print("Fetching active US equities from Alpaca...")
    req     = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets  = trading_client.get_all_assets(req)
    symbols = [a.symbol for a in assets if a.tradable and a.fractionable and len(a.symbol) <= 4]
    print(f"Found {len(symbols)} tradable symbols.")

    # --- 2. Timeframe (UTC required by Alpaca) ---
    end   = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=400)   # ~400 calendar days → ~280 trading days

    # --- 3. Benchmark data ---
    print("Fetching Benchmark Data (SPY, QQQ)...")
    idx_req  = StockBarsRequest(
        symbol_or_symbols=["SPY", "QQQ"], timeframe=TimeFrame.Day, start=start, end=end
    )
    idx_bars = data_client.get_stock_bars(idx_req).df
    idx_bars.rename(
        columns={'close': 'Close', 'high': 'High', 'low': 'Low', 'volume': 'Volume'},
        inplace=True
    )
    spy_df = idx_bars.loc["SPY"].copy() if "SPY" in idx_bars.index else None
    qqq_df = idx_bars.loc["QQQ"].copy() if "QQQ" in idx_bars.index else None

    vcp_results, rb_results, pb_results, mpb_results = [], [], [], []

    # --- 4. Process in chunks to respect Alpaca rate limits ---
    chunk_size = 100
    print("Running Master Technical Screener...")

    for i in tqdm(range(0, len(symbols), chunk_size)):
        chunk = symbols[i:i + chunk_size]
        try:
            request = StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start, end=end
            )
            bars = data_client.get_stock_bars(request).df
            if bars.empty:
                continue

            bars.rename(
                columns={'close': 'Close', 'high': 'High', 'low': 'Low', 'volume': 'Volume'},
                inplace=True
            )

            for symbol in chunk:
                if symbol not in bars.index:
                    continue

                df = bars.loc[symbol].copy()

                # Need ≥252 bars so all MAs (200-day) and 52-week ranges are fully warm
                if len(df) < 252:
                    continue

                price = df['Close'].iloc[-1]

                # ===== GLOBAL LIQUIDITY FILTER =====
                avg_vol_20        = df['Volume'].tail(20).mean()
                avg_dollar_vol_20 = (df['Close'] * df['Volume']).tail(20).mean()

                if price < 12.0 or avg_vol_20 < 400_000 or avg_dollar_vol_20 < 15_000_000:
                    continue

                # ===== PULSE CHECK: reject acquisition pegs / flatlined charts =====
                # Runs before is_stage_2 and all MA work — zero compute wasted on
                # dead tickers that would otherwise pass the trend template because
                # their pegged price happens to sit above all moving averages.
                if is_buyout_or_flatline(df):
                    continue

                # Pre-compute MAs shared across multiple algorithms (vectorized)
                close      = df['Close']
                sma20_val  = close.rolling(20).mean().iloc[-1]
                sma200_val = close.rolling(200).mean().iloc[-1]

                # ===== ALGORITHM 1: VCP (MINERVINI TREND TEMPLATE + CONTRACTION) =====
                if is_stage_2(df):
                    benchmark = qqq_df if len(symbol) == 4 else spy_df
                    rs        = calculate_rs(df, benchmark)

                    if rs is not None and rs >= 1.2:
                        h, l          = find_swings(df)
                        c             = get_contractions(h, l)
                        vol_dry_ratio = volume_dry_last_contraction(df, contraction_bars=5)
                        pivot_dist    = pivot_distance(df)

                        # Thresholds calibrated to yield real candidates:
                        #   vol_dry_ratio ≤ 0.75  — volume meaningfully contracting
                        #   pivot_dist    ≤ 0.08  — within 8% of the breakout pivot
                        if valid_vcp(c) and vol_dry_ratio <= 0.75 and pivot_dist <= 0.08:
                            poc      = get_poc(df)
                            poc_dist = (price - poc) / poc * 100
                            if 0 <= poc_dist <= 30:
                                b_ratio = breakout_volume(df)
                                vcp_results.append({
                                    "Symbol":       symbol,
                                    "Score":        vcp_score(rs, c, vol_dry_ratio, b_ratio, pivot_dist, poc_dist),
                                    "RS":           round(rs, 2),
                                    "Contractions": len(c),
                                    "Final_C_%":    round(c[-1], 2),
                                    "Vol_Dry":      round(vol_dry_ratio, 2),
                                })

                # ===== ALGORITHM 2: RUBBER BAND (OVERSOLD DIP IN UPTREND) =====
                if price > sma200_val:
                    df['RSI'] = calculate_rsi(df['Close'], 14)
                    current_rsi = df['RSI'].iloc[-1]

                    if current_rsi <= 35:
                        current_lower_band = calculate_bollinger_bands(df['Close']).iloc[-1]
                        if price <= current_lower_band:
                            band_distance = ((price - current_lower_band) / current_lower_band) * 100
                            rb_results.append({
                                "Symbol":     symbol,
                                "RSI":        round(current_rsi, 2),
                                "Band_Dist_%": round(band_distance, 2)
                            })

                # ===== ALGORITHM 3: ESCAPE VELOCITY & FIRST TOUCH =====
                pb_signal = find_momentum_pullbacks(df)
                if pb_signal is not None:
                    pb_signal["Symbol"] = symbol
                    pb_results.append(pb_signal)

                # ===== ALGORITHM 4: MINERVINI LEADER PULLBACK =====
                # Upgraded version of Algorithm 3: ATR-normalised strike zone,
                # first-touch counting, distribution filter, Stage-2 alignment,
                # over-extension guard, and actionable entry/stop levels.
                mpb_signal = find_minervini_pullback(df)
                if mpb_signal is not None:
                    mpb_signal["Symbol"] = symbol
                    mpb_results.append(mpb_signal)

        except Exception as e:
            logging.error(f"Error processing chunk starting with {chunk[0]}: {e}")
            continue

        time.sleep(0.3)   # Respect Alpaca free-tier rate limit (~200 req/min)

    # --- 5. Format & Rank Output ---
    vcp_df = (
        pd.DataFrame(vcp_results).sort_values("Score", ascending=False).head(target)
        if vcp_results else pd.DataFrame()
    )
    rb_df = (
        pd.DataFrame(rb_results).sort_values("Band_Dist_%", ascending=True).head(target)
        if rb_results else pd.DataFrame()
    )
    pb_df = (
        pd.DataFrame(pb_results).sort_values("Stretch_%", ascending=False).head(target)
        if pb_results else pd.DataFrame()
    )

    # Rank Minervini pullbacks: tightest strike zone (lowest combined ATR distance)
    # AND most imminent triggers first.
    if mpb_results:
        mpb_df = pd.DataFrame(mpb_results)
        # Drop the embedded params dict before sorting (it's a dict column)
        mpb_display = mpb_df.drop(columns=["params"], errors="ignore")
        mpb_display["atr_dist_sum"] = (
            mpb_display["dist_sma20_atr"] + mpb_display["dist_vwap20_atr"]
        )
        mpb_display = (
            mpb_display
            .sort_values(["is_trigger", "atr_dist_sum"], ascending=[False, True])
            .head(target)
        )
    else:
        mpb_display = pd.DataFrame()

    return vcp_df, rb_df, pb_df, mpb_display


if __name__ == "__main__":
    import sys

    def _get_sample_arg(default: int = 300) -> int:
        """Parse --sample N from argv, falling back to `default`."""
        if "--sample" in sys.argv:
            idx = sys.argv.index("--sample")
            return int(sys.argv[idx + 1])
        return default

    if "--diagnose" in sys.argv:
        # Funnel diagnostics for the original find_momentum_pullbacks (Algorithm 3).
        # Usage:  python alpaca_master_screener.py --diagnose [--sample N]
        run_pb_diagnostics(sample_size=_get_sample_arg(300))
        sys.exit(0)

    if "--diagnose-mpb" in sys.argv:
        # Full gate telemetry for find_minervini_pullback (Algorithm 4).
        # Usage:  python alpaca_master_screener.py --diagnose-mpb [--sample N]
        #
        # To test relaxed params without editing the function:
        #   run_minervini_diagnostics(params={"max_atr_dist_sma20": 1.5,
        #                                     "strike_zone_mode": "OR",
        #                                     "max_dist_days": 4})
        run_minervini_diagnostics(sample_size=_get_sample_arg(500))
        sys.exit(0)

    vcp, rb, pb, mpb = run_master_scanner()

    print("\n" + "="*60)
    print("ALGORITHM 1: VCP (MOMENTUM BREAKOUTS)")
    print("="*60)
    print(vcp if not vcp.empty else "No VCP setups found today.")

    print("\n" + "="*60)
    print("ALGORITHM 2: RUBBER BAND (OVERSOLD DIPS)")
    print("="*60)
    print(rb if not rb.empty else "No Rubber Band setups found today.")

    print("\n" + "="*60)
    print("ALGORITHM 3: ESCAPE VELOCITY & FIRST TOUCH (20-SMA/VWAP)")
    print("="*60)
    print(pb if not pb.empty else "No Pullback setups found today.")

    print("\n" + "="*60)
    print("ALGORITHM 4: MINERVINI LEADER PULLBACK (UPGRADED)")
    print("="*60)
    if not mpb.empty:
        # Surface the most actionable columns for terminal readability
        display_cols = [
            "Symbol", "is_trigger", "risk_cap_blocked",
            "price", "entry_price", "stop_price", "risk_pct",
            "stretch_pct", "bars_since_peak",
            "dist_sma20_atr", "dist_vwap20_atr",
            "touch_count_window",
            "flagged_count_window", "distribution_count_window", "stall_count_window",
            "vol_ratio_3_50", "stretch_from_sma50_pct",
        ]
        print(mpb[[c for c in display_cols if c in mpb.columns]].to_string(index=False))
    else:
        print("No Minervini Leader Pullback setups found today.")
    print()
