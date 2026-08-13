# import FinanceDataReader as fdr
# import pandas as pd
# import numpy as np
# from datetime import datetime, timedelta
# import time
# from tqdm import tqdm

# def get_korean_universe():
#     print("Fetching KOSPI/KOSDAQ universe...")
#     df = fdr.StockListing('KRX')
#     ignore_pattern = r'우$|우[A-Z]$|스팩|제[0-9]+호|리츠'
#     df = df[~df['Name'].str.contains(ignore_pattern, regex=True, na=False)]
#     df = df.sort_values('Marcap', ascending=False).reset_index(drop=True)
#     return df

# def get_point_of_control(df, lookback_days=120, bins=50):
#     """
#     Calculates the Volume Profile over a specific lookback period 
#     and returns the Point of Control (POC) price.
#     """
#     recent_df = df.tail(lookback_days).copy()
    
#     # We use 'Typical Price' instead of just Close for a more accurate volume distribution
#     recent_df['Typical_Price'] = (recent_df['High'] + recent_df['Low'] + recent_df['Close']) / 3
    
#     # Group the prices into 50 distinct buckets
#     recent_df['Price_Bucket'] = pd.cut(recent_df['Typical_Price'], bins=bins)
    
#     # Sum the volume inside each bucket
#     volume_profile = recent_df.groupby('Price_Bucket', observed=False)['Volume'].sum()
    
#     # Find the bucket with the maximum volume (The Anvil)
#     poc_bucket = volume_profile.idxmax()
    
#     # Return the exact middle price of that maximum volume bucket
#     return round(poc_bucket.mid, 0)

# def run_vcp_v3_screen(target_count=5):
#     universe = get_korean_universe()
#     print(f"Total eligible stocks: {len(universe)}")
    
#     end_date = datetime.today()
#     start_date = end_date - timedelta(days=400)
    
#     captains = []
    
#     print("\nScanning for True VCPs & Institutional Anvils...")
#     for idx, row in tqdm(universe.iterrows(), total=len(universe)):
#         symbol = row['Code']
#         name = row['Name']
#         marcap = row['Marcap']
        
#         try:
#             df = fdr.DataReader(symbol, start_date, end_date)
#             if len(df) < 200:
#                 continue
            
#             last_close = df['Close'].iloc[-1]
#             last_vol = df['Volume'].iloc[-1]
            
#             sma20 = df['Close'].rolling(window=20).mean().iloc[-1]
#             sma50 = df['Close'].rolling(window=50).mean().iloc[-1]
#             sma200 = df['Close'].rolling(window=200).mean().iloc[-1]
            
#             # 1. The Floor
#             if not (last_close > sma20 and sma20 > sma50 and sma50 > sma200):
#                 continue
                
#             # 2. Ceiling Proximity
#             high_52w = df['High'].tail(252).max()
#             if last_close < 0.85 * high_52w:
#                 continue
                
#             # 3. The 4-Week Contraction
#             recent_5_days = df.tail(5)
#             prior_15_days = df.shift(5).tail(15)
            
#             recent_range = recent_5_days['High'].max() - recent_5_days['Low'].min()
#             prior_range = prior_15_days['High'].max() - prior_15_days['Low'].min()
            
#             if recent_range > (prior_range * 0.60):
#                 continue
                
#             # 4. Extreme Volume Drought
#             vol_3d_avg = df['Volume'].tail(3).mean()
#             vol_50d_avg = df['Volume'].tail(50).mean()
            
#             if vol_3d_avg > (vol_50d_avg * 0.50):
#                 continue
                
#             # --- MARK III UPGRADE: Calculate Point of Control ---
#             # We only spend compute power calculating POC for stocks that pass the strict VCP filters
#             poc_price = get_point_of_control(df, lookback_days=120, bins=50)
            
#             # Calculate how far the current price is from the institutional floor
#             poc_distance_pct = round(((last_close - poc_price) / poc_price) * 100, 2)
            
#             captains.append({
#                 'Symbol': symbol,
#                 'Name': name,
#                 'Close': last_close,
#                 'POC_Anvil': poc_price,
#                 'Dist_from_Anvil(%)': poc_distance_pct,
#                 '20_MA': round(sma20, 0)
#             })
            
#             if len(captains) >= target_count:
#                 break
                
#             time.sleep(0.1)
            
#         except Exception as e:
#             continue
            
#     return pd.DataFrame(captains)

# if __name__ == "__main__":
#     results = run_vcp_v3_screen(target_count=5)
    
#     print("\n\n=== Mark III VCP Targets with Volume Profile ===")
#     if results.empty:
#         print("Zero stocks met the criteria today.")
#     else:
#         print(results.to_string(index=False))

import FinanceDataReader as fdr
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
from tqdm import tqdm
import logging
import os

# Set up logging to catch FDR API errors without stopping the scanner
logging.basicConfig(
    filename='scanner_errors.log', 
    level=logging.WARNING, 
    format='%(asctime)s - %(message)s'
)

def get_korean_universe():
    print("Fetching KOSPI/KOSDAQ universe...")
    df = fdr.StockListing('KRX')
    ignore_pattern = r'우$|우[A-Z]$|스팩|제[0-9]+호|리츠'
    df = df[~df['Name'].str.contains(ignore_pattern, regex=True, na=False)]
    df = df.sort_values('Marcap', ascending=False).reset_index(drop=True)
    return df

