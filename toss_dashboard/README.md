# Toss Auto Trader

Fully automatic US-stock trader for a Toss Securities account, with a live dashboard.
One process runs the engine, the REST/WebSocket API and the UI. It runs two sleeves:

| Sleeve | Capital | Holding period | Backtest (all Toss costs) |
|---|---|---|---|
| **Swing: monthly momentum + volatility targeting** | `TRADER_SWING_CAPITAL` (3,500) | weeks to months | 2019 – Oct 2026: **+24.5%/yr**, Sharpe 1.12, max DD **−20%**, positive every year incl. 2022 (+15%) |
| **Intraday: stocks-in-play breakout, safe profile** | `TRADER_CAPITAL` (1,500) | minutes to overnight | 2024 – Oct 2026: +26%/yr, max DD −4.8% (not tested in a bear market) |

The order path was verified live on the Toss account (create, modify and cancel, plus
WebSocket fill events). Amount-based and fractional orders were also validated against
the API.

## Swing sleeve (main strategy)

It uses the retail trader's edge: you can hold winners for months, ignore benchmark
rebalancing, and skip tight stops that shake out institutions.

1. **Universe:** the 1,000 most-traded US stocks (price ≥ $10, 50-day $ volume ≥ $30M),
   trading above their 200-day average, with annualized volatility ≤ 60%. The
   volatility cap is what keeps meme spikes and blow-off tops out.
2. **Rank** by IBD-style relative strength: 40% 3-month, 20% each 6/9/12-month return.
3. **Hold the top 10, equal weight.** Rebalance on the month's last close, trading at
   the next open. Holdings stay while they rank in the top 20, which keeps turnover
   low (about 4–5 buys a month).
4. **No stop losses.** Patience is the edge: adding stops or a 100-day exit cut returns.
5. **Volatility targeting.** Scale stock exposure so the strategy runs at about 20%
   annualized volatility (25–100% invested). This is the crash protection from
   "Momentum has its moments" (Barroso & Santa-Clara 2015). It de-risks automatically
   when markets get violent.
6. **Optional SOXS hedge** (`TRADER_SWING_HEDGE=soxs_broad`, off by default): hold
   10% SOXS while semis and the broad market break down together.

**Live execution:** each morning (from 09:15 ET) the engine refreshes daily bars for all
~6,100 non-fund US stocks from Alpaca's free API. It re-runs the validated model from its
Jan 2024 anchor and copies the model's weights at 09:36 ET using Toss amount-based
(fractional) market orders. Small drift (within 70–130% of target) is not traded.
Because the model is replayed daily, live trading matches the backtest, including
catch-up after days the PC was off. Missed fills are recovered after a restart.

### Swing research (2019 – Oct 2026, 7,266 US stocks incl. delisted, Toss 0.1%/side + 0.1% slippage)

| Strategy | CAGR | Sharpe (IS 2019-22 / OOS 2023-26) | Max DD | 2022 |
|---|---|---|---|---|
| SPY buy & hold | +17.4% | 0.67 / 1.42 | −34% | −18% |
| QQQ buy & hold | +23.3% | 0.67 / 1.50 | −35% | −33% |
| SMH buy & hold | +41.3% | 0.78 / 1.53 | −45% | −34% |
| Minervini-style breakouts (best of 144) | +3.6% | — | −30% | −11% |
| Pure 12-1 momentum, all liquid stocks | +0.3% | 0.25 / 0.23 | −75% | −29% |
| Momentum top 1000 + vol cap 60% (no overlay) | +37–38% | 0.89–0.93 / 1.29–1.32 | −32 to −33% | +23% |
| **Chosen: + 20% volatility target** | **+24.5%** | **0.82 / 1.45** | **−20%** | **+15%** |
| same, slippage 0.3% (3× assumed) | +22.8% | 0.75 / 1.38 | −21% | +14% |
| same, commission 0.25% (2.5× Toss) | +22.8% | 0.74 / 1.40 | −21% | +12% |
| same + SOXS hedge 10% | +20.6% | 0.65 / 1.27 | −21% | −2% |
| same with SPY<200-day → cash | worse in every variant tested (whipsaw) | | | |

Years for the chosen model: 2019 +1%, 2020 +54%, 2021 +5%,
2022 +15%, 2023 +19%, 2024 +62%, 2025 +26%, 2026 YTD +18%.

