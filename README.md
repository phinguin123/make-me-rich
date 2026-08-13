# make-me-rich

Korean and US equity tools: a live Kiwoom dashboard, a VCP/momentum scanner, and Alpaca pullback screeners.

## Layout

```
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
