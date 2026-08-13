"""
Entrypoint for the Korean VCP/Momentum scanner.

Usage
-----
  python -m vcp_scanner
  python -m vcp_scanner --backtest
  python -m vcp_scanner --backtest-only

Writes ranked CSV, scorecard, and report.md under output/vcp/.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
_BACKEND = _ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from kiwoom import Bot, REAL
from kiwoom.http.client import Client as KiwoomHttpClient

from vcp_scanner.config import APP_KEY, APP_SECRET, OUTPUT_DIR
from vcp_scanner.scanner import run_scanner
from vcp_scanner.ranking import summary_table

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(OUTPUT_DIR / "vcp_checker_errors.log"),
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
RANKED_CSV = str(OUTPUT_DIR / "vcp_targets_ranked.csv")
BACKTEST_PARQUET = str(OUTPUT_DIR / "vcp_backtest.parquet")


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
        default=str(OUTPUT_DIR),
        dest="output_dir",
        help="Directory for report.md and vcp_scorecard.parquet (default: output/vcp)",
    )
    return p.parse_args()


async def _run_scan(args: argparse.Namespace) -> None:
    """Full scan via Kiwoom API, then report (and optional backtest)."""
    if not APP_KEY or not APP_SECRET:
        print("  [ERROR] Missing APP_KEY / APP_SECRET. Copy .env.example to .env.")
        sys.exit(1)

    async with Bot(host=REAL, appkey=APP_KEY, secretkey=APP_SECRET) as bot:
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

    ranked = pd.read_csv(RANKED_CSV, dtype={"symbol": str})
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
