"""
vcp_scanner/scanner.py

Main pipeline — run_scanner(bot) → ranked DataFrame.

Pipeline stages (with printed counts)
--------------------------------------
S0  Universe loaded (warrants / SPACs / REITs stripped)
S1  History gate      — need ≥ MIN_HISTORY_ROWS rows of OHLCV
S1q Data-quality gate — validates OHLCV; split-adjusts if heuristic fires
S2  Trend gate        — close > SMA20 > SMA50 > SMA200
S3  52w-high gate     — close ≥ 70 % of 52-week high
S4  Liquidity gate    — ADV20 turnover ≥ market-specific floor
S5  Volume-drought    — 5-day avg vol < 75 % of 50-day avg vol
S6  Feature compute   — REST calls (ka10001 + prefetched ka10058 flows)
S7  Hard gate: adv20_turnover_krw ≥ GATE_ADV_TURNOVER_KRW
S8  Hard gate: foreign_flow_ratio_5d ≥ GATE_FOREIGN_FLOW_RATIO_5D
S9  Final ranked output — top MAX_CANDIDATES

Drop audit
----------
All symbols excluded at any stage are written to dropped_tickers.log via
data_quality.log_drop().  The log includes timestamp, symbol, name, and
the specific reason code so you can audit the funnel offline.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from tqdm.asyncio import tqdm

try:
    import FinanceDataReader as fdr
except ModuleNotFoundError as e:
    raise ModuleNotFoundError(
        "FinanceDataReader is not available.\n"
        "Activate your venv: .\\myenv2\\Scripts\\activate"
    ) from e

from .compute import compute_features
from .config import (
    GATE_ADV_TURNOVER_KRW,
    GATE_FOREIGN_FLOW_RATIO_5D,
    KOSDAQ_MIN_TRADING_VALUE,
    KOSPI_MIN_TRADING_VALUE,
    KST,
    MAX_CANDIDATES,
    MIN_HISTORY_ROWS,
)
from .data_quality import adjust_for_splits, log_drop, validate_ohlcv
from .features.flows import prefetch_universe_flows
from .ranking import rank_candidates, summary_table

log = logging.getLogger(__name__)

# Regex to filter out preferred shares, SPACs, REITs, warrants
_IGNORE_PATTERN = r'우$|우[A-Z]$|스팩|제[0-9]+호|리츠'


def _min_tv(market_type: str) -> int:
    return KOSPI_MIN_TRADING_VALUE if market_type == "KOSPI" else KOSDAQ_MIN_TRADING_VALUE


def _stage(n: int | str, label: str, count: int, total: int | None = None) -> None:
    """Print a stage-count line that's easy to read and parse."""
    pct = f"  ({count / total * 100:.1f}%)" if total else ""
    print(f"  S{n}  {label:<40} {count:>5}{pct}")