def get_dynamic_point_of_control(df, lookback_days=120):
    """
    Calculates the Volume Profile using Dynamic Binning.
    Adjusts the bucket size based on the stock's actual historical volatility.
    """
    recent_df = df.tail(lookback_days).copy()
    recent_df['Typical_Price'] = (recent_df['High'] + recent_df['Low'] + recent_df['Close']) / 3
    
    max_price = recent_df['Typical_Price'].max()
    min_price = recent_df['Typical_Price'].min()
    
    # Safety catch for flatlined stocks
    if max_price == min_price:
        return round(max_price, 0)
        
    # Dynamic Binning: Calculate bins based on price range percentage
    # Bounded between 20 (low vol) and 100 (high vol) buckets for maximum precision
    price_range_pct = (max_price - min_price) / min_price
    calculated_bins = int(price_range_pct * 100) 
    optimal_bins = max(20, min(calculated_bins, 100))
    
    recent_df['Price_Bucket'] = pd.cut(recent_df['Typical_Price'], bins=optimal_bins)
    volume_profile = recent_df.groupby('Price_Bucket', observed=False)['Volume'].sum()
    
    poc_bucket = volume_profile.idxmax()
    return round(poc_bucket.mid, 0)

def run_vcp_v3_screen(target_count=5, output_file='vcp_targets.csv'):
    universe = get_korean_universe()
    print(f"Total eligible stocks: {len(universe)}")
    
    end_date = datetime.today()
    start_date = end_date - timedelta(days=400)
    
    captains = []
    
    # Clear previous run's file if it exists
    if os.path.exists(output_file):
        os.remove(output_file)
        
    print("\nScanning for True VCPs & Institutional Anvils (Accuracy Prioritized)...")
    
    for idx, row in tqdm(universe.iterrows(), total=len(universe)):
        symbol = row['Code']
        name = row['Name']
        
        try:
            df = fdr.DataReader(symbol, start_date, end_date)
            if len(df) < 200:
                continue
            
            last_close = df['Close'].iloc[-1]
            
            sma20 = df['Close'].rolling(window=20).mean().iloc[-1]
            sma50 = df['Close'].rolling(window=50).mean().iloc[-1]
            sma200 = df['Close'].rolling(window=200).mean().iloc[-1]
            
            # 1. The Floor (Trend Alignment)
            if not (last_close > sma20 and sma20 > sma50 and sma50 > sma200):
                continue
                
            # 2. Ceiling Proximity (Within 15% of 52-week high)
            high_52w = df['High'].tail(252).max()
            if last_close < 0.85 * high_52w:
                continue
                
            # 3. The 4-Week Contraction (Volatility compression)
            recent_5_days = df.tail(5)
            prior_15_days = df.shift(5).tail(15)
            
            recent_range = recent_5_days['High'].max() - recent_5_days['Low'].min()
            prior_range = prior_15_days['High'].max() - prior_15_days['Low'].min()
            
            if recent_range > (prior_range * 0.60):
                continue
                
            # 4. Extreme Volume Drought (Supply has dried up)
            vol_3d_avg = df['Volume'].tail(3).mean()
            vol_50d_avg = df['Volume'].tail(50).mean()
            
            if vol_3d_avg > (vol_50d_avg * 0.50):
                continue

            # --- NEW: 5. The Liquidity Floor (50 Billion KRW minimum) ---
            # Calculate daily trading value (Close Price * Volume)
            df['Trading_Value'] = df['Close'] * df['Volume']
            
            # Calculate the 20-day average of the trading value
            avg_trading_val_20d = df['Trading_Value'].tail(20).mean()
            
            # 50,000,000,000 KRW = 500억 원
            if avg_trading_val_20d < 50_000_000_000: 
                continue
                
            # --- THE ANVIL: Dynamic Point of Control ---
            poc_price = get_dynamic_point_of_control(df, lookback_days=120)
            poc_distance_pct = round(((last_close - poc_price) / poc_price) * 100, 2)
            
            # --- NEW: 6. The Anvil Tether (Max 15% Stretch) ---
            # If price is below the POC (negative) or stretched too far (>15%), discard it.
            if poc_distance_pct < 0 or poc_distance_pct > 15.0:
                continue

            target_data = {
                'Symbol': symbol,
                'Name': name,
                'Close': last_close,
                'POC_Anvil': poc_price,
                'Dist_from_Anvil(%)': poc_distance_pct,
                '20_MA': round(sma20, 0)
            }
            
            captains.append(target_data)
            
            # Save incrementally so you don't lose data if you manually stop the script
            pd.DataFrame([target_data]).to_csv(output_file, mode='a', header=not os.path.exists(output_file), index=False)
            
            if len(captains) >= target_count:
                break
                
            # Be kind to the FDR API since we aren't rushing
            time.sleep(0.2)
            
        except Exception as e:
            # Log the error quietly and keep marching
            logging.warning(f"Failed on {symbol} ({name}): {str(e)}")
            continue
            
    return pd.DataFrame(captains)

if __name__ == "__main__":
    results = run_vcp_v3_screen(target_count=7)
    
    print("\n\n=== Mark III VCP Targets with Dynamic Volume Profile ===")
    if results.empty:
        print("Zero stocks met the criteria today. The market is not ready.")
    else:
        print(results.to_string(index=False))
        print("\nTargets successfully saved to vcp_targets.csv")