import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import warnings
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)

# ================= API CREDENTIALS =================
API_KEY = "PKQ2XILCAMFJLKF4AM3JIJYXLY"
API_SECRET = "8QDtrLFfY25TKrWedBRkxyXbw4e8sWcpu5wA1PZ2X1y7"

data_client = StockHistoricalDataClient(API_KEY, API_SECRET)

def calculate_atr(df, period=14):
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(window=period).mean()

def generate_blueprint(symbol="VRT"):
    print(f"📡 Fetching tactical data for {symbol}...\n")
    
    # Get last 100 days of data (offset by 20 mins for free tier)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=150)
    
    request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end)
    bars = data_client.get_stock_bars(request).df
    
    if bars.empty:
        print(f"No data found for {symbol}.")
        return
        
    df = bars.loc[symbol].copy()
    df.rename(columns={'close': 'Close', 'high': 'High', 'low': 'Low', 'volume': 'Volume'}, inplace=True)
    
    # --- Tactical Calculations ---
    current_price = df['Close'].iloc[-1]
    
    # Moving Averages
    sma10 = df['Close'].rolling(10).mean().iloc[-1]
    sma20 = df['Close'].rolling(20).mean().iloc[-1]
    sma50 = df['Close'].rolling(50).mean().iloc[-1]
    
    # Volatility (ATR)
    df['ATR'] = calculate_atr(df, 14)
    current_atr = df['ATR'].iloc[-1]
    
    # Pivot (Resistance line of the current base - last 15 days)
    recent_high = df['High'].tail(15).max()
    dist_to_pivot = ((recent_high - current_price) / current_price) * 100
    
    # Volume Analysis
    vol_50d_avg = df['Volume'].tail(50).mean()
    vol_3d_avg = df['Volume'].tail(3).mean()
    vol_dry_ratio = vol_3d_avg / vol_50d_avg
    
    # Base Low (Support of the current contraction)
    base_low = df['Low'].tail(15).min()

    # --- Print the Dossier ---
    print("="*40)
    print(f"🎯 TACTICAL BLUEPRINT: {symbol}")
    print("="*40)
    print(f"Current Price:      ${current_price:.2f}")
    print(f"14-Day ATR:         ${current_atr:.2f} (Normal daily swing)")
    print("-" * 40)
    print("🚧 KEY LEVELS")
    print(f"Base Resistance:    ${recent_high:.2f} ({dist_to_pivot:.2f}% away)")
    print(f"Base Support (Low): ${base_low:.2f}")
    print(f"20-Day SMA:         ${sma20:.2f}")
    print(f"50-Day SMA:         ${sma50:.2f}")
    print("-" * 40)
    print("📊 VOLUME PROFILE")
    print(f"50-Day Avg Vol:     {int(vol_50d_avg):,}")
    print(f"Recent 3-Day Vol:   {int(vol_3d_avg):,}")
    print(f"Volume Dry-Up:      {vol_dry_ratio:.2f}x (Under 0.6 is ideal for VCP)")
    print("="*40)

if __name__ == "__main__":
    generate_blueprint("GEV")