async def run_scanner(
    bot,
    *,
    output_file: str   = "vcp_targets_ranked.csv",
    days_back:   int   = 400,    # OHLCV history window (calendar days)
    flow_days:   int   = 20,     # lookback for flow pre-fetch
    rest_sleep:  float = 0.12,   # seconds between REST calls
) -> pd.DataFrame:
    """
    Run the full VCP momentum scan and return a ranked DataFrame.

    Gate thresholds and output size are read from config.py (overridable
    via environment variables — see config.py for names).

    Outputs
    -------
    * vcp_targets_ranked.csv   — analyst-facing summary (always written)
    * dropped_tickers.log      — every excluded symbol with reason code

    Returns
    -------
    Ranked DataFrame (full feature set, not the summary view).
    Pass this to generate_report() for the parquet + report.md artifacts.
    """
    end_date   = datetime.now(KST).date()
    start_date = end_date - timedelta(days=days_back)

    # ── Universe ───────────────────────────────────────────────────────────────
    print("\n[ VCP Scanner — stage counts ]")
    universe  = fdr.StockListing("KRX")
    universe  = universe[~universe["Name"].str.contains(_IGNORE_PATTERN, regex=True, na=False)]
    universe  = universe.sort_values("Marcap", ascending=False).reset_index(drop=True)
    n_universe = len(universe)
    _stage(0, "Universe loaded", n_universe)

    kospi_set = set(fdr.StockListing("KOSPI")["Code"].astype(str))

    # ── Benchmarks ─────────────────────────────────────────────────────────────
    print("  Loading benchmark indices...")
    kospi_idx  = fdr.DataReader("KS11", start_date, end_date)
    kosdaq_idx = fdr.DataReader("KQ11", start_date, end_date)

    # ── Pre-fetch universe flows (ka10058) — both 20d and 5d windows ──────────
    flow_end      = end_date.strftime("%Y%m%d")
    flow_start_20 = (end_date - timedelta(days=flow_days + 7)).strftime("%Y%m%d")
    flow_start_5  = (end_date - timedelta(days=5 + 4)).strftime("%Y%m%d")  # +4 cal buffer

    print(f"  Pre-fetching 20d flows  ({flow_start_20}→{flow_end})...")
    flows_20d = await prefetch_universe_flows(
        bot, flow_start_20, flow_end, suffix="_net_20d", sleep=rest_sleep
    )
    print(f"  Pre-fetching  5d flows  ({flow_start_5}→{flow_end})...")
    flows_5d = await prefetch_universe_flows(
        bot, flow_start_5, flow_end, suffix="_net_5d", sleep=rest_sleep
    )

    for sym, d in flows_5d.items():
        flows_20d.setdefault(sym, {}).update(d)
    prefetched_flows = flows_20d
    print(f"  Flow cache covers {len(prefetched_flows)} symbols")

    # ── Remove stale output ────────────────────────────────────────────────────
    if os.path.exists(output_file):
        os.remove(output_file)

    # ── Per-symbol OHLCV fast-filters + feature computation ───────────────────
    all_features: list[dict[str, Any]] = []
    drop_counts: dict[str, int] = {}

    # Stage counters
    n_s1 = n_s1q = n_s2 = n_s3 = n_s4 = n_s5 = 0

    print(f"\n  Scanning {n_universe} symbols (fast filters → REST)...")

    async for _, row in tqdm(universe.iterrows(), total=n_universe):
        symbol = str(row["Code"])
        name   = str(row["Name"])
        mtype  = "KOSPI" if symbol in kospi_set else "KOSDAQ"
        bench  = kospi_idx if mtype == "KOSPI" else kosdaq_idx

        try:
            df = fdr.DataReader(symbol, start_date, end_date)

            # S1 — History row count
            if len(df) < MIN_HISTORY_ROWS:
                reason = f"insufficient_rows:{len(df)}"
                log_drop(symbol, name, reason)
                drop_counts[reason[:20]] = drop_counts.get(reason[:20], 0) + 1
                continue
            n_s1 += 1

            # S1q — Data-quality validation + split adjustment
            vr = validate_ohlcv(df, symbol, name, min_rows=MIN_HISTORY_ROWS)
            if not vr.ok:
                log_drop(symbol, name, vr.reason)
                drop_counts[vr.reason[:20]] = drop_counts.get(vr.reason[:20], 0) + 1
                continue

            # Apply split adjustment when heuristic fires; log if adjusted
            df, was_split = adjust_for_splits(df)
            if was_split:
                log.info("Split-adjusted %s (%s)", symbol, name)
            n_s1q += 1

            close    = float(df["Close"].iloc[-1])
            sma20    = float(df["Close"].rolling(20).mean().iloc[-1])
            sma50    = float(df["Close"].rolling(50).mean().iloc[-1])
            sma200   = float(df["Close"].rolling(200).mean().iloc[-1])
            high_52w = float(df["High"].tail(252).max())

            # S2 — Trend alignment
            if not (close > sma20 > sma50 > sma200):
                log_drop(symbol, name, "trend_gate")
                drop_counts["trend_gate"] = drop_counts.get("trend_gate", 0) + 1
                continue
            n_s2 += 1

            # S3 — 52-week high ceiling proximity
            if close < 0.70 * high_52w:
                log_drop(symbol, name, "52w_ceiling_gate")
                drop_counts["52w_ceiling_gate"] = drop_counts.get("52w_ceiling_gate", 0) + 1
                continue
            n_s3 += 1

            # S4 — Liquidity floor (market-specific min trading value)
            tv_20d = float((df["Close"] * df["Volume"]).tail(20).mean())
            if tv_20d < _min_tv(mtype):
                log_drop(symbol, name, f"liquidity_gate:{mtype}")
                drop_counts["liquidity_gate"] = drop_counts.get("liquidity_gate", 0) + 1
                continue
            n_s4 += 1

            # S5 — Volume drought: 5d avg < 75 % of 50d avg
            vol_5d  = float(df["Volume"].tail(5).mean())
            vol_50d = float(df["Volume"].tail(50).mean())
            if vol_50d > 0 and vol_5d > vol_50d * 0.75:
                log_drop(symbol, name, "volume_drought_gate")
                drop_counts["volume_drought_gate"] = drop_counts.get("volume_drought_gate", 0) + 1
                continue
            n_s5 += 1

            # S6 — Full feature computation (REST calls happen here)
            await asyncio.sleep(rest_sleep)

            feats = await compute_features(
                bot,
                symbol,
                df,
                bench,
                market_type=mtype,
                prefetched_flows=prefetched_flows,
                flow_days=flow_days,
                rest_sleep=rest_sleep,
            )

            if not feats:
                log_drop(symbol, name, "feature_compute_failed")
                continue

            feats["name"] = name
            all_features.append(feats)

        except Exception as exc:
            log.warning("Failed on %s (%s): %s", symbol, name, exc)
            log_drop(symbol, name, f"exception:{type(exc).__name__}")
            continue

    # ── Print OHLCV filter funnel ──────────────────────────────────────────────
    print()
    _stage(1,  "History ≥ 200 rows",               n_s1,           n_universe)
    _stage("1q", "Data-quality gate (OHLCV valid)",  n_s1q,          n_universe)
    _stage(2,  "Trend: close > SMA20>50>200",       n_s2,           n_universe)
    _stage(3,  "Ceiling: within 30% of 52w high",   n_s3,           n_universe)
    _stage(4,  "Liquidity: ADV20 floor",             n_s4,           n_universe)
    _stage(5,  "Volume drought (5d < 75% of 50d)",  n_s5,           n_universe)
    _stage(6,  "Feature compute (REST survived)",   len(all_features), n_universe)

    if not all_features:
        print("\n  No candidates survived the scan.")
        return pd.DataFrame()

    feat_df = pd.DataFrame(all_features)

    # ── S7: Hard gate — ADV20 turnover (KRW) ──────────────────────────────────
    adv_col = "avg_value_20d"
    if adv_col in feat_df.columns and GATE_ADV_TURNOVER_KRW > 0:
        mask_tv = pd.to_numeric(feat_df[adv_col], errors="coerce").fillna(0) >= GATE_ADV_TURNOVER_KRW
        dropped = feat_df[~mask_tv]
        for _, dr in dropped.iterrows():
            log_drop(str(dr.get("symbol", "")), str(dr.get("name", "")),
                     f"adv20_gate:<{GATE_ADV_TURNOVER_KRW/1e8:.0f}억")
        feat_df = feat_df[mask_tv]
    _stage(7, f"ADV20 ≥ {GATE_ADV_TURNOVER_KRW / 1e8:.0f}억 KRW",
           len(feat_df), n_universe)

    # ── S8: Hard gate — foreign flow ratio (5d) ────────────────────────────────
    ratio_col = "foreign_flow_ratio_5d"
    if ratio_col in feat_df.columns and GATE_FOREIGN_FLOW_RATIO_5D >= 0:
        mask_ff = (
            pd.to_numeric(feat_df[ratio_col], errors="coerce")
            .fillna(-999) >= GATE_FOREIGN_FLOW_RATIO_5D
        )
        dropped = feat_df[~mask_ff]
        for _, dr in dropped.iterrows():
            log_drop(str(dr.get("symbol", "")), str(dr.get("name", "")),
                     f"foreign_flow_gate:<{GATE_FOREIGN_FLOW_RATIO_5D:.2f}")
        feat_df = feat_df[mask_ff]
    _stage(8, f"Foreign ratio 5d ≥ {GATE_FOREIGN_FLOW_RATIO_5D:.2f}× ADV",
           len(feat_df), n_universe)

    if feat_df.empty:
        print("\n  No candidates survived the hard gates.")
        return pd.DataFrame()

    # ── S9: Rank and cap ───────────────────────────────────────────────────────
    ranked  = rank_candidates(feat_df)
    top_n   = ranked.head(MAX_CANDIDATES)
    summary = summary_table(top_n)
    _stage(9, f"Final top-{MAX_CANDIDATES} ranked", len(summary), n_universe)

    # Print drop-reason summary
    if drop_counts:
        print("\n  Drop-reason summary:")
        for reason, cnt in sorted(drop_counts.items(), key=lambda x: -x[1])[:8]:
            print(f"    {reason:<28}  {cnt:>5}")
        print(f"  (Full list → dropped_tickers.log)")

    # ── Export CSV ─────────────────────────────────────────────────────────────
    summary.to_csv(output_file, index=False, encoding="utf-8-sig")
    print(f"\n  Saved → {output_file}\n")

    return top_n
