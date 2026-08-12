"""
vcp_scanner/compute.py

compute_features(symbol, ...) → dict

Orchestrates all four feature groups for a single symbol:
  price     — ATR contraction, vol percentile, POC, breakout proximity, RS
  liquidity — median daily trading value, turnover proxy
  flows     — foreign / institutional net buying normalised by ADV
  leverage  — credit rate level, 5d/20d delta, cross-sectional z-score

The returned dict is "wide" (one row per symbol) so that callers can build a
DataFrame and pass it to rank_candidates().
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .config import PRICE_SUB_WEIGHTS, SCORE_WEIGHTS
from .features.flows     import compute_flow_scores, fetch_symbol_flows
from .features.leverage  import compute_leverage_features, fetch_stock_info
from .features.liquidity import liquidity_features
from .features.price import (
    atr_contraction_sequence,
    breakout_proximity,
    poc_features,
    relative_strength,
    volatility_percentile,
)

log = logging.getLogger(__name__)


async def compute_features(
    bot,
    symbol: str,
    df: pd.DataFrame,
    benchmark_df: pd.DataFrame,
    *,
    market_type: str = "KOSDAQ",
    prefetched_flows: dict[str, dict[str, float]] | None = None,
    flow_days: int = 20,
    rest_sleep: float = 0.12,
) -> dict[str, Any]:
    """
    Compute the full feature vector for one symbol.

    Parameters
    ----------
    bot              : authenticated Kiwoom Bot instance
    symbol           : KRX 6-digit code string, e.g. '005930'
    df               : OHLCV DataFrame (FinanceDataReader format); must have
                       ≥ MIN_HISTORY_ROWS rows with columns
                       [Open, High, Low, Close, Volume]
    benchmark_df     : OHLCV DataFrame for the relevant index (KOSPI or KOSDAQ)
    market_type      : 'KOSPI' or 'KOSDAQ' — drives threshold selection
    prefetched_flows : output of prefetch_universe_flows(); when provided,
                       universe-level flow data is read from the cache instead
                       of making per-symbol ka10059 REST calls (much faster).
    flow_days        : number of calendar days to look back for ka10059 calls
    rest_sleep       : seconds between each Kiwoom REST call

    Returns
    -------
    dict with ~50+ keys covering all feature groups plus sub-scores and a
    preliminary composite_score.  Pass a list of these dicts to rank_candidates().
    Returns an empty dict if the DataFrame is too short.
    """
    if df is None or len(df) < 10:
        return {}

    result: dict[str, Any] = {
        "symbol":      symbol,
        "market_type": market_type,
        "last_close":  float(df["Close"].iloc[-1]),
    }

    # ── 1. Price structure ────────────────────────────────────────────────────
    result.update(atr_contraction_sequence(df))
    result.update(volatility_percentile(df))
    result.update(poc_features(df))
    result.update(breakout_proximity(df))
    result.update(relative_strength(df, benchmark_df))

    price_score = (
        PRICE_SUB_WEIGHTS["atr_contraction"] * (result.get("atr_contraction_score") or 0.0)
        + PRICE_SUB_WEIGHTS["vol_percentile"] * (result.get("vol_percentile_score")  or 0.0)
        + PRICE_SUB_WEIGHTS["breakout_prox"]  * (result.get("breakout_prox_score")   or 0.0)
        + PRICE_SUB_WEIGHTS["poc_distance"]   * (result.get("poc_distance_score")    or 0.0)
        + PRICE_SUB_WEIGHTS["rs_score"]       * (result.get("rs_score")              or 0.0)
    )
    result["price_score"] = float(price_score)

    # ── 2. Stock info (credit + shares outstanding) — one REST call ───────────
    info = await fetch_stock_info(bot, symbol, sleep=rest_sleep)
    credit_level = info.get("credit_level")
    shares_out   = info.get("shares_out")
    result["shares_out"] = shares_out

    # ── 3. Liquidity ──────────────────────────────────────────────────────────
    result.update(liquidity_features(df, shares_outstanding=shares_out))

    # ── 4. Flows ─────────────────────────────────────────────────────────────
    adv_shares = result.get("adv_shares_20d")

    if prefetched_flows is not None and symbol in prefetched_flows:
        raw_flows = dict(prefetched_flows[symbol])   # copy so we don't mutate cache
    else:
        # Fallback: per-symbol ka10059 (slower but gives 5d ignition detail)
        raw_flows = await fetch_symbol_flows(bot, symbol, days=flow_days, sleep=rest_sleep)

    result.update(raw_flows)
    flow_scores = compute_flow_scores(raw_flows, adv_shares, float_shares=shares_out)
    result.update(flow_scores)

    # Normalised 5-day foreign flow ratio: foreign_net_5d / adv20_shares.
    # Positive = foreign was a net buyer recently; used as a hard gate in scanner.
    foreign_net_5d = float(raw_flows.get("foreign_net_5d") or 0.0)
    result["foreign_flow_ratio_5d"] = (
        foreign_net_5d / adv_shares if (adv_shares and adv_shares > 0) else None
    )

    # ── 5. Leverage ───────────────────────────────────────────────────────────
    # credit_delta and credit_zscore are filled in by rank_candidates()
    # after it has the cross-sectional distribution.
    lev = compute_leverage_features(
        credit_level=credit_level,
        market_type=market_type,
    )
    result.update(lev)

    # ── 6. Preliminary composite score ────────────────────────────────────────
    # rank_candidates() will recompute this after cross-sectional normalisation;
    # this value is useful for early short-circuits during scanning.
    result["composite_score"] = float(
        SCORE_WEIGHTS["price"]     * result.get("price_score",     0.0)
        + SCORE_WEIGHTS["liquidity"] * result.get("liquidity_score", 0.0)
        + SCORE_WEIGHTS["flows"]     * result.get("flows_score",     0.0)
        + SCORE_WEIGHTS["leverage"]  * result.get("leverage_score",  0.0)
    )

    return result
