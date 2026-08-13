import _paths  # noqa: F401 — repo root on sys.path
from screener.config import API_KEY, API_SECRET
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

client = StockHistoricalDataClient(API_KEY, API_SECRET)


def calculate_poc(ticker, days_back=90, bucket_size=0.50):
    print(f"Fetching data to calculate exact POC for {ticker} over the last {days_back} days...")

    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days_back)

    try:
        bars = client.get_stock_bars(
            StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame.Hour,
                start=start_date,
                end=end_date,
            )
        ).df

        if bars.empty:
            print("No data found.")
            return

        df = bars.xs(ticker) if ticker in bars.index.get_level_values(0) else bars

        volume_profile = defaultdict(float)
        for _, row in df.iterrows():
            typical_price = (row["high"] + row["low"] + row["close"]) / 3
            bucket = round(typical_price / bucket_size) * bucket_size
            volume_profile[bucket] += row["volume"]

        poc_price = max(volume_profile, key=volume_profile.get)
        poc_volume = volume_profile[poc_price]
        sorted_nodes = sorted(volume_profile.items(), key=lambda item: item[1], reverse=True)

        print("\n--- VOLUME PROFILE RESULTS ---")
        print(f"Ticker: {ticker}")
        print(f"Timeframe: Last {days_back} Days")
        print(f"POINT OF CONTROL (POC): ${poc_price:.2f}")
        print(f"Volume at POC: {poc_volume:,.0f} shares")
        print("-" * 30)
        print("Other High-Volume Nodes (Support/Resistance):")
        for i in range(1, min(4, len(sorted_nodes))):
            print(f"Node {i}: ${sorted_nodes[i][0]:.2f} ({sorted_nodes[i][1]:,.0f} shares)")
        print("------------------------------")

    except Exception as e:
        print(f"API Error: {e}")


if __name__ == "__main__":
    calculate_poc("MOD", days_back=90, bucket_size=0.50)
