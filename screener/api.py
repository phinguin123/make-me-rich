"""
api.py — Alpaca client singletons.

Importing this module initialises both clients exactly once.
Every other module imports from here so credentials live only in config.py.
"""

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient

from .config import API_KEY, API_SECRET

if not API_KEY or not API_SECRET:
    raise RuntimeError(
        "Missing ALPACA_API_KEY / ALPACA_API_SECRET. Copy .env.example to .env."
    )

trading_client = TradingClient(API_KEY, API_SECRET, paper=True)
data_client    = StockHistoricalDataClient(API_KEY, API_SECRET)
