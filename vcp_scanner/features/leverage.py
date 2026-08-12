"""
vcp_scanner/features/leverage.py

Credit / margin (leverage) features via ka10001 (주식기본정보요청).

What we extract
---------------
credit_level    — current credit balance rate (신용비율, %)
shares_out      — total issued shares (for turnover proxy in liquidity module)
credit_delta_5d — 5-day change in credit rate (cross-run; see note below)
credit_delta_20d— 20-day change
credit_zscore   — cross-sectional z-score computed by rank_candidates() later

Note on historical credit data
-------------------------------
ka10001 only returns the *current* credit rate; there is no daily-history TR in
the public Kiwoom REST spec.  The delta and z-score features are therefore
populated in two stages:
  1. fetch_stock_info()  — returns the current snapshot.
  2. rank_candidates()   — fills in cross-sectional z-score across all candidates.
If you run the scanner daily and persist the output CSV, you can compute the 5d
and 20d deltas externally by joining today's run with prior runs.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from ..config import (
    KOSDAQ_MAX_CREDIT_RATE,
    KOSPI_MAX_CREDIT_RATE,
    LEVERAGE_SUB_WEIGHTS,
)
from ..kiwoom_client import GOOD_RETURN_CODES, safe_post

log = logging.getLogger(__name__)

_CREDIT_FIELD_CANDIDATES: tuple[str, ...] = (
    "crd_rt", "crdt_rt", "credit_rt", "crd_ratio", "crdrt",
)
_SHARES_FIELD_CANDIDATES: tuple[str, ...] = (
    "list_shrs", "lstg_stk_cnt", "issued_shares", "tot_issued_stk",
    "stk_issu_cnt", "issu_stk_cnt",
)


async def fetch_stock_info(
    bot,
    symbol: str,
    *,
    sleep: float = 0.12,
) -> dict[str, Any]:
    """
    Call ka10001 and extract credit rate and shares outstanding.

    Returns
    -------
    {
        "credit_level":      <float | None>,   # % e.g. 2.34
        "shares_out":        <int   | None>,   # total issued shares
    }
    """
    body = await safe_post(
        bot,
        "/api/dostk/stkinfo",
        "ka10001",
        {"stk_cd": symbol},
        sleep=sleep,
    )

    rc = body.get("return_code")
    if rc not in GOOD_RETURN_CODES and rc is not None:
        return {"credit_level": None, "shares_out": None}

    credit_level: float | None = None
    for field in _CREDIT_FIELD_CANDIDATES:
        raw = body.get(field)
        if raw not in (None, ""):
            try:
                credit_level = float(str(raw).replace(",", "").replace("%", "").strip())
                break
            except ValueError:
                continue

    shares_out: int | None = None
    for field in _SHARES_FIELD_CANDIDATES:
        raw = body.get(field)
        if raw not in (None, ""):
            try:
                shares_out = int(float(str(raw).replace(",", "").strip()))
                break
            except ValueError:
                continue

    return {"credit_level": credit_level, "shares_out": shares_out}


def compute_leverage_features(
    credit_level: float | None,
    credit_delta_5d:  float | None = None,
    credit_delta_20d: float | None = None,
    credit_zscore:    float | None = None,
    market_type: str = "KOSDAQ",
) -> dict[str, float | None]:
    """
    Convert raw credit numbers into normalised sub-scores and a composite
    leverage_score.

    Lower credit = less crowded / less forced-selling risk = higher score.

    Parameters
    ----------
    credit_level     — current rate %
    credit_delta_5d  — change from 5 sessions ago (negative = improving)
    credit_delta_20d — change from 20 sessions ago
    credit_zscore    — cross-sectional z-score (populated by rank_candidates)
    market_type      — 'KOSPI' or 'KOSDAQ' (affects max_rate threshold)

    Returns
    -------
    credit_level, credit_delta_5d, credit_delta_20d, credit_zscore,
    leverage_score  — [0, 1]
    """
    feats: dict[str, float | None] = {
        "credit_level":     credit_level,
        "credit_delta_5d":  credit_delta_5d,
        "credit_delta_20d": credit_delta_20d,
        "credit_zscore":    credit_zscore,
        "leverage_score":   0.5,
    }

    if credit_level is None:
        return feats

    max_rate = KOSDAQ_MAX_CREDIT_RATE if market_type == "KOSDAQ" else KOSPI_MAX_CREDIT_RATE

    # Level: 0 % → score 1.0; max_rate → score 0.0
    level_score = float(max(0.0, min(1.0, 1.0 - credit_level / max_rate)))

    # Delta: decreasing credit (negative delta) is bullish
    delta_score = 0.5
    if credit_delta_5d is not None:
        # delta of −max_rate → score 1.0; delta of +max_rate → score 0.0
        delta_score = float(max(0.0, min(1.0, 0.5 - credit_delta_5d / (max_rate * 2))))

    delta_20d_score = 0.5
    if credit_delta_20d is not None:
        delta_20d_score = float(max(0.0, min(1.0, 0.5 - credit_delta_20d / (max_rate * 2))))

    # Z-score: below cross-sectional mean is preferred
    z_score_score = 0.5
    if credit_zscore is not None:
        z_score_score = float(max(0.0, min(1.0, 0.5 - credit_zscore * 0.1)))

    W = LEVERAGE_SUB_WEIGHTS
    composite = (
        W["level"]     * level_score
        + W["delta_5d"]  * delta_score
        + W["delta_20d"] * delta_20d_score
        + W["zscore"]    * z_score_score
    )
    feats["leverage_score"] = float(np.clip(composite, 0.0, 1.0))

    return feats
