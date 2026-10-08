# Toss Auto Trader

Fully automatic US-stock day trader for a Toss Securities account, with a live
dashboard. One process runs the engine, the REST/WebSocket API and the UI.

- **Strategy:** stocks-in-play 5-minute opening-range breakout, long only, restricted to
  very volatile names in an uptrend. It is the only variant of ~200 tested that stays
  profitable after Toss's 0.1%/side commission.
- **Backtest (Jan 2024 – Oct 2026, $3,000 start, all costs):** +62% CAGR, Sharpe 1.24,
  max drawdown −19%, ~1.8 trades/day. Returns are lumpy (see below).
- **Status:** the order path was verified live on the Toss account (create, modify,
  cancel and WebSocket fill events). The engine runs in `live` mode with
  `TRADER_CAPITAL=5000`.

## Strategy

1. **09:35:10 ET: scan.** Rank liquid US stocks (price > $5, 14-day ADV ≥ 1M shares,
   ATR ≥ $0.50, no ETFs, funds or leveraged products) by the relative volume of their
   first 5-minute bar versus its own 14-day average. Keep the top 30.
2. **Filter.** The first 5-minute candle must be green, ATR must be ≥ 5% of price,
   and the previous close must be above the 50-day average.
3. **Entry.** Buy when price trades 1¢ above the 5-minute high, before 11:30 ET. Only
   the first breakout counts. Orders are marketable limits that never chase more than
   0.4% past the trigger, and entries are skipped when the spread is above 0.5%.
4. **Risk.** Initial stop is 0.1 × ATR below entry, then a trailing stop 0.75 × ATR
   under the highest completed 1-minute high. Each trade risks 1% of equity, with at
   most 35% of equity in one position and 4 positions at a time. No leverage.
5. **Exit.** On the stop, or at 15:57 ET. Exception: if the trade is ≥ 1R up and closing
   in the top 20% of its day range, hold overnight with the stop at breakeven or better,
   and exit by the next day's close.

### Why this strategy (research summary)

All numbers below include Toss's 0.1%/side commission, the SEC fee and 0.05%/side
slippage, on 1-minute SIP data with integer shares and a $3,000 start. In-sample (IS)
is 2024-01..2025-06, out-of-sample (OOS) is 2025-07..2026-10.

| Strategy | CAGR | Sharpe IS / OOS | Max DD |
|---|---|---|---|
| ORB on all stocks in play (published "stocks in play" rules) | −46% | −1.31 / −1.44 | −83% |
| Best of 196 unfiltered ORB / trend-continuation / overnight variants | −2% | 0.34 / −0.19 | −32% |
| **Chosen: ORB + ATR ≥ 5% + uptrend** | **+62%** | **0.68 / 2.03** | **−19%** |
| same, commission 0.15% | +38% | 0.42 / 1.57 | −28% |
| same, commission 0.20% | +19% | 0.16 / 1.12 | −40% |
| same, slippage 0.10%/side | +38% | 0.47 / 1.51 | −26% |
| same, scanner keeps only the top 10 | +36% | 1.04 / 0.94 | −15% |
| same + risk-on filter (`regime: risk_on`) | +34% | 0.96 / 1.12 | −10% |
| same, entries allowed until 15:30 | +71% | 0.67 / 2.10 | −21% |

What the research found:

- **Fees decide everything.** At 0% commission the plain breakout makes +41–46% CAGR in
  both periods. Toss's 0.2% round trip wipes it out on ordinary stocks, because a tight
  stop is only ~0.4% away and fees eat half of it. Very volatile, uptrending names move
  far enough to pay the fee.
- **High frequency is not viable at this cost.** About 2 trades/day is the right speed.
  Toss also restricts accounts that send hundreds of small orders, and the engine caps
  orders at 20 per 10 minutes and 120 per day.
- **Returns are lumpy.** 2024 was roughly flat. Most gains came in Q2 2025 (+49%),
  Q2 2026 (+88%) and Q3 2026 (+32%). Win rate is about 19%, with a few large winners.
  Expect losing months.
- **Afternoon entries add nothing.** Allowing entries until 15:30 adds only 54 trades in
  2.75 years. Entries between 11:30 and 14:30 lose about 0.8% each, so the cutoff stays
  at 11:30 ET.
