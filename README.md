# make-me-rich

Korean and US equity tools: a fully automatic Toss US-stock day trader, a live Kiwoom
dashboard, a VCP/momentum scanner, and Alpaca pullback screeners.

## Toss Auto Trader (`toss_dashboard/`)

An automatic US-stock trader for a Toss Securities account, with a live dashboard. Full
details, research and market read: [toss_dashboard/README.md](toss_dashboard/README.md).

| Sleeve | Capital | Holding | Backtest (all Toss costs) |
|---|---|---|---|
| **Swing: monthly momentum + volatility targeting** | $3,500 | weeks to months | 2019 – Oct 2026: **+24.5%/yr**, max DD −20%, positive every year incl. 2022 (+15%); SPY did +17.4%, −34% |
| **Intraday: stocks-in-play breakout (safe profile)** | $1,500 | minutes to overnight | 2024 – Oct 2026: +26%/yr, max DD −4.8% |

**Swing sleeve:**
- **Universe:** the 1,000 most-traded US stocks above their 200-day average with
  volatility ≤ 60%.
- **Holdings:** the top 10 by relative strength, rebalanced monthly. No stops; holdings
  stay while they rank in the top 20.
- **Exposure:** scaled to a 20% volatility target (25–100% invested).
- **Execution:** the validated model is replayed every morning on fresh data and copied
  with Toss fractional (amount-based) orders.

**Research:**
- Intraday breakouts, Minervini-style breakouts, RSI dip-buying and unfiltered momentum
  all fail at Toss cost or in 2022.
- Volatility-capped momentum with volatility targeting survived every test, including
  3× slippage.
- A SOXS hedge reduced returns without reducing drawdowns, so it's optional (off by default).

**Running it:** start the program between 21:00 and 22:15 KST.
- Intraday sleeve: trades 22:35–00:30 KST.
- Swing sleeve: rebalances at 22:36 KST, mostly on the first trading day of each month.
- Press Stop engine after 05:00 KST.

```bash
cd toss_dashboard/backend && ../../.venv/bin/python app.py   # dashboard: http://localhost:5050
```

## Layout

```
toss_dashboard/        Toss auto trader: engine (FastAPI) + React dashboard + research
backend/              FastAPI + Kiwoom WebSocket dashboard
frontend/              React order-book UI
screener/              US Alpaca pullback screener package
vcp_scanner/           Korean VCP / momentum scanner package
apps/foreign_trend/    PyQt live tape, broker flow, program trading
scripts/krx/           FDR screeners and KRX data scrapers
scripts/us/            one-off Alpaca helpers
tests/                 VCP scanner unit tests
output/                generated reports, CSVs, logs (gitignored)
```

Credentials live in `.env` (copy from `.env.example`). Do not hardcode keys.

US tools also need `ALPACA_API_KEY` and `ALPACA_API_SECRET` in `.env`.
KRX scrapers read `session_data.json` at the repo root.

## Python setup

There is no checked-in virtualenv. Create one in the repo:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The FastAPI dashboard can also run via Docker (`backend/requirements.txt`) without this venv.
The React UI is separate: `cd frontend && npm install`.

## Dashboard (Kiwoom order book)

```bash
cp .env.example .env   # then fill in APP_KEY / APP_SECRET
docker compose -f docker-compose.dev.yml up --build
```

- Frontend: http://localhost:5173
- Backend WebSocket: `ws://localhost:8000/ws`

Production: `docker compose up --build`

## Korean VCP scanner

```bash
python -m vcp_scanner
python -m vcp_scanner --backtest
pytest tests/ -v
```

Writes ranked CSV + report under `output/vcp/`.

## US Alpaca screener

```bash
python -m screener.main
python -m screener.main --diagnose-mpb --sample 500
```

## Live PyQt tape

```bash
python apps/foreign_trend/foreign_trend.py 078350
```

## Other scripts

```bash
python scripts/krx/master_screener.py
python scripts/krx/session_manager.py    # keep KRX cookies alive
python scripts/us/alpaca_sniper.py
```
