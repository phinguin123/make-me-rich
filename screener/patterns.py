"""
patterns.py — Chart pattern utilities shared across algorithms.

Currently exposes a single global pulse-check used by both the master
scanner pre-filter and the diagnostics tooling: every other historical
helper (Stage-2 trend template, swing detection, VCP contractions) was
tied to the now-retired Algorithms 1-3 and has been removed.
"""

import pandas as pd


def is_buyout_or_flatline(df: pd.DataFrame) -> bool:
    """
    Returns True if the ticker looks like an acquisition target or halted chart.

    Condition 1 — 3+ days in the last 10 where the daily Close change is exactly 0.
    Condition 2 — Mean intraday (High-Low)/Low over the last 5 days < 0.25%.
    """
    tail10 = df.tail(10)
    tail5  = df.tail(5)

    flat_days = tail10["Close"].diff().eq(0).sum()
    if flat_days >= 3:
        return True

    avg_spread = ((tail5["High"] - tail5["Low"]) / tail5["Low"]).mean()
    if avg_spread < 0.0025:
        return True

    return False
