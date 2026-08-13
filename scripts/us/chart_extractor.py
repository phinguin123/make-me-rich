import _paths  # noqa: F401 — repo root on sys.path
from screener.config import API_KEY, API_SECRET
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from datetime import datetime, timedelta, timezone
import json
import warnings

warnings.filterwarnings("ignore", category=UserWarning)

# ==========================================
# CONFIGURATION
# ==========================================
SYMBOL        = "PRIM"
TRADING_DAYS  = 5    # number of past trading days to include
MARKET_OPEN   = (13, 30)   # UTC — NYSE open
MARKET_CLOSE  = (20,  0)   # UTC — NYSE close


# ==========================================
# SETUP CLIENT
# ==========================================
client = StockHistoricalDataClient(API_KEY, API_SECRET)


def get_5min_data(symbol: str, trading_days: int = 5) -> dict:
    # Fetch enough calendar days to guarantee trading_days worth of data
    # (account for weekends + holidays: ~2× buffer is plenty)
    lookback_days = trading_days * 3
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    bars = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(15, TimeFrameUnit.Minute),
        start=start,
    ))
    df = bars.df.xs(symbol)

    # Keep last `trading_days` distinct trading dates
    utc_idx      = df.index.tz_convert("UTC")
    utc_dates    = utc_idx.normalize()
    last_n_dates = sorted(utc_dates.unique())[-trading_days:]
    df           = df[utc_dates.isin(last_n_dates)].copy()
    utc_idx      = df.index.tz_convert("UTC")

    # Regular market hours only (13:30–20:00 UTC)
    open_min  = MARKET_OPEN[0]  * 60 + MARKET_OPEN[1]
    close_min = MARKET_CLOSE[0] * 60 + MARKET_CLOSE[1]
    minutes   = utc_idx.hour * 60 + utc_idx.minute
    df        = df[(minutes >= open_min) & (minutes < close_min)]

    # One compact row per bar
    rows = []
    for idx, row in df.iterrows():
        t = idx.tz_convert("UTC")
        rows.append([
            t.strftime("%Y-%m-%d"),
            t.strftime("%H:%M"),
            round(float(row["open"]),  2),
            round(float(row["high"]),  2),
            round(float(row["low"]),   2),
            round(float(row["close"]), 2),
            int(row["volume"]),
        ])

    return {
        "symbol":  symbol,
        "schema":  "Each row: [date, time_utc, open, high, low, close, volume]. 15-min bars, regular hours (13:30-20:00 UTC).",
        "trading_days_covered": [str(d.date()) for d in last_n_dates],
        "bars":    rows,
    }


# ==========================================
# ENTRY POINT
# ==========================================
if __name__ == "__main__":
    print(f"Fetching 15-min data for {SYMBOL} ({TRADING_DAYS} trading days, regular hours only)...")
    try:
        data     = get_5min_data(SYMBOL, TRADING_DAYS)
        filename = f"US_stock_5m_chart_{SYMBOL}.json"
        with open(filename, "w") as f:
            # Write schema/meta with indent, bars as one line each
            f.write('{\n')
            f.write(f'  "symbol": {json.dumps(data["symbol"])},\n')
            f.write(f'  "schema": {json.dumps(data["schema"])},\n')
            f.write(f'  "trading_days_covered": {json.dumps(data["trading_days_covered"])},\n')
            f.write('  "bars": [\n')
            for i, row in enumerate(data["bars"]):
                comma = "," if i < len(data["bars"]) - 1 else ""
                f.write(f'    {json.dumps(row)}{comma}\n')
            f.write('  ]\n}')

        print("--- SUCCESS ---")
        print(f"File created      : {filename}")
        print(f"Trading days      : {data['trading_days_covered']}")
        print(f"Total 5-min bars  : {len(data['bars'])}")
    except Exception as e:
        print(f"FAILED: {e}")
