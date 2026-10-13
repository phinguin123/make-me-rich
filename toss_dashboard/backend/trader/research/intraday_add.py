"""Would the (built, switched-off) stocks-in-play intraday sleeve improve the account?

Backtest of the live safe profile on SIP minute data (2024-01 .. 2026-10), then its daily returns are
combined with the scalp (breakout + pre-market book) and swing sleeves on the same days.

    python -m trader.research.intraday_add
"""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

from . import allocation_opt as AO
from .backtest import Params, generate_trades, load_all, run_portfolio

SAFE = Params(strategy="orb", or_minutes=5, top_n=30, require_green=True, min_atr_pct=0.06, require_trend=True,
              stop_mode="atr", stop_atr=0.10, trail_atr=0.75, exit_mode="hold_strong", regime="risk_on",
              entry_cutoff=time(11, 30), max_positions=4, risk_per_trade=0.01, max_pos_pct=0.25, capital=3000.0)


def main() -> None:
    inplay, macro, cache, days = load_all()
    trades = generate_trades(SAFE, inplay, macro, cache)
    taken, daily = run_portfolio(SAFE, trades)
    daily.index = pd.to_datetime(daily.index)
    base = AO.load()
    alldays = base.index[(base.index >= pd.Timestamp(min(days))) & (base.index <= pd.Timestamp(max(days)))]
    daily = daily.reindex(alldays).fillna(0.0)  # days without intraday trades earn nothing (not missing)
    eq = SAFE.capital + daily.cumsum()
    r_in = (daily / eq.shift(1).fillna(SAFE.capital)).rename("intraday")
    yrs = len(r_in) / 252
    e = (1 + r_in).cumprod()
    print(f"intraday safe profile {r_in.index[0]:%Y-%m} .. {r_in.index[-1]:%Y-%m}: {len(taken)} trades "
          f"({len(taken) / yrs:.0f}/yr), CAGR {e.iloc[-1] ** (1 / yrs) - 1:+.1%}, Sharpe {r_in.mean() / r_in.std() * np.sqrt(252):.2f}, "
          f"max DD {(e / e.cummax() - 1).min():+.1%}, days in market {(r_in != 0).mean():.0%}")
    df = pd.DataFrame({"scalp": base["orb"] + base["pre"], "swing": base["swing"]}).join(r_in, how="inner").fillna(0.0)
    print(f"overlap {df.index[0]:%Y-%m-%d} .. {df.index[-1]:%Y-%m-%d} ({len(df)} days); correlations:")
    print(df.corr().round(2).to_string())
    R = df[["scalp", "swing", "intraday"]].to_numpy()
    yi = df.index.year.to_numpy()
    print("\nmix (scalp/swing/intraday)       CAGR   Sharpe  maxDD   worst yr")
    rows = []
    for sc in (0.2, 0.25, 0.3, 0.35):
        for it in (0.0, 0.1, 0.2, 0.3):
            sw = round(1 - sc - it, 2)
            if sw < 0:
                continue
            w = np.array([sc, sw, it])
            acc = (1 + (R * w).sum(axis=1)).cumprod()  # daily-rebalanced approximation over the short overlap
            r = np.diff(np.concatenate([[1.0], acc])) / np.concatenate([[1.0], acc[:-1]])
            h = AO.hist_metrics(acc, yi)
            rows.append((sc, sw, it, h))
            print(f"  {sc:.0%} / {sw:.0%} / {it:.0%}               {h['CAGR']:+6.1%}  {h['Sharpe']:5.2f}  {h['maxDD']:+6.1%}  {h['worst yr']:+6.1%}")


if __name__ == "__main__":
    main()
