"""
vcp_scanner/features/liquidity.py

Liquidity features:
  - 20-day median daily trading value (KRW) — robust to outlier volume spikes
  - 20-day average daily trading value      — for ADV proxy in flow normalisation
  - Turnover proxy (avg daily volume / float shares) if float is available

Scoring uses a log-scale so that the step from 50억→100억 is treated the same
as 500억→1000억, which matches how Korean traders think about liquidity tiers.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def liquidity_features(
    df: pd.DataFrame,
    shares_outstanding: int | None = None,
) -> dict[str, float | None]:
    """
    Parameters
    ----------
    df                  : OHLCV DataFrame
    shares_outstanding  : total issued shares (from ka10001 / listing data);
                          used only for turnover proxy — safe to omit.

    Returns
    -------
    med_value_20d     — median daily KRW traded (last 20 sessions)
    avg_value_20d     — mean   daily KRW traded (last 20 sessions)
    adv_shares_20d    — mean   daily share volume (last 20 sessions)
    turnover_20d      — adv_shares / shares_outstanding  (None if unavailable)
    liquidity_score   — [0, 1]; log-scaled on med_value_20d
    """
    tv = df["Close"] * df["Volume"]

    med_20 = float(tv.tail(20).median())
    avg_20 = float(tv.tail(20).mean())
    adv_sh = float(df["Volume"].tail(20).mean())

    feats: dict[str, float | None] = {
        "med_value_20d":  med_20,
        "avg_value_20d":  avg_20,
        "adv_shares_20d": adv_sh,
        "turnover_20d":   (adv_sh / shares_outstanding) if (shares_outstanding and shares_outstanding > 0) else None,
    }

    # Log-scale scoring:
    #   10억 (1e9)  → log10 = 9.0  → score ≈ 0
    #   50억 (5e9)  → log10 ≈ 9.7  → score ≈ 0.35
    #  100억 (1e10) → log10 = 10.0 → score ≈ 0.50
    # 1000억 (1e11) → log10 = 11.0 → score ≈ 1.00
    if med_20 > 0:
        score = float(np.clip((np.log10(med_20) - 9.0) / 2.0, 0.0, 1.0))
    else:
        score = 0.0

    feats["liquidity_score"] = score
    return feats
