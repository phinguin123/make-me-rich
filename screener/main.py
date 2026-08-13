"""
main.py — Entry point for the Alpaca Master Screener.

Usage
-----
Normal scan:
    python -m screener.main

Diagnostics (Algorithm 1 — Minervini Leader Pullback):
    python -m screener.main --diagnose-mpb [--sample N]

Diagnostics (Algorithm 2 — Power Trend Pullback):
    python -m screener.main --diagnose-ptp [--sample N]

Fine-tuning workflow
--------------------
Edit screener/config.py to change any threshold, then re-run.
Both algorithms and the diagnostics tools automatically pick up the new values.
"""

import sys
import logging
import warnings
from pathlib import Path

import pandas as pd

warnings.simplefilter(action="ignore", category=pd.errors.PerformanceWarning)

_LOG_DIR = Path(__file__).resolve().parents[1] / "output" / "us"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=str(_LOG_DIR / "alpaca_scanner_errors.log"), level=logging.WARNING)


def _get_sample_arg(default: int = 500) -> int:
    if "--sample" in sys.argv:
        idx = sys.argv.index("--sample")
        return int(sys.argv[idx + 1])
    return default


def main() -> None:
    if "--diagnose-mpb" in sys.argv:
        from screener.diagnostics import run_minervini_diagnostics
        run_minervini_diagnostics(sample_size=_get_sample_arg(500))
        return

    if "--diagnose-ptp" in sys.argv:
        from screener.diagnostics import run_ptp_diagnostics
        run_ptp_diagnostics(sample_size=_get_sample_arg(500))
        return

    from screener.engine import run_master_scanner

    mpb, ptp = run_master_scanner()

    # ── Algorithm 1: Minervini Leader Pullback ───────────────────────────────
    print("\n" + "=" * 60)
    print("ALGORITHM 1: MINERVINI LEADER PULLBACK (UPGRADED)")
    print("=" * 60)
    if not mpb.empty:
        display_cols = [
            "Symbol", "is_trigger", "risk_cap_blocked",
            "price", "entry_price", "stop_price", "risk_pct",
            "handle_ceiling", "handle_range_atr", "handle_close_band_pct",
            "stretch_pct", "bars_since_peak",
            "dist_sma20_atr", "dist_vwap20_atr",
            "touch_count_window",
            "flagged_count_window", "distribution_count_window", "stall_count_window",
            "vol_ratio_3_50", "stretch_from_sma50_pct",
        ]
        print(mpb[[c for c in display_cols if c in mpb.columns]].to_string(index=False))
    else:
        print("No Minervini Leader Pullback setups found today.")

    # ── Algorithm 2: Power Trend Pullback (PTP) ──────────────────────────────
    print("\n" + "=" * 60)
    print("ALGORITHM 2: POWER TREND PULLBACK (PTP)")
    print("=" * 60)
    if not ptp.empty:
        display_cols = [
            "Symbol",
            "price", "trigger_price", "stop_price", "pullback_low", "risk_pct",
            "down_streak", "stretch_5d_pct", "ret_40d_pct", "adx14",
            "ema10", "ema21", "sma50", "sma200",
            "vol_dry", "intraday_rvol_min",
        ]
        print(ptp[[c for c in display_cols if c in ptp.columns]].to_string(index=False))
    else:
        print("No Power Trend Pullback setups found today.")
    print()


if __name__ == "__main__":
    main()