What the research found:
- Momentum works only with a **volatility cap**. Without it, the top momentum names are
  meme stocks, crypto miners and SPAC spikes (median 12-month gain +390%), and they crash.
- **Patience beats activity.** Monthly rebalancing beat weekly. No stop beat 100-day or
  trailing-stop exits. Going to cash when SPY breaks its 200-day hurt.
- **Volatility targeting** cut the max drawdown from −33% to −20% and kept every year
  positive. In 2022 momentum rotated into energy and defensives and made money.
- **The SOXS hedge cost more than it protected** (SOXS decays fast, and bear-market
  rallies whipsaw it), so it is off by default.
- **Minervini as a mechanical system** had a real ~1:2.3 payoff, but only about 33% of
  trades won, so it roughly broke even. His edge is discretionary judgment plus earnings
  data that this bot doesn't have.
- **Machine learning didn't help (2021 – Oct 2026, walk-forward).** I swapped the momentum
  ranking for a LightGBM ranker (17 price/volume features, retrained monthly only on past
  data) and kept the same universe, portfolio rules and costs:

  | Ranker | CAGR | Sharpe | Max DD |
  |---|---|---|---|
  | **Momentum (current)** | **+24.5%** | **1.06** | **−22%** |
  | ML retrained monthly, all history | +8.3% | 0.47 | −36% |
  | ML retrained monthly, last 36 months only | +3.8% | 0.28 | −36% |
  | ML trained once (2019-20) | +6.0% | 0.38 | −24% |

  Continuous retraining ("keep learning") didn't help, and the faster-adapting version
  was worst: market data is noisy and keeps changing character. Momentum's edge sits in
  the extreme top names (+1.9%/month excess for the top 10), which an average-accuracy
  model doesn't target. `python -m trader.research.ml_ranker` reproduces it.
- **Retail-capacity edges didn't beat it either** (2019 – Mar 2026, small-cap costs of 0.5% slippage):
  - Momentum in small/mid caps (ranks 500–4,000 by $ volume): −3% to +17%/yr, worse risk.
  - Insider cluster buying from SEC Form 4 data (`trader/research/insider.py`):
    +16–18%/yr at best with −49% drawdowns.
  - Blending insider with momentum (correlation 0.36): Sharpe 1.10 → 1.13 at best,
    with bigger drawdowns. Not worth the complexity.
- **The return level is a risk choice:** the same momentum model returns +24.5% at a 20%
  volatility target (−20% DD), +27.8% at 30%, and +37% without targeting (−32% DD).

### Market read (data through 2026-10-08)

- **Rates:**
  - 10y Treasury at 5.28%, up 72bp in 3 months and 114bp in a year (highest in ~20 years).
  - 2y at 4.77%; the 10y–3m curve is positive (+0.99).
  - High-yield spread at 3.09%: low, but up 38bp in a month.
- **Macro:** dollar index +3.2% in a month, oil +28% in 3 months, VIX 15 (VIX3M 18,
  no stress), MOVE 101.
- **Breadth is narrow.** SPY is within 1% of its all-time high and SMH is 20% above its
  200-day, yet only **45% of liquid stocks are above their 200-day and 28% above their
  50-day**. A few leaders are carrying the index.
- **What history says (2016–2026):**
  - Rising yields (+50bp in 3 months) and a rising dollar were mild headwinds:
    3-month SMH returns about halved, and the odds of a −15% semis drawdown rose
    from 16% to about 31%.
  - SMH more than 35% above its 200-day was followed by flat semis and a 39% chance
    of a −15% drop. That isn't true today (+20%).
  - None of these signals reliably predicts a crash. Price trend and volatility do the
    real work, and the volatility target reacts to them automatically.
- **What the model holds now (86% invested):** refiners riding oil (DINO, VLO, MPC),
  biotech/diagnostics (ILMN, NTRA, RVMD, SYRE), FTNT, HNGE and CAKE. **No
  semiconductors**: they are currently too volatile for the strategy's risk cap. That
  is the model's own answer to the overheated-semis risk.

