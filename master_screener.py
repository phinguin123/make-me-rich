import FinanceDataReader as fdr
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
from tqdm import tqdm
import logging
import warnings

# Suppress pandas fragmentation warnings for cleaner output
warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)
logging.basicConfig(filename='master_scanner_errors.log', level=logging.WARNING)

def get_korean_universe():
    df = fdr.StockListing('KRX')
    ignore_pattern = r'우$|우[A-Z]$|스팩|제[0-9]+호|리츠'
    df = df[~df['Name'].str.contains(ignore_pattern, regex=True, na=False)]
    return df.sort_values('Marcap', ascending=False).reset_index(drop=True)

# ================= INDICATORS & HELPERS =================

def calculate_rs(stock_df, index_df, period=60):
    try:
        return (stock_df['Close'].iloc[-1] / stock_df['Close'].iloc[-period]) / \
               (index_df['Close'].iloc[-1] / index_df['Close'].iloc[-period])
    except: return None

def find_swings(df, w=5):
    highs, lows = [], []
    for i in range(w, len(df)-w):
        if df['High'].iloc[i] == max(df['High'].iloc[i-w:i+w]): highs.append((i, df['High'].iloc[i]))
        if df['Low'].iloc[i] == min(df['Low'].iloc[i-w:i+w]): lows.append((i, df['Low'].iloc[i]))
    return highs, lows

def get_contractions(highs, lows):
    swings = sorted(highs + lows, key=lambda x: x[0])
    return [(swings[i-1][1] - swings[i][1]) / swings[i-1][1] * 100
            for i in range(1, len(swings)) if swings[i-1][1] > swings[i][1]]

def valid_vcp(c): return len(c) >= 2 and all(c[i] < c[i-1] for i in range(1, len(c[-3:])))
def volume_dry(df): return df['Volume'].tail(5).mean() / df['Volume'].tail(50).mean()
def breakout_volume(df): return df['Volume'].iloc[-1] / df['Volume'].tail(50).mean()
def pivot_distance(df): pivot = df['High'].tail(30).max(); return (pivot - df['Close'].iloc[-1]) / pivot

def get_poc(df):
    d = df.tail(120).copy()
    tp = (d['High'] + d['Low'] + d['Close']) / 3
    # Prevent divide by zero if tp.min() is 0
    min_tp = tp.min() if tp.min() > 0 else 1 
    bins = max(20, min(int((tp.max()-min_tp)/min_tp*100), 100))
    d['b'] = pd.cut(tp, bins=bins)
    # FIX: Added observed=False to silence the pandas warning
    return d.groupby('b', observed=False)['Volume'].sum().idxmax().mid

def vcp_score(rs, contractions, vol_dry_ratio, breakout_ratio, pivot_dist, poc_dist):
    s = min(rs * 20, 30)
    if len(contractions) >= 2: s += 20
    s += max(0, (1 - vol_dry_ratio) * 15)
    s += min(breakout_ratio * 10, 20)
    s += max(0, (1 - pivot_dist) * 10)
    if 0 <= poc_dist <= 20: s += 5
    return round(s, 2)

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_bollinger_bands(series, period=20, std_dev=2):
    sma = series.rolling(window=period).mean()
    rolling_std = series.rolling(window=period).std()
    return sma - (rolling_std * std_dev)

# ================= MASTER ENGINE =================

