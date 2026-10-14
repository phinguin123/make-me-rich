"""Backfill stocks-in-play research data for 2022-2023 without touching the existing 2024-2026 files.

  python -m trader.research.backfill download   # research/download.py's stages into data/research_stage_2022/
  python -m trader.research.backfill merge      # copy the new per-month / per-day files and append the single
                                                # files (in-play list, index ETF minutes) into data/research/,
                                                # backing up the originals first; existing 2024+ rows stay as they are
The 2,451 delisted symbols are included (Alpaca's asset list has active and inactive names).
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sys

import pandas as pd

from . import download as DL
from ..config import DATA_DIR

STAGE = DATA_DIR / "research_stage_2022"
LIVE = DATA_DIR / "research"
START, END = "2022-01-01", "2023-12-31"  # daily bars start 120 days earlier (2021-09) for the averages


def download() -> None:
    STAGE.mkdir(parents=True, exist_ok=True)
    DL.OUT = STAGE
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(STAGE / "download.log"), logging.StreamHandler()])
    asyncio.run(DL.main(START, END))


def merge() -> None:
    backup = LIVE / "backup_before_2022_merge"
    backup.mkdir(exist_ok=True)
    for sub in ("opening", "minute"):
        n = 0
        for f in sorted((STAGE / sub).glob("*.parquet")):
            dst = LIVE / sub / f.name
            if not dst.exists():
                shutil.copy2(f, dst)
                n += 1
        print(f"{sub}: {n} files added")
    for name in ("inplay.parquet", "macro_1m.parquet"):
        old, new = pd.read_parquet(LIVE / name), pd.read_parquet(STAGE / name)
        shutil.copy2(LIVE / name, backup / name)
        key = "date" if name == "inplay.parquet" else "ts"
        if name == "inplay.parquet":
            old_dates = set(pd.to_datetime(old["date"]).dt.date)
            new = new[~pd.to_datetime(new["date"]).dt.date.isin(old_dates)]
        else:
            new = new[new["ts"] < old["ts"].min()]
        pd.concat([new, old], ignore_index=True).sort_values(key).reset_index(drop=True).to_parquet(LIVE / name)
        print(f"{name}: +{len(new)} rows (original backed up in {backup.name}/)")


if __name__ == "__main__":
    {"download": download, "merge": merge}[sys.argv[1]]()