**Data sources:** all free.
- Alpaca: daily and minute bars, IEX real-time.
- FRED: Treasury yields, credit spreads, dollar index.
- Yahoo: ES/NQ/ZN/CL/GC futures, VIX/VIX3M/MOVE, SOX.
- Toss: real-time quotes and orders.

No paid service is required. Unusual Whales-style options flow and "put walls" don't fit a
monthly strategy and have no robust systematic evidence, so they're not used. Optional
upgrades:
- **Alpaca SIP real-time ($99/mo):** improves the intraday scanner (its free IEX feed
  matches the full-market ranking only 51% of the time).
- **Databento CME live (~$33/mo + exchange fees):** needed only for real-time futures
  trading, which this bot doesn't do.

## SOXL/SOXS scalp sleeve (`TRADER_SCALP_CAPITAL`, off by default)

**Research:** 2021 – Oct 2026, 1-minute SIP bars across all sessions, plus Toss day-market bars
since Nov 2025. Costs: Toss 0.1%/side, plus slippage of 0.03% regular, 0.10% pre/after and
0.25% day market per side. Script: `trader/research/scalp.py`.

| Edge tested | Result after costs |
|---|---|
| Late-day leveraged-ETF momentum (rebalancing flows) | −0.13 to −0.25%/trade; no gross edge since 2024 |
| Opening gap fade / follow | negative |
| Pre-market momentum or fade | ~0 gross, so −0.3 to −0.5% net (pre-market round trip ≈ 0.4%) |
| VWAP stretch reversion | 60% win rate but −0.3%/trade (losses are large) |
| Day market (Korean hours) first-hour momentum | −0.6 to −1.4%/trade (session mean-reverts, thin) |
| Opening-range breakout, all days | ~0 |
| **Opening-range breakout, only when SOXX < 50-day avg** | **+0.32%/trade (IS +0.34%, OOS +0.30%), ~88/yr** |

**Rules:** trade only after SOXX closed below its 50-day average. The opening range is SOXL's
first 30 minutes. The first break up buys SOXL; the first break down buys SOXS. Entries run
until 11:30 ET, one trade a day. The stop is SOXL back through the opposite side of the range;
otherwise exit at the close. Size is 50% of the sleeve per trade.

**Results:**
- Bear-regime trades only: about +13%/yr with a −19% max drawdown at 50% size (+22% / −35% at
  100%). Positive in 2021–2025; 2026 YTD −0.16%/trade.
- The edge survives up to 0.1%/side slippage and disappears at 0.2%. t ≈ 1.6: a modest edge.
- Why it can work: in semis selloffs, dealer hedging and leveraged-ETF rebalancing amplify
  intraday trends. In uptrends the same breakouts mean-revert, so the sleeve stands aside.
- The regular-session breakout is not used in the other sessions; they work differently (below).

### Pre-market: SOXL dip below the pre-market VWAP (`TRADER_SCALP_PRE`, on with the sleeve)

**Research:** `trader/research/scalp_ext.py`. Pre-market on 2021–2026 SIP bars, cross-checked on
Toss bars since Nov 2025. The day market used Toss bars since Nov 2025 and Samsung/SK Hynix
1-minute bars for the lead-lag tests.

| Edge tested | Result after costs |
|---|---|
| Overnight gap ≥ 6% at 04:00, follow to 09:29 | +0.43%/trade at realistic spreads, t 1.5; down-gaps ~0 |
| 08:30 ET (CPI/jobs) move continuing to the open | −0.25%/trade: these moves reverse |
| Pre-market trend into 09:00 continuing to the open | ~0 gross |
| Day-market move → pre-market follow | +0.58%, t 2.0, but negative in the first half |
| **SOXL dip ≥ 0.4 × 20-day vol below pre-market VWAP, 07:00–09:00, limit buy** | **+1.02 to +1.50%/trade, t 3.5–7.6, ~10–15/yr** |
| Day market: Korean semis lead SOXL (session, 5- and 15-minute) | gross +0.04 to +0.3%, net negative |
| Day market: SOXL catch-up after the KRX close | −0.38%/trade |
| Day market: VWAP-reversion limit orders | −0.2 to −0.8%/trade |
| Day market: prior regular / after-hours momentum or fade | −0.1 to −0.5%/trade |
| Day market: stale SOXL vs SOXS quotes | ~0 or negative |

