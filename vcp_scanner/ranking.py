"""
vcp_scanner/ranking.py

rank_candidates(df) → df

Takes the raw feature DataFrame produced by compute_features() and returns
a fully ranked, explainable output.

Ranking pipeline
----------------
1.  Cross-sectional credit z-score  — updates leverage_score to be
    universe-aware (a stock with 3 % credit is only good or bad relative
    to today's universe distribution).
2.  Flow spike dampening            — penalises symbols where the 5-day
    normalised flow is more than FLOW_SPIKE_RATIO × the 20-day norm.
    Korean theme stocks can show a single day of massive retail/foreign buy
    that inflates ignition_5d without representing a genuine institutional
    accumulation trend.
3.  Recompute composite_score from the four group scores.
4.  Attach score_detail_* columns that show each sub-feature's weighted
    contribution to composite_score (they sum to composite_score).
5.  Sort descending; attach rank column.

summary_table(ranked_df) → df
    Returns the concise analyst-facing view of the ranked output.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import (
    FLOWS_SUB_WEIGHTS,
    KOSDAQ_MAX_CREDIT_RATE,
    KOSPI_MAX_CREDIT_RATE,
    LEVERAGE_SUB_WEIGHTS,
    PRICE_SUB_WEIGHTS,
    SCORE_WEIGHTS,
)

# ── Flow spike-dampening config ───────────────────────────────────────────────
# When |flow_5d_norm| > FLOW_SPIKE_RATIO × |flow_20d_norm| the 5-day window
# is at least this many times "hotter" than the 20-day baseline, suggesting a
# single-day event rather than sustained institutional accumulation.
# The flows_score for that symbol is multiplied by FLOW_SPIKE_PENALTY.
FLOW_SPIKE_RATIO:   float = 2.5
FLOW_SPIKE_PENALTY: float = 0.80   # dampen by 20 %


# ── Helpers ───────────────────────────────────────────────────────────────────

def _robust_zscore(series: pd.Series) -> pd.Series:
    """
    MAD-based robust z-score: not distorted by outlier credit spikes.
    Uses 1.4826 × MAD as the consistent standard-deviation estimator.
    """
    med = series.median()
    mad = (series - med).abs().median()
    if mad == 0.0:
        return pd.Series(np.zeros(len(series)), index=series.index)
    return (series - med) / (1.4826 * mad)


def _to_numeric(df: pd.DataFrame, col: str, fill: float = 0.5) -> pd.Series:
    if col in df.columns:
        return pd.to_numeric(df[col], errors="coerce").fillna(fill)
    return pd.Series([fill] * len(df), index=df.index)


def _flow_spike_multiplier(df: pd.DataFrame) -> pd.Series:
    """
    Return a per-row penalty multiplier for the flows_score.

    A multiplier of 1.0 = no penalty; FLOW_SPIKE_PENALTY (0.80) = 20 % dampen.

    Logic
    -----
    If |foreign_net_5d / adv_shares| > FLOW_SPIKE_RATIO × |foreign_net_20d / adv_shares|
    AND the 20d norm is not near-zero (to avoid division noise),
    then we flag this as a "spike" and apply the penalty.

    We use the foreign flow as the primary spike indicator because foreign
    retail surges around Korean theme events are the most common false signal.
    """
    f5  = pd.to_numeric(df.get("flow_foreign_norm",  pd.Series([0.0] * len(df), index=df.index)), errors="coerce").fillna(0.0)
    f20 = pd.to_numeric(df.get("flow_foreign_norm",  pd.Series([0.0] * len(df), index=df.index)), errors="coerce").fillna(0.0)

    # Derive 5d foreign norm directly if available
    if "foreign_net_5d" in df.columns and "adv_shares_20d" in df.columns:
        adv  = pd.to_numeric(df["adv_shares_20d"],   errors="coerce").replace(0, np.nan)
        fn5  = pd.to_numeric(df["foreign_net_5d"],   errors="coerce").fillna(0.0)
        fn20 = pd.to_numeric(df.get("foreign_net_20d", pd.Series([0.0] * len(df), index=df.index)), errors="coerce").fillna(0.0)
        f5   = fn5  / adv
        f20  = fn20 / adv

    # Spike condition: |5d| > FLOW_SPIKE_RATIO × |20d| and 20d is non-trivial
    baseline_nonzero = f20.abs() > 0.05   # 5 % of ADV as minimum baseline
    spike_flag = baseline_nonzero & (f5.abs() > FLOW_SPIKE_RATIO * f20.abs())

    multiplier = pd.Series(1.0, index=df.index)
    multiplier[spike_flag] = FLOW_SPIKE_PENALTY
    return multiplier


# ── Main ranking function ─────────────────────────────────────────────────────

def rank_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """
    Re-score and rank a feature DataFrame.

    Parameters
    ----------
    df : DataFrame where each row is one symbol's compute_features() output.

    Returns
    -------
    Ranked DataFrame (descending composite_score) with added columns:
      rank                      — 1-based integer rank
      composite_score           — final weighted composite [0, 1]
      price_score               — price group score [0, 1]
      liquidity_score           — liquidity group score [0, 1]
      flows_score               — flows group score [0, 1] (spike-dampened)
      leverage_score            — leverage group score [0, 1] (cross-sectionally adjusted)
      flow_spike_flag           — 1 if flow spike dampen was applied, else 0
      score_detail_price_*      — weighted contribution of each price sub-feature
      score_detail_flows_*      — weighted contribution of each flow sub-feature
      score_detail_leverage     — leverage group contribution
      score_detail_liquidity    — liquidity group contribution
    """
    if df.empty:
        return df

    df = df.copy()

    # ── Step 1: Cross-sectional credit z-score → updated leverage_score ───────
    if "credit_level" in df.columns:
        credit_vals = pd.to_numeric(df["credit_level"], errors="coerce")
        credit_fill = credit_vals.median()
        credit_filled = credit_vals.fillna(credit_fill)

        # Per-symbol max_rate based on market_type
        max_rates = df.get(
            "market_type", pd.Series(["KOSDAQ"] * len(df), index=df.index)
        ).map(
            lambda m: KOSDAQ_MAX_CREDIT_RATE if str(m).upper() == "KOSDAQ"
            else KOSPI_MAX_CREDIT_RATE
        )

        credit_z    = _robust_zscore(credit_filled)
        level_score = (1.0 - credit_filled.clip(lower=0) / max_rates).clip(0.0, 1.0)

        d5 = pd.to_numeric(
            df.get("credit_delta_5d", pd.Series([0.0] * len(df), index=df.index)),
            errors="coerce",
        ).fillna(0.0)
        delta_score = (0.5 - d5 / (max_rates * 2.0)).clip(0.0, 1.0)

        d20 = pd.to_numeric(
            df.get("credit_delta_20d", pd.Series([0.0] * len(df), index=df.index)),
            errors="coerce",
        ).fillna(0.0)
        delta_20d_score = (0.5 - d20 / (max_rates * 2.0)).clip(0.0, 1.0)

        z_score_score = (0.5 - credit_z * 0.1).clip(0.0, 1.0)

        df["credit_zscore"] = credit_z.round(4)

        W = LEVERAGE_SUB_WEIGHTS
        df["leverage_score"] = (
            W["level"]     * level_score
            + W["delta_5d"]  * delta_score
            + W["delta_20d"] * delta_20d_score
            + W["zscore"]    * z_score_score
        ).clip(0.0, 1.0)

    # ── Step 2: Flow spike dampening ──────────────────────────────────────────
    spike_mult = _flow_spike_multiplier(df)
    df["flow_spike_flag"] = (spike_mult < 1.0).astype(int)

    # Ensure all required group-score columns are present and numeric
    for col in ("price_score", "liquidity_score", "flows_score", "leverage_score"):
        df[col] = _to_numeric(df, col, fill=0.5)

    # Apply spike dampen to flows_score
    df["flows_score"] = (df["flows_score"] * spike_mult).clip(0.0, 1.0)

    # ── Step 3: Final composite score ─────────────────────────────────────────
    W = SCORE_WEIGHTS
    df["composite_score"] = (
        W["price"]     * df["price_score"]
        + W["liquidity"] * df["liquidity_score"]
        + W["flows"]     * df["flows_score"]
        + W["leverage"]  * df["leverage_score"]
    ).clip(0.0, 1.0).round(6)

    # ── Step 4: Explainable score-detail columns ──────────────────────────────
    # Each column is the weighted contribution to composite_score.
    # All score_detail_* columns sum to composite_score.

    PW = PRICE_SUB_WEIGHTS
    for raw_col, sub_w, label in [
        ("atr_contraction_score", PW["atr_contraction"], "atr_contraction"),
        ("vol_percentile_score",  PW["vol_percentile"],  "vol_percentile"),
        ("breakout_prox_score",   PW["breakout_prox"],   "breakout_prox"),
        ("poc_distance_score",    PW["poc_distance"],    "poc_distance"),
        ("rs_score",              PW["rs_score"],        "rs"),
    ]:
        src = _to_numeric(df, raw_col, fill=0.5)
        df[f"score_detail_price_{label}"] = (W["price"] * sub_w * src).round(4)

    FW = FLOWS_SUB_WEIGHTS
    for raw_col, sub_key, label in [
        ("flow_foreign_score",   "foreign",   "foreign"),
        ("flow_pension_score",   "pension",   "pension"),
        ("flow_fininvest_score", "fininvest", "fininvest"),
        ("flow_invtrust_score",  "invtrust",  "invtrust"),
    ]:
        src = _to_numeric(df, raw_col, fill=0.5)
        # Include the spike multiplier so that score_detail_flows_* still sums to
        # the flows contribution in composite_score (which is post-dampening).
        df[f"score_detail_flows_{label}"] = (W["flows"] * FW[sub_key] * src * spike_mult).round(4)

    df["score_detail_leverage"]  = (W["leverage"]  * df["leverage_score"]).round(4)
    df["score_detail_liquidity"] = (W["liquidity"] * df["liquidity_score"]).round(4)

    # ── Step 5: Sort and rank ─────────────────────────────────────────────────
    df = df.sort_values("composite_score", ascending=False).reset_index(drop=True)
    df.insert(0, "rank", df.index + 1)

    return df


# ── Analyst-facing summary ────────────────────────────────────────────────────

def summary_table(ranked_df: pd.DataFrame) -> pd.DataFrame:
    """
    Concise analyst-facing view of the ranked output.

    Columns included (if present)
    ------------------------------
    Identity     : rank, symbol, name, market_type, last_close
    Scores       : composite_score, price_score, liquidity_score,
                   flows_score, leverage_score
    Flags        : flow_spike_flag, vi_days_in_window
    Score drilldown : all score_detail_* columns
    Key raw      : atr_5_20_ratio, atr_10_40_ratio, vol_percentile,
                   poc_distance_pct, breakout_proximity_pct,
                   rs_3m, rs_6m, med_value_20d,
                   credit_level, credit_delta_5d, credit_zscore,
                   foreign_flow_ratio_5d,
                   flow_foreign_norm, flow_pension_norm,
                   flow_inst_weighted_norm, flow_ignition_5d
    """
    identity_cols = ["rank", "symbol", "name", "market_type", "last_close"]
    score_cols    = [
        "composite_score",
        "price_score", "liquidity_score", "flows_score", "leverage_score",
    ]
    flag_cols  = ["flow_spike_flag", "vi_days_in_window"]
    detail_cols = sorted(c for c in ranked_df.columns if c.startswith("score_detail_"))
    raw_cols   = [
        "atr_5_20_ratio", "atr_10_40_ratio",
        "vol_percentile",
        "poc_distance_pct",
        "breakout_proximity_pct",
        "rs_3m", "rs_6m",
        "med_value_20d",
        "adv_shares_20d",
        "credit_level", "credit_delta_5d", "credit_zscore",
        "foreign_flow_ratio_5d",
        "flow_foreign_norm", "flow_pension_norm",
        "flow_fininvest_norm", "flow_invtrust_norm",
        "flow_inst_weighted_norm", "flow_ignition_5d",
        "days_with_data",
    ]

    keep = [
        c for c in identity_cols + score_cols + flag_cols + detail_cols + raw_cols
        if c in ranked_df.columns
    ]

    out = ranked_df[keep].copy()

    round_rules: dict[str, int] = {}

    for c in score_cols + detail_cols:
        round_rules[c] = 4

    for c in (
        "atr_5_20_ratio", "atr_10_40_ratio", "rs_3m", "rs_6m",
        "foreign_flow_ratio_5d",
        "flow_foreign_norm", "flow_pension_norm",
        "flow_fininvest_norm", "flow_invtrust_norm",
        "flow_inst_weighted_norm", "flow_ignition_5d",
    ):
        round_rules[c] = 3

    for c in ("vol_percentile", "poc_distance_pct", "breakout_proximity_pct"):
        round_rules[c] = 1

    for c in ("credit_level", "credit_delta_5d", "credit_zscore"):
        round_rules[c] = 2

    for c in ("med_value_20d", "adv_shares_20d"):
        round_rules[c] = 0

    for col, dp in round_rules.items():
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").round(dp)

    return out
