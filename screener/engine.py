"""
engine.py — Master screener engine.

Fetches the active US-equity universe, applies the global liquidity
pre-filter, runs both surviving algorithms, and returns ranked
DataFrames ready for display or export.

    Algorithm 1 — Minervini Leader Pullback (find_minervini_pullback)
    Algorithm 2 — Power Trend Pullback      (find_power_trend_pullback)
"""

import time
import logging

import pandas as pd
from tqdm import tqdm
from datetime import datetime, timedelta, timezone

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus

from .api import trading_client, data_client
from .config import (
    FETCH_DAYS, CHUNK_SIZE, RATE_LIMIT_SLEEP, TOP_N,
    MIN_PRICE, MIN_AVG_VOLUME_20D, MIN_AVG_DOLLAR_VOL_20D,
)
from .patterns import is_buyout_or_flatline
from .algorithms import find_minervini_pullback, find_power_trend_pullback


def run_master_scanner(target: int = TOP_N) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Full market scan across the two surviving strategies.

    Returns
    -------
    mpb_display : Minervini Leader Pullback candidates (Algorithm 1)
    ptp_display : Power Trend Pullback candidates      (Algorithm 2)
    """

    # 1. Universe
    print("Fetching active US equities from Alpaca...")
    req     = GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    assets  = trading_client.get_all_assets(req)
    symbols = [a.symbol for a in assets if a.tradable and a.fractionable and len(a.symbol) <= 4]
    print(f"Found {len(symbols)} tradable symbols.")

    # 2. Timeframe
    end   = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=FETCH_DAYS)

    mpb_results: list[dict] = []
    ptp_results: list[dict] = []

    # 3. Process in chunks
    print("Running Master Technical Screener (Algorithm 1 + Algorithm 2)...")

    for i in tqdm(range(0, len(symbols), CHUNK_SIZE)):
        chunk = symbols[i:i + CHUNK_SIZE]
        try:
            bars = data_client.get_stock_bars(
                StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start, end=end)
            ).df
            if bars.empty:
                continue

            bars.rename(
                columns={"close": "Close", "high": "High", "low": "Low", "volume": "Volume"},
                inplace=True,
            )

            for symbol in chunk:
                if symbol not in bars.index:
                    continue

                df = bars.loc[symbol].copy()
                if len(df) < 252:
                    continue

                price = df["Close"].iloc[-1]

                # Global liquidity filter
                avg_vol_20        = df["Volume"].tail(20).mean()
                avg_dollar_vol_20 = (df["Close"] * df["Volume"]).tail(20).mean()
                if price < MIN_PRICE or avg_vol_20 < MIN_AVG_VOLUME_20D or avg_dollar_vol_20 < MIN_AVG_DOLLAR_VOL_20D:
                    continue

                # Pulse check: reject acquisition pegs / flatlined charts
                if is_buyout_or_flatline(df):
                    continue

                # ── Algorithm 1: Minervini Leader Pullback ────────────────────
                mpb_signal = find_minervini_pullback(df)
                if mpb_signal is not None:
                    mpb_signal["Symbol"] = symbol
                    mpb_results.append(mpb_signal)

                # ── Algorithm 2: Power Trend Pullback (PTP) ───────────────────
                ptp_signal = find_power_trend_pullback(df)
                if ptp_signal is not None:
                    ptp_signal["Symbol"] = symbol
                    ptp_results.append(ptp_signal)

        except Exception as e:
            logging.error(f"Error processing chunk starting with {chunk[0]}: {e}")
            continue

        time.sleep(RATE_LIMIT_SLEEP)

    # 4. Rank Algorithm 1 (Minervini) — triggers first, then tightest setups
    if mpb_results:
        mpb_df = pd.DataFrame(mpb_results)
        mpb_display = mpb_df.drop(columns=["params"], errors="ignore")
        mpb_display["atr_dist_sum"] = (
            mpb_display["dist_sma20_atr"] + mpb_display["dist_vwap20_atr"]
        )
        mpb_display = (
            mpb_display
            .sort_values(["is_trigger", "atr_dist_sum"], ascending=[False, True])
            .head(target)
        )
    else:
        mpb_display = pd.DataFrame()

    # 5. Rank Algorithm 2 (PTP) — tightest risk first, strongest momentum tiebreaker
    if ptp_results:
        ptp_df = pd.DataFrame(ptp_results)
        ptp_display = (
            ptp_df
            .sort_values(["risk_pct", "ret_40d_pct"], ascending=[True, False])
            .head(target)
        )
    else:
        ptp_display = pd.DataFrame()

    return mpb_display, ptp_display