**Rules:**
- The dip size is 0.4 × SOXL's 20-day daily volatility, kept between 1.5% and 6% (about 3% in 2026).
- From 07:00 to 09:00 ET, when SOXL trades that far below its pre-market VWAP, the sleeve places
  a limit buy at that level. The order rests for up to 10 minutes. The sleeve never chases.
- A resting take-profit sells at entry + dip. Otherwise the position is sold at 09:25 ET with
  limit orders. If that fails, it's sold at market after the open.
- One trade a day, 50% of the sleeve. It trades in uptrends and downtrends alike.

**Results:**
- +1.50%/trade if every touch fills.
- +1.02% if we're filled only when a 1-minute bar closes below our price (t 3.5).
- Both halves of the sample are positive. 2026 is weaker.
- Worst trade is about −3.7%; the worst dip inside a trade is −11%.

**Why it can work:** the pre-market is thin. Forced or stop-loss selling pushes SOXL below fair
value, and the open's liquidity pulls it back. A resting limit order is paid for supplying that
liquidity. Outside regular hours, prints can trade below our price on another venue without
filling us, so expect the lower number.

**The day market is not traded.** Ten hypothesis families failed after costs. The venue is a single
ATS (Blue Ocean) with wide spreads and a record of mass cancellations (Aug 2024).

## Edge health monitor (`trader/edges.py`)

Each live edge's closed trades are compared with its backtest, and the edge is sized down or
switched off when live results fall clearly outside what the backtest allows.
Expectations live in `trader/edges.json`; regenerate them after re-running research with
`python -m trader.research.edge_stats`.

| Edge | Backtest avg/trade | Std/trade | Trades/yr | Half size at drawdown | Off at drawdown |
|---|---|---|---|---|---|
| Opening-range breakout (bear regime) | +0.33% | 4.3% | ~90 | −55% | −82% |
| Pre-market SOXL dip | +1.02% | 2.1% | ~13 | −8.3% | −13.2% |

**Rules.** Drawdown is the sum of per-trade returns on the position from its peak. At 50%
sizing, the account sees half of it. The two limits are the worst one-year runs that only 10% and
1% of 20,000 resampled backtest years reach.
- **Off (size ×0):** the drawdown passes the 1-in-100 limit, or after 15+ trades the live average
  is 3 standard errors below the backtest (z ≤ −3). It stays off until you press **Re-enable**
  on the dashboard; the edge is then judged afresh from its next trade.
- **Reduced (×0.5):** the drawdown passes the 1-in-10 limit, or z ≤ −2 after 8+ trades. It goes
  back to full size once z > −1.5 and the drawdown has recovered to half the limit.
- **Healthy (×1):** otherwise.

**What it can and can't catch:**
- It catches an edge that broke: execution problems, costs far above the model, or a market that
  turned firmly against it.
- It can't quickly see a slow fade to zero. The breakout's per-trade noise (4.3%) is 13× its
  edge, so that takes hundreds of trades.
- So re-run the research with new data every quarter, and update `edges.json` or retire edges
  whose fresh out-of-sample results fail.

## Korean investor-flow (수급) research

I replicated the core claims of Daishin's "수급의 정석, 구조와 알파" (Apr 2026) on Toss flow data
(Nov 2019 – Oct 2026). The universe is 2,000 KR common stocks with point-in-time market-cap
buckets. Portfolios are long-only, and costs are 0.015% commission per side, the **0.20%
2026 sell tax**, and slippage. Scripts: `trader/research/kr_flows.py` (download) and
`kr_flow_study.py`.

| Signal (long-only, vs size-bucket equal weight) | Gross excess | Cost | Net excess |
|---|---|---|---|
| Other corporations 5-day buying, mid caps (report's headline) | +10.1%/yr (t 3.4) | 16.9%/yr | **−6.8%** |
| Following pension buying, large, 20D | −6.2%/yr (t −2.6) | 1.9% | −8.1% |
| Pension net-SELL screen, large, 20D | +6.2%/yr (t 2.6) | 1.8% | +4.4% |
| Strategy 2: other corp ∩ PEF, mid, 5D/5D | +10.5%/yr | 22.6%/yr | **−12.1%** |
| Strategy 3: fin. investment 4-week persistence, large | +18.2%/yr (t 1.4) | 4.4% | +13.8% (but only from 2024 financials) |
| **Momentum ∩ pension net-sell, large, monthly** (Strategy 1 proxy) | +14.5%/yr (t 2.7) | 2.2% | **+12.4%** |

- The report's descriptive findings replicate in gross terms. Its weekly strategies don't
  survive the 0.20% sell tax.
- Strategy 3's result came from financials during the 2024 "Value-up" rally (+71% in 2024,
  −12% in 2023, −7% in 2025), so it's regime luck.
