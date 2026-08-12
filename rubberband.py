import FinanceDataReader as fdr
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
from tqdm import tqdm
import logging

logging.basicConfig(filename='rubberband_errors.log', level=logging.WARNING)

def get_korean_universe():
    df = fdr.StockListing('KRX')
    ignore_pattern = r'우$|우[A-Z]$|스팩|제[0-9]+호|리츠'
    df = df[~df['Name'].str.contains(ignore_pattern, regex=True, na=False)]
    return df.sort_values('Marcap', ascending=False).reset_index(drop=True)

# ---------------- Indicators ----------------

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_bollinger_bands(series, period=20, std_dev=2):
    sma = series.rolling(window=period).mean()
    rolling_std = series.rolling(window=period).std()
    upper_band = sma + (rolling_std * std_dev)
    lower_band = sma - (rolling_std * std_dev)
    return lower_band

# ---------------- MAIN ----------------
def run(target=15):
    uni = get_korean_universe()
    end = datetime.today()
    start = end - timedelta(days=300) # Need enough days for 200 SMA

    results = []

    print("Scanning for Rubber Band (Mean Reversion) setups...")
    for _, row in tqdm(uni.iterrows(), total=len(uni)):
        try:
            df = fdr.DataReader(row['Code'], start, end)
            if len(df) < 200:
                continue

            price = df['Close'].iloc[-1]

            # 1. Liquidity Filter (Average 5 Billion KRW over last 20 days)
            if (df['Close'] * df['Volume']).tail(20).mean() < 5_000_000_000:
                continue

            # 2. Long-Term Trend Filter (Do not catch falling knives)
            # The stock MUST be above its 200-day moving average
            sma200 = df['Close'].rolling(200).mean().iloc[-1]
            if price < sma200:
                continue

            # 3. Short-Term Oversold Filter (RSI < 30)
            df['RSI'] = calculate_rsi(df['Close'], 14)
            current_rsi = df['RSI'].iloc[-1]
            if current_rsi > 30: # Only want severely oversold stocks
                continue

            # 4. Extreme Deviation Filter (Piercing Lower Bollinger Band)
            lower_band = calculate_bollinger_bands(df['Close'])
            current_lower_band = lower_band.iloc[-1]
            
            # Distance from lower band (Negative means it pierced below it)
            band_distance = ((price - current_lower_band) / current_lower_band) * 100
            
            if price > current_lower_band:
                continue # Must be touching or below the lower band

            results.append({
                "Symbol": row['Code'],
                "Name": row['Name'],
                "Price": price,
                "RSI": round(current_rsi, 2),
                "Band_Dist_%": round(band_distance, 2)
            })

            time.sleep(0.15) # Be polite to the API

        except:
            continue

    df = pd.DataFrame(results)
    
    if df.empty:
        return df
        
    # Sort by how deeply they pierced the Bollinger Band (most negative first)
    return df.sort_values("Band_Dist_%", ascending=True).head(target)

# RUN
if __name__ == "__main__":
    res = run()
    print("\n")
    print(res if not res.empty else "No severely oversold stocks found in uptrends today.")