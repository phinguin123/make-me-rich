# make-me-rich

Korean and US equity tools: a fully automatic Toss US-stock day trader, a live Kiwoom
dashboard, a VCP/momentum scanner, and Alpaca pullback screeners.

## Toss Auto Trader (`toss_dashboard/`)

An automatic intraday trader for a Toss Securities account. It scans, enters, manages
stops and exits on its own, with a live dashboard. Full details:
[toss_dashboard/README.md](toss_dashboard/README.md).

**Strategy: stocks-in-play opening-range breakout, long only.**

1. At 09:35 ET, rank liquid stocks by first-5-minute volume versus their 14-day
   average and keep the top 30.
2. Keep only names with a green first candle, ATR ≥ 6% of price, and a close above
   the 50-day average.
3. Buy the first break of the 5-minute high before 11:30 ET, but only while the
   market is risk-on (QQQ above its VWAP and VIXY below its open).
4. Stop 0.1 ATR below entry, trailing 0.75 ATR. Exit at the close, or hold strong
   closers overnight.
5. Risk 1% of equity per trade, at most 25% in one name and 4 positions. New entries
   stop for the day after a 2% loss.

**Why:** about 200 strategy variants were backtested on 2.75 years of SIP 1-minute data
with Toss's real costs (0.1%/side + slippage):

| | CAGR | Sharpe (IS / OOS) | Max DD |
|---|---|---|---|
| Breakout on all stocks in play | −46% | −1.31 / −1.44 | −83% |
| Best unfiltered breakout / trend / overnight variant | −2% | 0.34 / −0.19 | −32% |
| Bigger-swing profile: breakout + ATR ≥ 5% + uptrend | +62% | 0.68 / 2.03 | −19% |
| **Safe profile (active): + risk-on filter, ATR ≥ 6%, 25% cap** | **+26%** | **1.11 / 1.01** | **−4.8%** |
| Safe profile, at a 0.15% fee | +23% | 1.04 / 0.78 | −5.8% |
| Safe profile, at a 0.20% fee (double Toss's rate) | +19% | 0.96 / 0.56 | −6.8% |

The safe profile was positive in every year (2024 +11%, 2025 +54%, 2026 YTD +9%) and
its worst month was −2.2%. Capital preservation comes first.

- The breakout edge is real (+41–46% CAGR at zero commission), but Toss's 0.2% round
  trip destroys it on normal stocks.
- Only very volatile, uptrending names move far enough to pay the fee.
- About 2 trades/day is the realistic speed. Thousands of trades would pay more in
  fees than the account holds, and Toss restricts order spamming.
- Returns are lumpy: the bigger-swing profile was flat in 2024. The safe profile
  trades about 0.4 times a day and waits for risk-on markets.
- Taxes: there is no US transaction tax, and the SEC fee is in the backtest. Korea's
  22% overseas capital-gains tax applies to net annual gains above ₩2.5M (see
  toss_dashboard/README.md).

**Running it:** start between 21:00 and 22:15 KST, and the bot trades
22:35–00:30 KST. Press Stop engine after 05:00 KST. Shift one hour later after
US DST ends.

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
