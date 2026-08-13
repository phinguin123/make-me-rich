import _paths
from _paths import OUTPUT_DIR, SESSION_PATH
import FinanceDataReader as fdr
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import requests
import json
import time
from tqdm import tqdm
import logging
import os

# --- DISPLAY SETTINGS ---
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 1000)
pd.options.display.float_format = '{:,.2f}'.format

logging.basicConfig(filename=str(OUTPUT_DIR / 'squeeze_radar_errors.log'), level=logging.WARNING)

# --- 1. SESSION MANAGEMENT ---
def get_active_session():
    """Loads the session cookies maintained by session_manager.py"""
    session = requests.Session()
    try:
        with SESSION_PATH.open("r") as f:
            cookies = json.load(f)
        for name, value in cookies.items():
            session.cookies.set(name, value, domain="data.krx.co.kr")
        return session
    except FileNotFoundError:
        print(f"Error: {SESSION_PATH} not found. Run session_manager.py first!")
        return None

# --- 2. THE KRX SBL SCRAPER ---
def scrape_short_balance(session, ticker, isin, start, end):
    """Hits the KRX API to get the Short Selling / SBL Balance"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0203",
        "X-Requested-With": "XMLHttpRequest"
    }
    
    payload = {
        "bld": "dbms/MDC/STAT/srt/MDCSTAT30502",
        "locale": "ko_KR",
        "searchType": "2",
        "mktTpCd": "1",
        "trdDd": end,
        "isuCd": isin,
        "isuCd2": ticker,
        "strtDd": start,
        "endDd": end,
        "share": "1",
        "money": "1",
        "csvxls_isNo": "false"
    }

    url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
    try:
        response = session.post(url, data=payload, headers=headers, timeout=5)
        data = response.json().get('OutBlock_1', [])
        
        if not data:
            return None
            
        df = pd.DataFrame(data)
        
        # We only need the most recent row's Short Ratio
        latest_data = df.iloc[0] 
        short_ratio = str(latest_data.get('BAL_RTO', '0')).replace(',', '')
        short_volume = str(latest_data.get('BAL_QTY', '0')).replace(',', '')
        
        return {
            'Short_Ratio(%)': float(short_ratio) if short_ratio else 0.0,
            'Short_Volume': int(short_volume) if short_volume else 0
        }
    except Exception as e:
        logging.warning(f"KRX SBL Scrape failed for {ticker}: {str(e)}")
        return None

# --- 3. UNIVERSE GENERATION ---
def get_korean_universe():
    print("Fetching KOSPI/KOSDAQ universe...")
    df = fdr.StockListing('KRX')
    ignore_pattern = r'우$|우[A-Z]$|스팩|제[0-9]+호|리츠'
    df = df[~df['Name'].str.contains(ignore_pattern, regex=True, na=False)]
    
    # Filter out penny stocks and extremely illiquid garbage to save API calls
    df = df[(df['Close'] > 1000) & (df['Marcap'] > 50_000_000_000)]
    return df.reset_index(drop=True)

# --- 4. THE RADAR LOGIC ---
def run_sbl_squeeze_radar(output_file=None):
    if output_file is None:
        output_file = str(OUTPUT_DIR / "sbl_watch.csv")
    universe = get_korean_universe()
    session = get_active_session()
    
    if not session:
        return
        
    end_date_dt = datetime.today()
    start_date_dt = end_date_dt - timedelta(days=60)
    
    end_str = end_date_dt.strftime("%Y%M%d")
    start_str = start_date_dt.strftime("%Y%M%d")
    
    print(f"\nPhase 1: Pre-filtering {len(universe)} stocks for Squeeze Setups (FDR)...")
    
    potential_traps = []
    
    # PHASE 1: FDR PRE-FILTER (Fast)
    for idx, row in tqdm(universe.iterrows(), total=len(universe)):
        symbol = row['Code']
        name = row['Name']
        isin = row.get('ISU_CD', f"KR7{symbol}000") # Approximation if ISIN isn't in FDR
        
        try:
            df = fdr.DataReader(symbol, start_date_dt, end_date_dt)
            if len(df) < 40: continue
            
            last_close = df['Close'].iloc[-1]
            sma20 = df['Close'].rolling(20).mean().iloc[-1]
            
            # Condition 1: The stock is beaten down or trapped below the 20MA
            # (Shorts are comfortable and holding their positions)
            if last_close > sma20 * 1.05: 
                continue
                
            # Condition 2: Sudden anomaly in volume today or yesterday
            # (Something is waking the stock up, potentially spooking the shorts)
            vol_5d_avg = df['Volume'].tail(5).mean()
            vol_20d_avg = df['Volume'].tail(20).mean()
            
            if vol_5d_avg < vol_20d_avg * 1.5:
                continue
                
            potential_traps.append({
                'Symbol': symbol,
                'Name': name,
                'ISIN': isin,
                'Close': last_close,
                '20_MA': round(sma20, 0)
            })
            
        except Exception:
            continue

    print(f"\nPhase 1 Complete. Found {len(potential_traps)} potential traps.")
    if not potential_traps:
        print("No setups found. Market is not showing squeeze mechanics.")
        return

    print("\nPhase 2: Deep Scanning KRX for Short Seller Balances...")
    
    sbl_targets = []
    
    # PHASE 2: KRX DEEP SCAN (Slow)
    for trap in tqdm(potential_traps):
        # Be kind to the KRX API
        time.sleep(0.3)
        
        krx_data = scrape_short_balance(session, trap['Symbol'], trap['ISIN'], start_str, end_str)
        
        if not krx_data:
            continue
            
        short_ratio = krx_data['Short_Ratio(%)']
        
        # THE KILL ZONE: High Short Ratio (Adjust this threshold based on KOSDAQ/KOSPI norms)
        # In Korea, anything over 2.5% to 3% is highly elevated and vulnerable to a squeeze.
        if short_ratio >= 2.5:
            trap.update(krx_data)
            sbl_targets.append(trap)
            
            # Save incrementally
            pd.DataFrame([trap]).to_csv(output_file, mode='a', header=not os.path.exists(output_file), index=False)

    results_df = pd.DataFrame(sbl_targets)
    
    print("\n\n" + "="*80)
    print(" 🎯 SBL SHORT SQUEEZE WATCHLIST (Intel Only)")
    print("="*80)
    
    if results_df.empty:
        print("Zero stocks met the extreme SBL criteria today.")
    else:
        # Sort by the highest short ratio first
        results_df = results_df.sort_values(by='Short_Ratio(%)', ascending=False)
        print(results_df.to_string(index=False))
        print(f"\nSaved {len(results_df)} targets to {output_file} for data collection.")

if __name__ == "__main__":
    # Clear old data
    if os.path.exists('sbl_watch.csv'):
        os.remove('sbl_watch.csv')
        
    run_sbl_squeeze_radar()