- **Rejected ideas:**
  - Multi-day "gap and go" loses: high-volume gap-ups fall about 2% net over 20 days.
  - RSI(2) dip-buying is positive per signal but ~0 in-sample, and negative as a portfolio.
  - Trend continuation and overnight-drift variants lose at Toss cost.
- **Macro proxies** (free and real-time via Toss: QQQ for Nasdaq futures, VIXY for VIX,
  TLT for yields, UUP for the dollar, GLD, USO, IBIT/BTC):
  - VIXY moves −0.66 with QQQ intraday.
  - HYG, SMH and QQQ's first 30 minutes weakly predict QQQ's rest of day (t ≈ 2.5–2.8).
  - TLT, UUP, GLD and USO carry no intraday signal.
  - VIXY down > 1% in the first 30 minutes was followed by QQQ +0.28% on average.
- **Scanner data gap.** The live scanner uses Alpaca's free IEX feed. Its top 20
  overlaps the full-market (SIP) top 20 51% of the time on average (25 sampled days,
  rank correlation 0.52). The strategy still works on the top 10 or top 20, but the
  biggest gap between backtest and live is here. Alpaca's SIP feed ($99/mo) would close it.

## Daily schedule (KST)

The Toss US calendar drives the engine, so holidays and half days are handled. With US
daylight saving time (until Nov 1):

| KST | Engine |
|---|---|
| 20:30 | new trading day starts |
| 21:00 | builds today's universe (daily stats + opening-volume baselines, ~3–5 min) |
| **21:00–22:15** | **latest window to start the program** |
| 22:30 | US market opens |
| 22:35 | scan; filtered setups are armed |
| 22:35–00:30 | breakout entries |
| 04:57 | exits (strong winners may be held overnight) |
| after 05:00 | press **Stop engine** before turning the PC off, so overnight holds get a Toss server-side stop |

After November 1, everything shifts one hour later (start by 23:00, market 23:30–06:00).
The PC must stay awake while positions are open. For 24/7 operation, run the Docker
image on a server.

## Run

```bash
cd toss_dashboard/backend
../../.venv/bin/pip install -r requirements.txt
../../.venv/bin/python -m pytest tests -q          # unit tests
../../.venv/bin/python -m trader.check             # read-only: IP allowlist, token, account, feeds
setsid nohup ../../.venv/bin/python app.py > data/logs/app_stdout.log 2>&1 < /dev/null &
```

Dashboard: http://localhost:5050. It has start/stop, pause new entries, and flatten
bot positions, plus positions, stocks in play, closed trades, macro tiles and the
engine log. UI development: `cd toss_dashboard/frontend && npm install && npm run dev`
(proxies to :5050).

Docker (multi-arch, works on Oracle A1 / arm64):

```bash
cd toss_dashboard
docker compose up -d --build
```

The container publishes on 127.0.0.1 by default. Reach it with
`ssh -L 5050:localhost:5050 <server>`, or set `TRADER_BIND=0.0.0.0` together with a
`TRADER_DASHBOARD_TOKEN` and open `http://<server>:5050/?token=<token>` once.
Add the server's public IP to Toss WTS → 설정 → Open API → 허용 IP 관리 first. Run
the bot in only one place: a second process issues a new Toss token, which logs the
first one out.

## Settings (.env at the repo root)

| Variable | Default | Meaning |
|---|---|---|
| `TRADER_MODE` | `paper` | `paper` simulates fills on live Toss quotes; `live` sends real orders |
| `TRADER_CAPITAL` | `3000` | USD the bot may use (never more than Toss cash buying power) |
| `TRADER_RISK_PER_TRADE` | `0.01` | equity lost if a trade hits its initial stop |
| `TRADER_MAX_POSITIONS` | `4` | concurrent positions |
| `TRADER_MAX_POSITION_PCT` | `0.35` | max equity in one position |
| `TRADER_DAILY_LOSS_LIMIT` | `0.03` | no new entries for the day after this drawdown |
| `TRADER_MAX_ORDERS_10MIN` / `_DAY` | `20` / `120` | order throttle |
| `TRADER_HOLD_OVERNIGHT` | `true` | allow carrying strong closers overnight |
| `TRADER_SERVER_STOPS` | `true` | live: Toss conditional stop for overnight holds and on engine stop |
| `TRADER_MAX_COMMISSION` | `0.0012` | entries pause if Toss reports a higher US commission |
| `TRADER_HOST` | `0.0.0.0` | bind address (`127.0.0.1` on a desktop) |
| `TRADER_DASHBOARD_TOKEN` | — | required for the API/UI when set |
| `TRADER_AUTOSTART` | `true` | start the engine with the server |