def run_master_scanner(target=15):
    uni = get_korean_universe()
    end = datetime.today()
    start = end - timedelta(days=400)

    kospi = fdr.DataReader('KS11', start, end)
    kosdaq = fdr.DataReader('KQ11', start, end)

    vcp_results, rb_results, pb_results = [], [], []

    print("Fetching data and running Master Technical Screener...")
    for _, row in tqdm(uni.iterrows(), total=len(uni)):
        try:
            df = fdr.DataReader(row['Code'], start, end)
            if len(df) < 200: continue

            price = df['Close'].iloc[-1]

            if (df['Close'] * df['Volume']).tail(20).mean() < 5_000_000_000:
                continue

            sma20 = df['Close'].rolling(20).mean().iloc[-1]
            sma50 = df['Close'].rolling(50).mean().iloc[-1]
            sma200 = df['Close'].rolling(200).mean().iloc[-1]

            # ================= ALGORITHM 1: VCP =================
            if price > sma20 > sma50 > sma200 and price >= df['High'].tail(252).max() * 0.8:
                idx = kosdaq if row['Market']=='KOSDAQ' else kospi
                rs = calculate_rs(df, idx)
                
                if rs is not None and rs >= 1.2:
                    h,l = find_swings(df)
                    c = get_contractions(h,l)
                    vol_dry_ratio = volume_dry(df)
                    pivot_dist = pivot_distance(df)
                    
                    if valid_vcp(c) and vol_dry_ratio <= 0.6 and pivot_dist <= 0.07:
                        poc = get_poc(df)
                        poc_dist = (price - poc)/poc*100
                        if 0 <= poc_dist <= 30:
                            b_ratio = breakout_volume(df)
                            vcp_results.append({
                                "Symbol": row['Code'], "Name": row['Name'], 
                                "Score": vcp_score(rs, c, vol_dry_ratio, b_ratio, pivot_dist, poc_dist),
                                "RS": round(rs,2)
                            })

            # ================= ALGORITHM 2: RUBBER BAND =================
            if price > sma200: 
                df['RSI'] = calculate_rsi(df['Close'], 14)
                current_rsi = df['RSI'].iloc[-1]
                
                if current_rsi <= 35: # Relaxed to 35 from 30
                    current_lower_band = calculate_bollinger_bands(df['Close']).iloc[-1]
                    if price <= current_lower_band:
                        band_distance = ((price - current_lower_band) / current_lower_band) * 100
                        rb_results.append({
                            "Symbol": row['Code'], "Name": row['Name'], 
                            "RSI": round(current_rsi, 2), "Band_Dist_%": round(band_distance, 2)
                        })

            # ================= ALGORITHM 3: PULLBACKS =================
            recent_high = df['High'].tail(20).max()
            recent_low = df['Low'].tail(40).min()
            
            # FIX: Prevent division by zero
            if recent_low <= 0:
                continue
            
            if recent_high >= (recent_low * 1.25) and price <= (recent_high * 0.95):
                dist_to_sma = abs(price - sma20) / sma20
                if dist_to_sma <= 0.03:
                    vol_3d_avg = df['Volume'].tail(3).mean()
                    vol_20d_avg = df['Volume'].tail(20).mean()
                    if vol_3d_avg <= (vol_20d_avg * 0.6):
                        pb_results.append({
                            "Symbol": row['Code'], "Name": row['Name'],
                            "Surge_%": round((recent_high - recent_low) / recent_low * 100, 2),
                            "Dist_SMA20_%": round(dist_to_sma * 100, 2)
                        })

            time.sleep(0.1)

        except:
            continue

    vcp_df = pd.DataFrame(vcp_results).sort_values("Score", ascending=False).head(target) if vcp_results else pd.DataFrame()
    rb_df = pd.DataFrame(rb_results).sort_values("Band_Dist_%", ascending=True).head(target) if rb_results else pd.DataFrame()
    pb_df = pd.DataFrame(pb_results).sort_values("Surge_%", ascending=False).head(target) if pb_results else pd.DataFrame()

    return vcp_df, rb_df, pb_df

if __name__ == "__main__":
    vcp, rb, pb = run_master_scanner()
    
    print("\n" + "="*50)
    print("🏆 ALGORITHM 1: VCP (MOMENTUM BREAKOUTS)")
    print("="*50)
    print(vcp if not vcp.empty else "No VCP setups found today.")

    print("\n" + "="*50)
    print("📉 ALGORITHM 2: RUBBER BAND (OVERSOLD DIPS)")
    print("="*50)
    print(rb if not rb.empty else "No Rubber Band setups found today.")

    print("\n" + "="*50)
    print("🧘 ALGORITHM 3: PULLBACKS (RESTING ON 20-SMA)")
    print("="*50)
    print(pb if not pb.empty else "No Pullback setups found today.")
    print("\n")