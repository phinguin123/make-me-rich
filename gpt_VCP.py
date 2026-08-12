import FinanceDataReader as fdr
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time
from tqdm import tqdm
import logging
import os

logging.basicConfig(filename='scanner_errors.log', level=logging.WARNING)

def get_korean_universe():
    df = fdr.StockListing('KRX')
    ignore_pattern = r'우$|우[A-Z]$|스팩|제[0-9]+호|리츠'
    df = df[~df['Name'].str.contains(ignore_pattern, regex=True, na=False)]
    return df.sort_values('Marcap', ascending=False).reset_index(drop=True)

# ---------------- RS ----------------
def calculate_rs(stock_df, index_df, period=60):
    try:
        return (stock_df['Close'].iloc[-1] / stock_df['Close'].iloc[-period]) / \
               (index_df['Close'].iloc[-1] / index_df['Close'].iloc[-period])
    except:
        return None

# ---------------- Swings ----------------
def find_swings(df, w=5):
    highs, lows = [], []
    for i in range(w, len(df)-w):
        if df['High'].iloc[i] == max(df['High'].iloc[i-w:i+w]):
            highs.append((i, df['High'].iloc[i]))
        if df['Low'].iloc[i] == min(df['Low'].iloc[i-w:i+w]):
            lows.append((i, df['Low'].iloc[i]))
    return highs, lows

def get_contractions(highs, lows):
    swings = sorted(highs + lows, key=lambda x: x[0])
    return [(swings[i-1][1] - swings[i][1]) / swings[i-1][1] * 100
            for i in range(1, len(swings)) if swings[i-1][1] > swings[i][1]]

def valid_vcp(c):
    # RELAXED: Now requires at least 2 contractions instead of 3
    return len(c) >= 2 and all(c[i] < c[i-1] for i in range(1, len(c[-3:])))

# ---------------- Volume ----------------
def volume_dry(df):
    return df['Volume'].tail(5).mean() / df['Volume'].tail(50).mean()

def breakout_volume(df):
    avg = df['Volume'].tail(50).mean()
    today = df['Volume'].iloc[-1]
    return today / avg

# ---------------- Pivot ----------------
def pivot_distance(df):
    pivot = df['High'].tail(30).max()
    return (pivot - df['Close'].iloc[-1]) / pivot

# ---------------- POC ----------------
def get_poc(df):
    d = df.tail(120).copy()
    tp = (d['High'] + d['Low'] + d['Close']) / 3
    bins = max(20, min(int((tp.max()-tp.min())/tp.min()*100), 100))
    d['b'] = pd.cut(tp, bins=bins)
    return d.groupby('b')['Volume'].sum().idxmax().mid

# ---------------- Score ----------------
def score(rs, contractions, vol_dry_ratio, breakout_ratio, pivot_dist, poc_dist):
    s = 0
    s += min(rs * 20, 30)
    # RELAXED: Rewards 2 or more contractions
    if len(contractions) >= 2:
        s += 20
    s += max(0, (1 - vol_dry_ratio) * 15)
    s += min(breakout_ratio * 10, 20)
    s += max(0, (1 - pivot_dist) * 10)
    if 0 <= poc_dist <= 20:
        s += 5
    return round(s, 2)

# ---------------- MAIN ----------------
def run(target=10):
    uni = get_korean_universe()
    end = datetime.today()
    start = end - timedelta(days=400)

    kospi = fdr.DataReader('KS11', start, end)
    kosdaq = fdr.DataReader('KQ11', start, end)

    results = []

    for _, row in tqdm(uni.iterrows(), total=len(uni)):
        try:
            df = fdr.DataReader(row['Code'], start, end)
            if len(df) < 200:
                continue

            # Trend
            sma20 = df['Close'].rolling(20).mean().iloc[-1]
            sma50 = df['Close'].rolling(50).mean().iloc[-1]
            sma200 = df['Close'].rolling(200).mean().iloc[-1]
            price = df['Close'].iloc[-1]

            if not (price > sma20 > sma50 > sma200):
                continue

            # Minervini condition: Price within 20% of 52-week high
            if price < df['High'].tail(252).max() * 0.8:
                continue

            # RS
            idx = kosdaq if row['Market']=='KOSDAQ' else kospi
            rs = calculate_rs(df, idx)
            if rs is None or rs < 1.2:
                continue

            # VCP
            h,l = find_swings(df)
            c = get_contractions(h,l)
            if not valid_vcp(c):
                continue

            vol_dry_ratio = volume_dry(df)
            if vol_dry_ratio > 0.6:
                continue

            pivot_dist = pivot_distance(df)
            if pivot_dist > 0.07:
                continue

            breakout_ratio = breakout_volume(df)

            # Liquidity: Reduced to 5 Billion KRW (~3.6M USD) to include mid/small caps
            if (df['Close']*df['Volume']).tail(20).mean() < 5_000_000_000:
                continue

            # POC
            poc = get_poc(df)
            poc_dist = (price - poc)/poc*100
            if poc_dist < 0 or poc_dist > 30:
                continue

            final_score = score(rs, c, vol_dry_ratio, breakout_ratio, pivot_dist, poc_dist)

            results.append({
                "Symbol": row['Code'],
                "Name": row['Name'],
                "RS": round(rs,2),
                "Score": final_score,
                "BreakoutVol": round(breakout_ratio,2),
                "VolDry": round(vol_dry_ratio,2)
            })

            time.sleep(0.15)

        except:
            continue

    df = pd.DataFrame(results)
    
    # FIX: Check if DataFrame is empty before trying to sort it
    if df.empty:
        return df
        
    return df.sort_values("Score", ascending=False).head(target)

# RUN
if __name__ == "__main__":
    res = run()
    print("\n") # Add a little breathing room after the progress bar
    print(res if not res.empty else "No setups found today. Market might be choppy!")