- The monthly "factor × pension sell" structure survives: +27% CAGR (universe +15%),
  Sharpe 1.0, max DD −35%, positive excess every year 2020–2026.
- Its monthly correlation with the US momentum sleeve is +0.28. A 70/30 US/KR blend raises
  the monthly Sharpe from 1.37 to 1.53.

**Korea → US:**
- Samsung/Hynix foreign flows have no predictive power for the next US session (SMH rank
  corr −0.01; the trading rule lost 39% after fees).
- The Korean session moves with the US overnight gap (corr 0.34). That happens *at the same
  time*, so the US day market (Blue Ocean, a single thin venue) already prices it in.
- The only residual: strong KR memory days were followed by MU's regular session at
  +0.44% vs +0.06% (+0.24%/trade after fees, weak t). That's a paper-test candidate only.

## Intraday sleeve: strategy

1. **09:35:10 ET: scan.** Rank liquid US stocks (price > $5, 14-day ADV ≥ 1M shares,
   ATR ≥ $0.50, no ETFs, funds or leveraged products) by the relative volume of their
   first 5-minute bar versus its own 14-day average. Keep the top 30.
2. **Filter.** The first 5-minute candle must be green, ATR must be ≥ 6% of price,
   and the previous close must be above the 50-day average.
3. **Entry.** Buy when price trades 1¢ above the 5-minute high, before 11:30 ET, and
   only while the market is risk-on: QQQ's last minute closed above its VWAP and VIXY
   is below its open. Only the first breakout counts. Orders are marketable limits that never chase more than
   0.4% past the trigger, and entries are skipped when the spread is above 0.5%.
4. **Risk.** Initial stop is 0.1 × ATR below entry, then a trailing stop 0.75 × ATR
   under the highest completed 1-minute high. Each trade risks 1% of equity, with at
   most 25% of equity in one position and 4 positions at a time. No leverage. New
   entries stop for the day after a 2% loss.
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
| Bigger-swing profile: ORB + ATR ≥ 5% + uptrend | +62% | 0.68 / 2.03 | −19% |
| same, commission 0.15% | +38% | 0.42 / 1.57 | −28% |
| same, commission 0.20% | +19% | 0.16 / 1.12 | −40% |
| same, slippage 0.10%/side | +38% | 0.47 / 1.51 | −26% |
| same, scanner keeps only the top 10 | +36% | 1.04 / 0.94 | −15% |
| same + risk-on filter (`regime: risk_on`) | +34% | 0.96 / 1.12 | −10% |
| same, entries allowed until 15:30 | +71% | 0.67 / 2.10 | −21% |
| Bigger-swing + risk-on filter, ATR ≥ 6% (35% cap) | +37% | 1.09 / 1.04 | −6.7% |
| **Safe profile (active): + 25% position cap** | **+26%** | **1.11 / 1.01** | **−4.8%** |
| safe, commission 0.15% | +23% | 1.04 / 0.78 | −5.8% |
| safe, commission 0.20% | +19% | 0.96 / 0.56 | −6.8% |
| safe, slippage 0.10%/side | +23% | 1.01 / 0.85 | −5.9% |

The safe profile returned 2024 +11%, 2025 +54% and 2026 YTD +9%, with a worst month of
−2.2%. To switch to the bigger-swing profile, set `regime: none` and
`min_atr_pct: 0.05` in `backend/data/strategy.json`, add
`TRADER_MAX_POSITION_PCT=0.35` to `.env`, and restart.

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

## Costs and taxes

- **In the backtest, per trade:**
  - Toss commission 0.1% on the buy and 0.1% on the sell.
  - SEC fee 0.00206% on sells.
  - Slippage 0.05% per side.
- **No US transaction tax.** Korea's 증권거래세 applies to Korean stocks, not US stocks.
  The FINRA TAF ($0.000166/share sold) is negligible and not modeled.
