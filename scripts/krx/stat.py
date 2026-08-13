import _paths
from _paths import SESSION_PATH
import requests
import json
import pandas as pd
from tabulate import tabulate

# 1. Show every single column
pd.set_option('display.max_columns', None)

# 2. Don't wrap the lines (keep one row per line)
pd.set_option('display.width', 1000)

# 3. Show the full content of each cell (no '...')
pd.set_option('display.max_colwidth', None)

# This adds the comma for thousands and keeps it clean
pd.options.display.float_format = '{:,.2f}'.format

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

def scrape_stock(ticker, isin, start, end):
    session = get_active_session()
    if not session: return
    
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
    response = session.post(url, data=payload, headers=headers)
    
    # Convert the entire 'OutBlock_1' list into a DataFrame
    df = pd.DataFrame(response.json()['OutBlock_1'])
    
    # --- 1. DYNAMIC RENAMING ---
    # We map the internal KRX IDs to our friendly names
    rename_map = {
        'RPT_DUTY_OCCR_DD': 'Date',
        'BAL_QTY': 'Short_Volume',
        'LIST_SHRS': 'Listed_Shares',
        'BAL_AMT': 'Short_Value',
        'MKTCAP': 'Market_Cap',
        'BAL_RTO': 'Ratio(%)'
    }
    
    # Only rename columns that actually exist in the response
    existing_map = {k: v for k, v in rename_map.items() if k in df.columns}
    df = df.rename(columns=existing_map)

    # --- 2. SAFE NUMERIC CONVERSION ---
    # We identify which columns are actually present before converting
    target_cols = ['Short_Volume', 'Listed_Shares', 'Short_Value', 'Market_Cap', 'Ratio(%)']
    found_cols = [c for c in target_cols if c in df.columns]

    for col in found_cols:
        # Convert to string, remove commas, then to numeric
        df[col] = pd.to_numeric(df[col].astype(str).str.replace(',', ''), errors='coerce')

    # --- 3. SAFE DISPLAY FORMATTING ---
    df_display = df.copy()
    
    # Large integers (Volume, Value, Cap)
    for col in ['Short_Volume', 'Listed_Shares', 'Short_Value', 'Market_Cap']:
        if col in df_display.columns:
            df_display[col] = df_display[col].map(lambda x: f"{x:,.0f}" if pd.notnull(x) else "-")
    
    # Percentages
    if 'Ratio(%)' in df_display.columns:
        df_display['Ratio(%)'] = df_display['Ratio(%)'].map(lambda x: f"{x:,.2f}%" if pd.notnull(x) else "-")

    # --- 4. PRINT ---
    print("\n" + "="*95)
    print(f" KRX DATA REPORT | {ticker} ")
    print("="*95)
    print(tabulate(df_display, headers='keys', tablefmt='psql', showindex=False))
    
    return df

# Now you can keep changing this file all you want!
result = scrape_stock("052690", "KR7052690005", "20260310", "20260407")