"""
ptp_engine.py — Power Trend Pullback (PTP) detection engine.   v2.0

Algorithm 2 in the master scanner. The Minervini-style VCP engine
(Algorithm 1) misses secular momentum stocks that never pause long enough
to form a multi-week base. The Power Trend Pullback module captures those
runaway leaders by:

    1. Mathematically proving the stock is in a "power trend" — moving-
       average hierarchy ``EMA10 > EMA21 > SMA50``, multi-month uptrend,
       ≥ 15% appreciation over the last 40 bars, and ADX(14) > 25.

    2. Waiting for a precise 1-3 day mean-reversion into the EMA10 / EMA21
       confluence on dry volume — a tradable, engineered dip rather than a
       random pullback. 1-day "hook" reversals are explicitly allowed.

    3. Triggering an entry on intraday strength back through the high of
       the lowest day in the pullback sequence, with an institutional
       relative-volume gate (RVOL ≥ 1.3) at the moment of crossing.

    4. Sizing risk with a structure-first stop (1·ATR below the pullback
       low or just beneath the EMA21 — whichever is tighter) and rejecting
       any setup whose risk_pct exceeds the hard cap.

v2.0 architectural changes (data-driven retune)
-----------------------------------------------
* ``Close > EMA10`` removed from the regime hierarchy. Diagnostics on
  6,600 symbols showed this single conjunct cannibalised every valid
  candidate — by construction the close must be allowed to slide below
  EMA10 to reach EMA21 during a tradable pullback.
* Momentum velocity gate relaxed from +20% / 40d to +15% / 40d.
* ADX trend-strength gate relaxed from > 30 to > 25.
* 5-bar mean-reversion stretch gate relaxed from ≥ 10% to ≥ 7%.
* Resting band widened from ±1.5% to ±3.0% (standard equity volatility).
* Pullback streak length widened from {2, 3} to {1, 2, 3} so 1-day
  hook reversals into the EMA confluence are captured.
* Volume dry-up (Volume < SMA50(Volume)) is intentionally NOT relaxed.

Performance contract
--------------------
The implementation is strictly vectorized: every gate evaluates with a
constant number of rolling / cumulative numpy or pandas ops. There is no
``DataFrame.iterrows`` or row-level python loop anywhere in the module.
Total cost is O(n) in the number of bars and O(1) in additional symbols
beyond what the caller already iterates over.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import PTP_PARAMS


class PowerTrendPullbackEngine:
    """
    Vectorized PTP setup detector.

    The class is stateless beyond its parameter dict, so a single instance
    can be reused across the entire scanning universe.

    Parameters
    ----------
    params : dict, optional
        Override any subset of :data:`screener.config.PTP_PARAMS`. Useful
        for diagnostics sweeps without touching the production config.

    Notes
    -----
    Public surface:

    * :meth:`generate_signals`     — full-series signal table for backtests
    * :meth:`latest_signal`        — single dict for the most recent bar
    * :meth:`_calculate_moving_averages`,
      :meth:`_detect_power_regime`,
      :meth:`_identify_pullback`,
      :meth:`_calculate_execution_levels`
        — internal vectorized building blocks, exposed for testing.
    """

    # ── Construction ────────────────────────────────────────────────────────

    def __init__(self, params: dict | None = None) -> None:
        self.params: dict = {**PTP_PARAMS, **(params or {})}

    # ── Indicator stack ─────────────────────────────────────────────────────

    def _calculate_moving_averages(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Build every indicator the four gates depend on, in one O(n) sweep.

        Adds columns
        ------------
        ema10, ema21, sma50, sma200          — moving-average hierarchy
        sma50_slope_up_20d                   — bool: SMA50 rose for ≥ 20 bars
        ret_40d                              — 40-bar pct change (the stretch)
        atr14                                — Wilder ATR(14) for stops
        adx14, plus_di14, minus_di14         — Wilder ADX(14) for trend strength
        vol_sma50                            — 50-bar SMA(Volume) baseline

        Algorithmic complexity
        ----------------------
        Five rolling/EWM passes, each O(n). Total O(n).
        """
        P = self.params
        out = df.copy()

        close  = out["Close"]
        high   = out["High"]
        low    = out["Low"]
        volume = out["Volume"]

        # Moving-average hierarchy
        out["ema10"]  = close.ewm(span=10, adjust=False).mean()
        out["ema21"]  = close.ewm(span=21, adjust=False).mean()
        out["sma50"]  = close.rolling(50).mean()
        out["sma200"] = close.rolling(200).mean()

        # SMA50 trending up for ≥ N consecutive bars
        sma50_up = out["sma50"].diff() > 0
        slope_n  = P["sma50_slope_window"]
        out["sma50_slope_up_20d"] = sma50_up.rolling(slope_n).sum() == slope_n

        # Multi-week appreciation (the velocity stretch)
        out["ret_40d"] = close.pct_change(P["momentum_window"])

        # Wilder ATR & ADX share the True Range series
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        out["atr14"] = tr.ewm(alpha=1.0 / P["atr_period"], adjust=False).mean()

        # Directional movement components — vectorized via numpy.where
        up_move   = high.diff()
        down_move = -low.diff()
        plus_dm = pd.Series(
            np.where((up_move   > down_move) & (up_move   > 0), up_move,   0.0),
            index=out.index,
        )
        minus_dm = pd.Series(
            np.where((down_move > up_move)   & (down_move > 0), down_move, 0.0),
            index=out.index,
        )

        atr_for_adx = (
            tr.ewm(alpha=1.0 / P["adx_period"], adjust=False).mean().replace(0.0, np.nan)
        )
        plus_di  = 100.0 * plus_dm.ewm(alpha=1.0 / P["adx_period"], adjust=False).mean() / atr_for_adx
        minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / P["adx_period"], adjust=False).mean() / atr_for_adx
        di_sum   = (plus_di + minus_di).replace(0.0, np.nan)
        dx       = 100.0 * (plus_di - minus_di).abs() / di_sum

        out["plus_di14"]  = plus_di
        out["minus_di14"] = minus_di
        out["adx14"]      = dx.ewm(alpha=1.0 / P["adx_period"], adjust=False).mean()

        # Volume baseline
        out["vol_sma50"] = volume.rolling(P["vol_baseline_bars"]).mean()

        return out

    # ── Gate 1: regime ──────────────────────────────────────────────────────

    def _detect_power_regime(self, df: pd.DataFrame) -> pd.Series:
        """
        Boolean Series — True for bars where the stock is in a power trend.

        Conditions (all must hold)
        --------------------------
        * EMA10 > EMA21 > SMA50                                (MA hierarchy)
        * SMA50 > SMA200  OR  SMA50 trending up for ≥ N bars   (long-term)
        * 40-bar return ≥ ``min_momentum_pct``                 (the stretch)
        * ADX(14) > ``min_adx``                                (trend strength)

        v2.0 architectural change
        -------------------------
        ``Close > EMA10`` is intentionally NOT part of the hierarchy. By
        construction the price must be allowed to float below the 10 EMA
        (and possibly all the way to the 21 EMA) for a valid Power Trend
        Pullback to materialise. Requiring ``Close > EMA10`` mathematically
        cannibalised the very setups this engine is designed to catch and
        produced a 0% pass rate on a 6.6k-symbol backtest. The MA structure
        is now confirmed by the moving averages alone; the close is governed
        by the resting-on-EMA gate inside :meth:`_identify_pullback`.

        Algorithmic complexity
        ----------------------
        Pure element-wise logical & comparison operators across columns
        already produced by :meth:`_calculate_moving_averages`. O(n).
        """
        P = self.params

        ma_hierarchy = (
            (df["ema10"] > df["ema21"]) &
            (df["ema21"] > df["sma50"])
        )
        long_term_trend = (df["sma50"] > df["sma200"]) | df["sma50_slope_up_20d"]
        velocity        = df["ret_40d"] >= P["min_momentum_pct"]
        trend_strength  = df["adx14"]   >  P["min_adx"]

        regime = ma_hierarchy & long_term_trend & velocity & trend_strength
        return regime.fillna(False)

    # ── Gate 2: pullback mechanics ──────────────────────────────────────────

    def _identify_pullback(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Detect the engineered 2-3 day mean reversion into the EMA confluence.

        Adds columns
        ------------
        stretch_5d         — max (High / EMA21 − 1) over the last 5 bars
        close_band_ema10   — |Close − EMA10| / EMA10
        close_band_ema21   — |Close − EMA21| / EMA21
        rest_on_ema        — bool: close within ``close_band_pct`` of either MA
        lh_ll              — bool: lower high AND lower low vs prior bar
        down_streak        — run-length of consecutive ``lh_ll`` ending at bar
        in_pullback        — down_streak ∈ [min_pullback_days, max_pullback_days]
        vol_drying         — Volume < vol_sma50
        pullback_ok        — every G2 sub-condition holds

        Algorithmic complexity
        ----------------------
        The vectorized streak-count uses ``groupby((~lh_ll).cumsum()).cumsum()``
        which gives the run-length within each contiguous block of True in one
        pass. Combined with two rolling ops the method is O(n).
        """
        P = self.params
        out = df.copy()

        # Mean reversion: did the daily high recently get stretched ≥ X% above EMA21?
        stretch_now = out["High"] / out["ema21"] - 1.0
        out["stretch_5d"] = stretch_now.rolling(P["stretch_lookback"]).max()

        # Resting band — close must be within ±1.5% of EMA10 OR EMA21
        out["close_band_ema10"] = (out["Close"] - out["ema10"]).abs() / out["ema10"]
        out["close_band_ema21"] = (out["Close"] - out["ema21"]).abs() / out["ema21"]
        out["rest_on_ema"] = (
            (out["close_band_ema10"] <= P["close_band_pct"]) |
            (out["close_band_ema21"] <= P["close_band_pct"])
        )

        # Lower-high AND lower-low pattern — a strict consecutive-down day
        lh = out["High"] < out["High"].shift(1)
        ll = out["Low"]  < out["Low"].shift(1)
        lh_ll = (lh & ll).fillna(False)
        out["lh_ll"] = lh_ll

        # Vectorized run length: every False in lh_ll starts a new block id;
        # cumsum within the block gives the consecutive-True count ending at i.
        block_id = (~lh_ll).cumsum()
        out["down_streak"] = lh_ll.groupby(block_id).cumsum().astype(int)

        out["in_pullback"] = out["down_streak"].between(
            P["min_pullback_days"], P["max_pullback_days"]
        )

        # Volume dry-up — strictly below the 50-bar SMA(Volume) baseline
        out["vol_drying"] = out["Volume"] < out["vol_sma50"]

        stretch_ok = out["stretch_5d"] >= P["min_pullback_stretch_pct"]

        out["pullback_ok"] = (
            stretch_ok &
            out["rest_on_ema"] &
            out["in_pullback"] &
            out["vol_drying"]
        ).fillna(False)

        return out

    # ── Gates 3 / 4: trigger price, stop, risk ──────────────────────────────

    def _calculate_execution_levels(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute trigger_price, stop_price, and risk_pct in O(n).

        Trigger
        -------
        Within a strict LH/LL streak ending today, today's Low is the lowest
        low of the sequence by construction (each bar makes a new low). The
        trigger is therefore today's High. The execution layer fires the buy
        when intraday price crosses ``trigger_price`` AND the time-of-day
        RVOL clears ``intraday_rvol_min`` (Gate 3 — RVOL is enforced live;
        only the level is materialised here).

        Stop
        ----
        Two structural candidates are evaluated:

            * ATR-anchored : pullback_low  − 1 · ATR14
            * MA-anchored  : EMA21 · (1 − ``stop_ema_buffer_pct``)

        "Whichever is tighter" → the candidate closer to the entry → the
        higher of the two values. We use ``np.maximum``.

        Risk cap
        --------
        ``risk_pct = (trigger − stop) / trigger × 100``. Setups whose
        risk_pct exceeds ``max_risk_pct`` are flagged in
        ``risk_cap_blocked`` and excluded from the primed set.

        Algorithmic complexity
        ----------------------
        All operations are element-wise. O(n).
        """
        P = self.params
        out = df.copy()

        # Mask the per-bar levels to bars where the streak is valid; everything
        # else stays NaN and propagates harmlessly through downstream math.
        trigger_price = out["High"].where(out["in_pullback"])
        pullback_low  = out["Low"].where(out["in_pullback"])

        atr_stop   = pullback_low - P["stop_atr_mult"] * out["atr14"]
        ema21_stop = out["ema21"] * (1.0 - P["stop_ema_buffer_pct"])

        # "Whichever is tighter" = closer to entry = higher floor.
        stop_price = np.maximum(atr_stop, ema21_stop)
        stop_price = pd.Series(stop_price, index=out.index)

        # Sanity: stop must be strictly below the trigger; otherwise risk is invalid.
        stop_price = stop_price.where(stop_price < trigger_price)

        risk_pct = (trigger_price - stop_price) / trigger_price * 100.0

        out["trigger_price"]    = trigger_price.round(4)
        out["pullback_low"]     = pullback_low.round(4)
        out["stop_price"]       = stop_price.round(4)
        out["risk_pct"]         = risk_pct.round(3)
        out["risk_cap_blocked"] = (risk_pct > P["max_risk_pct"]).fillna(False)
        out["risk_within_cap"]  = risk_pct.notna() & (risk_pct <= P["max_risk_pct"])

        return out

    # ── Public API ──────────────────────────────────────────────────────────

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Run every gate end-to-end and return only the rows where the setup
        is primed for the next session.

        Parameters
        ----------
        df : pd.DataFrame
            Standard OHLCV frame with columns Open / High / Low / Close /
            Volume, indexed in ascending chronological order. The frame is
            consumed read-only; a new DataFrame is returned.

        Returns
        -------
        pd.DataFrame
            Filtered, chronologically indexed frame containing only primed
            rows. In addition to the original OHLCV columns it carries:

                trigger_price, stop_price, pullback_low, risk_pct,
                stretch_5d, down_streak, ret_40d, adx14,
                ema10, ema21, sma50, sma200,
                regime_ok, pullback_ok, risk_within_cap, is_primed

            Returns an empty frame of the same shape when no row qualifies.

        Algorithmic complexity
        ----------------------
        Composition of the four building blocks above, each O(n). Final
        boolean mask & ``loc`` selection are also O(n). Total O(n).
        """
        if len(df) < self.params["min_bars"]:
            return df.iloc[0:0]

        with_ind  = self._calculate_moving_averages(df)
        regime_ok = self._detect_power_regime(with_ind)
        with_pb   = self._identify_pullback(with_ind)
        with_exec = self._calculate_execution_levels(with_pb)

        primed = (
            regime_ok
            & with_exec["pullback_ok"]
            & with_exec["risk_within_cap"]
        ).fillna(False)

        signals = with_exec.loc[primed].copy()
        signals["regime_ok"] = True
        signals["is_primed"] = True
        return signals

    def latest_signal(self, df: pd.DataFrame) -> dict | None:
        """
        Per-symbol integration helper used by the master scanner.

        Returns a flat metric dict for the most recent bar IF that bar is
        a primed PTP setup, otherwise ``None``. The dict mirrors the
        public columns of :meth:`generate_signals` and is intended for
        immediate display / serialisation.
        """
        if len(df) < self.params["min_bars"]:
            return None

        with_ind  = self._calculate_moving_averages(df)
        regime_ok = bool(self._detect_power_regime(with_ind).iloc[-1])
        if not regime_ok:
            return None

        with_pb   = self._identify_pullback(with_ind)
        with_exec = self._calculate_execution_levels(with_pb)
        last      = with_exec.iloc[-1]

        if not bool(last.get("pullback_ok", False)):
            return None

        risk_pct_val = last["risk_pct"]
        if pd.isna(risk_pct_val) or risk_pct_val > self.params["max_risk_pct"]:
            return None

        return {
            "price":          round(float(with_ind["Close"].iloc[-1]), 2),
            "trigger_price":  round(float(last["trigger_price"]), 2),
            "stop_price":     round(float(last["stop_price"]), 2),
            "pullback_low":   round(float(last["pullback_low"]), 2),
            "risk_pct":       round(float(risk_pct_val), 2),
            "stretch_5d_pct": round(float(last["stretch_5d"]) * 100, 2),
            "ret_40d_pct":    round(float(with_ind["ret_40d"].iloc[-1]) * 100, 2),
            "adx14":          round(float(with_ind["adx14"].iloc[-1]), 2),
            "down_streak":    int(last["down_streak"]),
            "ema10":          round(float(with_ind["ema10"].iloc[-1]), 2),
            "ema21":          round(float(with_ind["ema21"].iloc[-1]), 2),
            "sma50":          round(float(with_ind["sma50"].iloc[-1]), 2),
            "sma200":         round(float(with_ind["sma200"].iloc[-1]), 2),
            "vol_dry":        bool(last["vol_drying"]),
            "intraday_rvol_min": float(self.params["intraday_rvol_min"]),
        }


def find_power_trend_pullback(df: pd.DataFrame, params: dict | None = None) -> dict | None:
    """
    Module-level convenience wrapper used by :mod:`screener.engine`.

    Equivalent to ``PowerTrendPullbackEngine(params).latest_signal(df)``.
    """
    return PowerTrendPullbackEngine(params).latest_signal(df)