- **Korean capital-gains tax (해외주식 양도소득세):** 22% (20% national + 2% local) of
  your net realized overseas-stock gains for the calendar year, above a ₩2.5M basic
  deduction. It's filed in May of the following year.
  - Gains and losses from the bot and your manual US trades are combined, and the
    result is measured in KRW at the exchange rates on each trade date.
  - The backtest doesn't model it, because it is charged once a year on net profit,
    not per trade. It doesn't change which strategy works, only how much you keep.
- **Example ($5,000, about ₩1,340/$):**
  - Safe profile at +26% ≈ $1,300 ≈ ₩1.74M → below the ₩2.5M deduction, so no tax
    if that's your only overseas gain that year.
  - Bigger-swing profile at +62% ≈ $3,100 ≈ ₩4.15M → (₩4.15M − ₩2.5M) × 22% ≈ ₩363k
    (about $270), so the after-tax return is about +56%.
- **Currency exchange:** converting KRW↔USD has a spread, which isn't modeled. The bot
  trades your USD balance directly.

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

**Swing sleeve:**
- 21:15–22:30: refreshes data and runs the model; 22:36 copies the model's weights.
- Real trades happen mainly on the first trading day of each month, plus occasional
  volatility-target resizes.
- It holds through the night and through days the PC is off. If the PC was off on a
  rebalance day, the catch-up happens at the next start.

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

Docker (multi-arch, works on Oracle A1 / arm64). From this machine, one command copies the
code, `.env` and state to the server, builds the image there and starts it:

```bash
cd toss_dashboard
./deploy_oracle.sh ubuntu@<server-ip>      # stop the local engine first
```

Or on the server itself: `cd toss_dashboard && docker compose up -d --build`. The container
restarts by itself after crashes and reboots (`restart: unless-stopped`). Logs:
`docker compose logs -f`, or `backend/data/logs/trader.log` on the server.

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
| `TRADER_CAPITAL` | `3000` | USD for the intraday sleeve (never more than Toss cash buying power) |
| `TRADER_SWING_CAPITAL` | `0` | USD for the swing sleeve (0 disables it) |
| `TRADER_SWING_HEDGE` | `none` | `soxs_broad` enables the optional SOXS hedge |
| `TRADER_RISK_PER_TRADE` | `0.01` | equity lost if a trade hits its initial stop |
| `TRADER_MAX_POSITIONS` | `4` | concurrent positions |
| `TRADER_MAX_POSITION_PCT` | `0.25` | max equity in one position |
| `TRADER_DAILY_LOSS_LIMIT` | `0.02` | no new entries for the day after this drawdown |
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
- Order throttle, 2% daily loss halt, 25% position cap, no leverage, 0.5% spread guard,
  risk-on market filter.
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
- **Tests:**
  - `tests/test_trader.py` covers the intraday logic.
  - `tests/test_swing.py` covers swing rebalancing, the no-trade bands,
    reconciliation, restart fill recovery and a synthetic-data model run.

## Reproduce the research

```bash
cd toss_dashboard/backend
python -m trader.research.download --start 2024-01-01 --end 2026-10-06   # ~40 min, Alpaca free plan
python -m trader.research.experiments all                                 # unfiltered sweeps
python -m trader.research.iex_check --days 25                             # scanner fidelity
python -m trader.research.minervini download && python -m trader.research.minervini   # 2018+ daily bars, breakout sweep
python -m trader.research.portfolio                                       # swing momentum sweep (needs etf_daily.parquet)
python -m trader.research.regime                                          # market signals vs drawdowns + current readings
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
    strategy.py          intraday strategy rules + validated defaults
    swing.py             swing sleeve: daily data store, model replay, rebalancing
    macro.py             free macro data (FRED, Yahoo futures/VIX)
    engine.py            scheduler, entries/exits, persistence, end-of-day logic
    broker.py            PaperBroker (simulated) / TossBroker (real orders)
    risk.py              sizing, daily loss halt, order throttle
    check.py             read-only preflight
    research/            intraday backtester, swing portfolio backtester, regime study, Minervini test
  tests/
frontend/                React dashboard (Vite + Tailwind)
Dockerfile, docker-compose.yml
```
