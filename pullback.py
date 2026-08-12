import FinanceDataReader as fdr
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
from tqdm import tqdm
import logging

logging.basicConfig(filename='pullback_errors.log', level=logging.WARNING)

def get_korean_universe():
    df = fdr.StockListing('KRX')
    ignore_pattern = r'우$|우[A-Z]$|스팩|제[0-9]+호|리츠'
    df = df[~df['Name'].str.contains(ignore_pattern, regex=True, na=False)]
    return df.sort_values('Marcap', ascending=False).reset_index(drop=True)

# ---------------- MAIN ----------------
def run(target=15):
    uni = get_korean_universe()
    end = datetime.today()
    start = end - timedelta(days=200) # Only need 200 days for this setup

    results = []

    print("Scanning for Post-Breakout Pullbacks (20-SMA Touch)...")
    for _, row in tqdm(uni.iterrows(), total=len(uni)):
        try:
            df = fdr.DataReader(row['Code'], start, end)
            if len(df) < 60:
                continue

            price = df['Close'].iloc[-1]

            # 1. Liquidity Filter (Average 5 Billion KRW over last 20 days)
            if (df['Close'] * df['Volume']).tail(20).mean() < 5_000_000_000:
                continue

            # 2. The Surge Filter (Must have had a 25%+ run recently)
            recent_high = df['High'].tail(20).max()
            recent_low = df['Low'].tail(40).min() # Look back a bit further for the base
            
            if recent_high < (recent_low * 1.25):
                continue # Not enough momentum to care about

            # 3. The Pullback Filter (Must be off its highs by at least 5%)
            if price > (recent_high * 0.95):
                continue # Still too close to the top, hasn't pulled back yet

            # 4. The Moving Average Touch Filter (Near the 20-day SMA)
            df['SMA20'] = df['Close'].rolling(20).mean()
            sma20 = df['SMA20'].iloc[-1]
            
            # Distance to 20 SMA (Must be within +/- 3% of the line)
            dist_to_sma = abs(price - sma20) / sma20
            if dist_to_sma > 0.03:
                continue

            # 5. Volume Contraction (Selling pressure must be dead)
            vol_3d_avg = df['Volume'].tail(3).mean()
            vol_20d_avg = df['Volume'].tail(20).mean()
            
            if vol_3d_avg > (vol_20d_avg * 0.6):
                continue # Volume is still too high, might break down further

            # Score: We want stocks that had the biggest run (High/Low ratio) 
            # but are now sitting perfectly quietly on the SMA.
            surge_strength = (recent_high - recent_low) / recent_low * 100

            results.append({
                "Symbol": row['Code'],
                "Name": row['Name'],
                "Price": price,
                "Surge_%": round(surge_strength, 2),
                "Dist_to_SMA20_%": round(dist_to_sma * 100, 2),
                "Vol_Dry_Ratio": round(vol_3d_avg / vol_20d_avg, 2)
            })

            time.sleep(0.15) # Be polite to the API

        except:
            continue

    df = pd.DataFrame(results)
    
    if df.empty:
        return df
        
    # Sort by the ones that had the most explosive initial surges
    return df.sort_values("Surge_%", ascending=False).head(target)

# RUN
if __name__ == "__main__":
    res = run()
    print("\n")
    print(res if not res.empty else "No clean pullbacks found today.")