Strategy parameters default to the validated values in `trader/strategy.py`
(`StrategyConfig`). `data/strategy.json` overrides them.

## Safety rules built in

- The engine only manages positions it opened (`data/state_<mode>.json`). Holdings you
  buy yourself are never touched.
- Order throttle, 3% daily loss halt, 35% position cap, no leverage, 0.5% spread guard.
- Entries pause automatically if the Toss US commission rises above
  `TRADER_MAX_COMMISSION`. The rate is re-checked every session (the API listed the
  0.1% rate as valid until 2026-10-08).
- Exits escalate: limit 0.3% under the bid, then 1% under, then market.
- Crash recovery: positions, P&L and paper cash persist after every change. Live
  positions are reconciled against Toss holdings on restart. Breakouts missed while
  the engine was down are skipped instead of chased.
- One Toss token per key, cached on disk so restarts don't revoke it.

## What was built and verified

- **Replaced the old bot** (Flask + VPIN/OFI/Almgren-Chriss on a Kiwoom feed). Toss's
  WebSocket ticks are sampled at ~1 s with top-of-book only, so tick-level
  microstructure signals cannot work on it.
- **Toss client from the official spec.** The old client used guessed endpoints.
  The new one follows the OpenAPI/AsyncAPI documents: per-group rate limits, a shared
  token cache, a typed error envelope, and declarative WebSocket subscriptions with
  reconnect and keepalive.
- **Research pipeline** (`trader/research/`):
  - Alpaca SIP download covering every NYSE/NASDAQ/AMEX/ARCA/BATS symbol (daily bars),
    opening 5-minute bars for ~1,000 candidates/day, and 1-minute bars (day D and D+1)
    for the daily top 30.
  - 12 macro ETF proxies.
  - Event-accurate backtester with portfolio constraints, plus sweeps, filter
    analysis, cost/slippage stress tests and the IEX-vs-SIP scanner check.
- **Paper run on the live market:** scan → filter → arm → entry → stop-out → P&L
  recorded, end to end.
- **Live verification on the Toss account:** a 1-share, non-marketable test order
  went through create, modify and cancel. It caught a real bug: Toss rejects bodiless
  POSTs without a JSON `Content-Type` (HTTP 415), so cancels would have failed in
  production. The bug is fixed, and the full lifecycle with WebSocket events was
  confirmed afterwards.
- **Tests:** `tests/test_trader.py` covers price formatting, sizing, throttle, loss
  halt, strategy filters and the trailing stop, the opening range, and paper fills.

## Reproduce the research

```bash
cd toss_dashboard/backend
python -m trader.research.download --start 2024-01-01 --end 2026-10-06   # ~40 min, Alpaca free plan
python -m trader.research.experiments all                                 # unfiltered sweeps
python -m trader.research.iex_check --days 25                             # scanner fidelity
```

Research data (~1.5 GB) and runtime state live in `backend/data/`, which is git-ignored.

## Layout

```
backend/
  app.py                 FastAPI host: engine + REST + /ws + built UI
  trader/
    config.py            settings from the repo-root .env
    toss/client.py       Toss Open API (REST)
    toss/stream.py       Toss WebSocket: trades, top-of-book, your order events
    alpaca.py            Alpaca free data: SIP history, IEX realtime
    scanner.py           stocks-in-play scanner
    market.py            live quotes, minute bars, VWAP, opening range
    strategy.py          strategy rules + validated defaults
    engine.py            scheduler, entries/exits, persistence, end-of-day logic
    broker.py            PaperBroker (simulated) / TossBroker (real orders)
    risk.py              sizing, daily loss halt, order throttle
    check.py             read-only preflight
    research/            download, backtester, sweeps, IEX check
  tests/
frontend/                React dashboard (Vite + Tailwind)
Dockerfile, docker-compose.yml
```
