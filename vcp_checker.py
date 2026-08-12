"""
vcp_checker.py — entrypoint for the Korean VCP/Momentum scanner.

All business logic now lives in the vcp_scanner package:
  vcp_scanner/config.py       — constants, weights, market defaults
  vcp_scanner/kiwoom_client.py  — async REST wrappers (safe_post, paginated_post)
  vcp_scanner/data_quality.py — OHLCV validation, split adjustment, drop logging
  vcp_scanner/features/
      price.py     — Wilder EMA ATR contraction, vol pct, POC, breakout proximity, RS
      liquidity.py — median trading value, turnover proxy
      flows.py     — ka10058 universe prefetch + ka10059 per-symbol + scoring
      leverage.py  — credit level, delta, cross-sectional z-score
  vcp_scanner/compute.py   — compute_features(symbol) → dict
  vcp_scanner/ranking.py   — rank_candidates(df) → df  (flow spike-dampened)
  vcp_scanner/scanner.py   — run_scanner(bot) — main pipeline
  vcp_scanner/backtest.py  — offline forward-return evaluation
  vcp_scanner/report.py    — vcp_scorecard.parquet + report.md

Usage
-----
  # Standard scan (writes vcp_targets_ranked.csv + vcp_scorecard.parquet + report.md)
  .\\myenv2\\Scripts\\python.exe vcp_checker.py

  # Scan + run backtest on today's signals (writes vcp_backtest.parquet too)
  .\\myenv2\\Scripts\\python.exe vcp_checker.py --backtest

  # Backtest only (re-uses existing vcp_targets_ranked.csv, no Kiwoom API needed)
  .\\myenv2\\Scripts\\python.exe vcp_checker.py --backtest-only

Environment variables
---------------------
  KIWOOM_APPKEY          override API key
  KIWOOM_SECRETKEY       override secret
  KIWOOM_RATE_SLEEP      inter-request sleep in seconds (default 0.12)
  VCP_DEBUG              set to 1 to enable verbose debug logging
  VCP_GATE_TURNOVER      minimum ADV20 in KRW (default 5_000_000_000)
  VCP_GATE_FOREIGN_RATIO minimum 5-day foreign flow ratio (default 0.0)
  VCP_MAX_CANDIDATES     maximum candidates in final output (default 20)
  VCP_DROPPED_LOG        path for dropped-tickers log (default dropped_tickers.log)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from kiwoom import Bot, REAL
from kiwoom.http.client import Client as KiwoomHttpClient

from vcp_scanner.config import APP_KEY, APP_SECRET
from vcp_scanner.scanner import run_scanner
from vcp_scanner.ranking import summary_table

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(
    filename="vcp_checker_errors.log",
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

if os.getenv("VCP_DEBUG", "").strip().lower() in ("1", "true", "yes", "y", "on"):
    _dbg = logging.getLogger("vcp_scanner")
    _dbg.setLevel(logging.DEBUG)
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s — %(message)s"))
    _dbg.addHandler(_h)

KST = ZoneInfo("Asia/Seoul")
RANKED_CSV     = "vcp_targets_ranked.csv"
BACKTEST_PARQUET = "vcp_backtest.parquet"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Korean VCP/Momentum scanner (Kiwoom REST)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--backtest",
        action="store_true",
        help="Run forward-return backtest on today's signals after the scan.",
    )
    p.add_argument(
        "--backtest-only",
        action="store_true",
        dest="backtest_only",
        help=(
            "Skip the scan; load existing vcp_targets_ranked.csv and run backtest only. "
            "No Kiwoom API connection required."
        ),
    )
    p.add_argument(
        "--output-dir",
        default=".",
        dest="output_dir",
        help="Directory for report.md and vcp_scorecard.parquet (default: .)",
    )
    return p.parse_args()


async def _run_scan(args: argparse.Namespace) -> None:
    """Full scan via Kiwoom API, then report (and optional backtest)."""
    from datetime import datetime

    appkey    = os.getenv("KIWOOM_APPKEY",    APP_KEY)
    secretkey = os.getenv("KIWOOM_SECRETKEY", APP_SECRET)

    async with Bot(host=REAL, appkey=appkey, secretkey=secretkey) as bot:
        await KiwoomHttpClient.connect(bot.api, bot.api._appkey, bot.api._secretkey)
        try:
            ranked = await run_scanner(bot, output_file=RANKED_CSV)
        finally:
            try:
                await KiwoomHttpClient.close(bot.api)
            except Exception:
                pass

    print("\n=== VCP Momentum Scan Results ===")
    if ranked.empty:
        print("No candidates found.")
        return

    view = summary_table(ranked)
    print(view.head(20).to_string(index=False))
    print(f"\nFull results → {RANKED_CSV}  ({len(ranked)} candidates ranked)")

    # Generate parquet scorecard + report.md
    backtest_df = None
    if args.backtest and Path(RANKED_CSV).exists():
        backtest_df = _run_backtest_on_csv(RANKED_CSV)

    _write_report(ranked, backtest_df, args.output_dir)


def _run_backtest_on_csv(csv_path: str) -> "pd.DataFrame":
    """Load an existing ranked CSV and run the forward-return backtest."""
    from datetime import datetime
    from vcp_scanner.backtest import run_backtest

    signal_date = datetime.now(KST).date()
    try:
        return run_backtest(
            csv_path,
            signal_date=signal_date,
            output_parquet=BACKTEST_PARQUET,
        )
    except Exception as exc:
        print(f"  [WARN] Backtest failed: {exc}")
        import pandas as pd
        return pd.DataFrame()


def _write_report(ranked: "pd.DataFrame", backtest_df: "pd.DataFrame | None", output_dir: str) -> None:
    from vcp_scanner.report import generate_report
    print("\n[ Writing report artifacts ]")
    generate_report(ranked, backtest=backtest_df, output_dir=output_dir)


def _backtest_only_mode(args: argparse.Namespace) -> None:
    """No API needed — re-use existing CSV."""
    import pandas as pd

    if not Path(RANKED_CSV).exists():
        print(f"  [ERROR] {RANKED_CSV} not found. Run a scan first.")
        sys.exit(1)

    ranked     = pd.read_csv(RANKED_CSV, dtype={"symbol": str})
    backtest_df = _run_backtest_on_csv(RANKED_CSV)
    _write_report(ranked, backtest_df, args.output_dir)


async def main() -> None:
    args = _parse_args()

    if args.backtest_only:
        _backtest_only_mode(args)
        return

    await _run_scan(args)


if __name__ == "__main__":
    asyncio.run(main())
