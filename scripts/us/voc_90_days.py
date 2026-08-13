import _paths  # noqa: F401 — repo root on sys.path
from screener.config import API_KEY, API_SECRET
import alpaca_trade_api as tradeapi
from datetime import datetime, timedelta
from collections import defaultdict
import pandas as pd

BASE_URL = 'https://paper-api.alpaca.markets' # Or live URL

api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')

def calculate_poc(ticker, days_back=90, bucket_size=0.50):
    print(f"Fetching data to calculate exact POC for {ticker} over the last {days_back} days...")
    
    # Calculate timeframe
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    # Format for Alpaca API (RFC-3339)
    start_str = start_date.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_str = end_date.strftime('%Y-%m-%dT%H:%M:%SZ')
    
    try:
        # --- 2. FETCH HISTORICAL BARS ---
        # Using 1Hour bars for a 90-day period is a good balance of accuracy and API limits. 
        # (You can change to '1Min' if you want ultra-precision, but it will take longer to download).
        bars = api.get_bars(ticker, tradeapi.TimeFrame.Hour, start=start_str, end=end_str, feed='iex').df

        if bars.empty:
            print("No data found.")
            return

        # --- 3. BUILD THE VOLUME PROFILE ---
        volume_profile = defaultdict(float)
        
        for index, row in bars.iterrows():
            # Use the Typical Price of the bar ((High + Low + Close) / 3)
            typical_price = (row['high'] + row['low'] + row['close']) / 3
            
            # Round the price into a "bucket" (e.g., nearest $0.50)
            bucket = round(typical_price / bucket_size) * bucket_size
            
            # Add the volume to that bucket
            volume_profile[bucket] += row['volume']
            
        # --- 4. FIND THE POINT OF CONTROL (POC) ---
        # Find the price bucket with the absolute maximum volume
        poc_price = max(volume_profile, key=volume_profile.get)
        poc_volume = volume_profile[poc_price]
        
        # Calculate Value Area (approximate top 3 volume nodes for context)
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

# --- RUN THE CALCULATION ---
# Let's find the exact POC for Vertiv (VRT) with 50-cent precision
calculate_poc('MOD', days_back=90, bucket_size=0.50)