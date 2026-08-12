"""
vcp_scanner/backtest.py

Offline backtester for VCP scanner signal outputs.

Usage
-----
    # Standalone (after a scan has produced vcp_targets_ranked.csv)
    from vcp_scanner.backtest import run_backtest
    results = run_backtest(
        "vcp_targets_ranked.csv",
        signal_date="2026-04-29",
        output_parquet="vcp_backtest.parquet",
    )

    # From the CLI (--backtest flag added to vcp_checker.py)
    .\\myenv2\\Scripts\\python.exe vcp_checker.py --backtest

Design
------
* Signal date  = the date the scan was executed (T+0).
* Entry price  = Open of T+1 (first available trading day after signal).
* Exit prices  = Close of T+N for N ∈ FORWARD_HORIZONS (1, 5, 10, 20).
* If market is closed on T+N (holiday / weekend) we use the next trading day.
* Max drawdown is computed over the 20-bar forward window using a daily
  peak-to-trough calculation.
* VI / limit-up incidence counts days where (High − Low) / prev_close > 10 %.

Korean-market notes
-------------------
* OHLCV from FinanceDataReader already excludes non-trading days, so array
  indexing by trading-day count handles holidays automatically.
* We fetch LOOKFORWARD_CALENDAR_DAYS = 45 calendar days (~30 trading days)
  to guarantee at least 20 trading-day forward bars.
* Backtest is purely descriptive / diagnostic — no transaction costs, no
  slippage, no KRX circuit-breaker modelling.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

try:
    import FinanceDataReader as fdr
except ModuleNotFoundError as e:
    raise ModuleNotFoundError(
        "FinanceDataReader is not available. Activate your venv: "
        ".\\myenv2\\Scripts\\activate"
    ) from e

log = logging.getLogger(__name__)

FORWARD_HORIZONS: tuple[int, ...] = (1, 5, 10, 20)
VI_THRESHOLD_PCT: float = 10.0
LIMIT_UP_PCT: float = 29.0
LOOKFORWARD_CALENDAR_DAYS: int = 45   # fetch window; guarantees ≥20 trading bars


# ── Internal helpers ──────────────────────────────────────────────────────────

def _max_drawdown(prices: pd.Series) -> float:
    """
    Peak-to-trough maximum drawdown over a price series.

    Returns a negative percentage, e.g. -12.5 means -12.5 % drawdown.
    Returns 0.0 if the series has fewer than 2 points.
    """
    if len(prices) < 2:
        return 0.0
    cum_max = prices.cummax()
    dd = (prices - cum_max) / cum_max.replace(0.0, np.nan)
    return float(dd.min()) * 100.0


def _vi_incidence(df: pd.DataFrame) -> int:
    """
    Count trading days where (High − Low) / prev_close > VI_THRESHOLD_PCT.
    """
    if df.empty or "High" not in df.columns or len(df) < 2:
        return 0
    prev_close = df["Close"].shift(1).replace(0.0, np.nan)
    pct_range  = (df["High"] - df["Low"]) / prev_close * 100.0
    return int((pct_range > VI_THRESHOLD_PCT).sum())


def _limit_up_incidence(df: pd.DataFrame) -> int:
    """Count days where close/prev_close − 1 > LIMIT_UP_PCT."""
    if df.empty or len(df) < 2:
        return 0
    prev_close = df["Close"].shift(1).replace(0.0, np.nan)
    ret_pct    = (df["Close"] / prev_close - 1.0) * 100.0
    return int((ret_pct > LIMIT_UP_PCT).sum())


def _fetch_forward(symbol: str, signal_date: date) -> pd.DataFrame | None:
    """Fetch OHLCV for the forward evaluation window."""
    start = signal_date
    end   = signal_date + timedelta(days=LOOKFORWARD_CALENDAR_DAYS)
    try:
        df = fdr.DataReader(
            str(symbol),
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
        )
        return df if not df.empty else None
    except Exception as exc:
        log.warning("Forward fetch failed for %s: %s", symbol, exc)
        return None


# ── Single-signal evaluator ───────────────────────────────────────────────────

def evaluate_signal(
    symbol: str,
    name: str,
    market_type: str,
    composite_score: float,
    signal_date: date,
    extra_cols: dict | None = None,
) -> dict:
    """
    Compute all forward-return metrics for one scanner signal.

    Parameters
    ----------
    symbol          : KRX 6-digit code
    name            : ticker name
    market_type     : 'KOSPI' or 'KOSDAQ'
    composite_score : scanner composite score at signal time
    signal_date     : date on which the scan was run (T+0)
    extra_cols      : additional fields from the scanner output to carry forward
                      (e.g. price_score, flows_score, etc.)

    Returns
    -------
    dict with keys: symbol, name, market_type, composite_score, signal_date,
                    forward_ret_{n}d, max_dd_20d, vi_count_20d,
                    limit_up_count_20d, data_ok, plus any extra_cols.
    """
    fwd = _fetch_forward(symbol, signal_date)

    row: dict = {
        "symbol":          symbol,
        "name":            name,
        "market_type":     market_type,
        "composite_score": composite_score,
        "signal_date":     signal_date.isoformat(),
    }
    if extra_cols:
        row.update(extra_cols)

    if fwd is None or len(fwd) < 2:
        for h in FORWARD_HORIZONS:
            row[f"forward_ret_{h}d"] = None
        row["max_dd_20d"]          = None
        row["vi_count_20d"]        = None
        row["limit_up_count_20d"]  = None
        row["data_ok"]             = False
        return row

    row["data_ok"] = True

    # Entry = open of first available trading bar on or after T+1
    entry_open  = fwd["Open"].iloc[0] if "Open" in fwd.columns else fwd["Close"].iloc[0]
    entry_price = float(entry_open) if entry_open > 0 else float(fwd["Close"].iloc[0])

    for h in FORWARD_HORIZONS:
        if len(fwd) > h:
            exit_close = float(fwd["Close"].iloc[h])
            row[f"forward_ret_{h}d"] = round((exit_close / entry_price - 1.0) * 100.0, 2)
        else:
            row[f"forward_ret_{h}d"] = None

    # Max drawdown and event counts over the 20-trading-day forward window
    fwd20  = fwd.head(20)
    prices = pd.concat(
        [pd.Series([entry_price], index=[fwd.index[0]]), fwd20["Close"]]
    ).reset_index(drop=True)
    row["max_dd_20d"]         = round(_max_drawdown(prices), 2)
    row["vi_count_20d"]       = _vi_incidence(fwd20)
    row["limit_up_count_20d"] = _limit_up_incidence(fwd20)

    return row


# ── Batch backtest ────────────────────────────────────────────────────────────

def run_backtest(
    signals_csv: str | Path,
    *,
    signal_date: date | str | None = None,
    output_parquet: str | Path | None = None,
    score_cols: Sequence[str] = (
        "price_score", "liquidity_score", "flows_score", "leverage_score",
    ),
) -> pd.DataFrame:
    """
    Run a forward-return backtest on a scanner output CSV.

    Parameters
    ----------
    signals_csv    : path to vcp_targets_ranked.csv (or any compatible CSV)
    signal_date    : T+0 date; defaults to today (KST) if not provided
    output_parquet : if set, saves results to this .parquet path
    score_cols     : additional score columns to carry into the output

    Returns
    -------
    DataFrame indexed by symbol with forward return metrics and score
    breakdowns, ready for analysis or report generation.
    """
    from zoneinfo import ZoneInfo
    from datetime import datetime

    signals = pd.read_csv(signals_csv, dtype={"symbol": str})
    if signals.empty:
        log.warning("No signals found in %s", signals_csv)
        return pd.DataFrame()

    if signal_date is None:
        signal_date = datetime.now(ZoneInfo("Asia/Seoul")).date()
    elif isinstance(signal_date, str):
        signal_date = date.fromisoformat(signal_date)

    print(f"\n[ Backtest — {len(signals)} signals — entry date T+1 from {signal_date} ]")

    results: list[dict] = []
    for _, row in signals.iterrows():
        extra = {c: row[c] for c in score_cols if c in signals.columns}
        rec = evaluate_signal(
            symbol          = str(row["symbol"]),
            name            = str(row.get("name", "")),
            market_type     = str(row.get("market_type", "KOSDAQ")),
            composite_score = float(row.get("composite_score", 0.0)),
            signal_date     = signal_date,
            extra_cols      = extra,
        )
        results.append(rec)

    df = pd.DataFrame(results)

    # Summarise to console
    valid = df[df["data_ok"] == True]
    print(f"  Data available: {len(valid)}/{len(df)} signals")
    for h in FORWARD_HORIZONS:
        col = f"forward_ret_{h}d"
        if col in valid.columns:
            s = pd.to_numeric(valid[col], errors="coerce").dropna()
            if not s.empty:
                win = (s > 0).mean() * 100
                print(f"  {h:>2}d  mean={s.mean():+.2f}%  median={s.median():+.2f}%  win%={win:.0f}%")

    if output_parquet:
        df.to_parquet(str(output_parquet), index=False)
        print(f"  Saved backtest → {output_parquet}")

    return df
