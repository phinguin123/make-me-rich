"""Does a machine-learning stock ranker beat the momentum ranking? Walk-forward test.

At each month end, a LightGBM model ranks the same eligible universe the swing sleeve uses
(1,000 most-traded stocks, price >= $10, above the 200-day, volatility <= 60%). It's trained
only on months whose next-month returns were already known. Variants:
  expanding  - retrained every month on all history ("keeps learning")
  rolling36  - retrained every month on the last 36 months only (adapts faster)
  static     - trained once on 2019-2020 and never updated
The scores feed the same portfolio backtester (top 10, monthly, 20% vol target, Toss costs).

    python -m trader.research.ml_ranker
"""
from __future__ import annotations

from dataclasses import replace

import lightgbm as lgb
import numpy as np
import pandas as pd

from .portfolio import PP, RES, backtest, load, stats

FEATURES = ["r21", "r63", "r126", "r252", "mom12_1", "rs", "vol21", "vol63", "hi52_dist", "px_sma50", "px_sma200",
            "sma50_200", "dv_rank", "dv_trend", "max21", "beta126", "rev5"]


def month_ends(days: pd.DatetimeIndex) -> list[pd.Timestamp]:
    s = pd.Series(days, index=days)
    return list(s.groupby(s.dt.to_period("M")).max())[:-1]  # drop the incomplete final month


def build_panel(w: dict) -> pd.DataFrame:
    c, v = w["close"], w["volume"]
    r = c.pct_change(fill_method=None)
    spy = r["SPY"]
    feats = {
        "r21": c.pct_change(21, fill_method=None), "r63": c.pct_change(63, fill_method=None),
        "r126": c.pct_change(126, fill_method=None), "r252": c.pct_change(252, fill_method=None),
        "mom12_1": w["mom12_1"], "rs": w["rs"], "vol21": r.rolling(21).std(), "vol63": w["vol63"],
        "hi52_dist": c / w["hi52"], "px_sma50": c / w["sma50"], "px_sma200": c / w["sma200"],
        "sma50_200": w["sma50"] / w["sma200"], "dv_rank": w["dv50"].rank(axis=1, pct=True),
        "dv_trend": (c * v).rolling(20).mean() / (c * v).rolling(100).mean(), "max21": r.rolling(21).max(),
        "beta126": r.rolling(126).cov(spy).div(spy.rolling(126).var(), axis=0), "rev5": c.pct_change(5, fill_method=None),
    }
    ends = month_ends(w["days"])
    fwd = c.shift(-21) / c - 1  # next ~month return from the signal close
    elig = (c >= 10) & (w["dv50"] >= 30e6) & (c > w["sma200"]) & (w["vol63"] <= 0.6)
    elig &= np.broadcast_to(w["is_stock"].to_numpy(), elig.shape)
    dv = w["dv50"].where(np.broadcast_to(w["is_stock"].to_numpy(), elig.shape))
    elig &= dv.rank(axis=1, ascending=False) <= 1000
    rows = []
    for d in ends:
        m = elig.loc[d]
        syms = m.index[m.to_numpy()]
        if len(syms) < 50:
            continue
        df = pd.DataFrame({k: f.loc[d, syms] for k, f in feats.items()})
        df = df.rank(pct=True)  # cross-sectional ranks (robust to regime-level shifts)
        df["fwd"] = fwd.loc[d, syms].to_numpy()
        df["label"] = df["fwd"].rank(pct=True)
        df["date"], df["symbol"] = d, syms
        rows.append(df)
    return pd.concat(rows, ignore_index=True)


def walk_forward(panel: pd.DataFrame, mode: str, first_test: str = "2021-01-01") -> pd.DataFrame:
    dates = sorted(panel["date"].unique())
    test_dates = [d for d in dates if d >= pd.Timestamp(first_test)]
    params = dict(objective="regression", learning_rate=0.03, num_leaves=15, min_data_in_leaf=200, bagging_fraction=0.8,
                  bagging_freq=1, feature_fraction=0.8, lambda_l2=5.0, verbose=-1, num_threads=8, seed=7)

    def fit(df):
        return lgb.train(params, lgb.Dataset(df[FEATURES], df["label"]), num_boost_round=300)
    out, static_model = [], None
    for d in test_dates:
        i = dates.index(d)
        train_dates = dates[: i - 1] if i >= 2 else []  # labels of month i-1 are realized only at d
        if mode == "rolling36":
            train_dates = train_dates[-36:]
        if mode == "static":
            if static_model is None:
                tr = panel[panel["date"] < pd.Timestamp("2021-01-01") - pd.Timedelta(days=31)]
                static_model = fit(tr.dropna(subset=["label"]))
            model = static_model
        else:
            tr = panel[panel["date"].isin(train_dates)].dropna(subset=["label"])
            model = fit(tr)
        te = panel[panel["date"] == d].copy()
        te["score"] = model.predict(te[FEATURES])
        out.append(te[["date", "symbol", "score", "fwd", "rs"]])
    return pd.concat(out, ignore_index=True)


def ic_report(pred: pd.DataFrame, col: str) -> tuple[float, float, float]:
    ics = pred.groupby("date").apply(lambda g: g[col].corr(g["fwd"], method="spearman"), include_groups=False).dropna()
    top = pred.groupby("date").apply(lambda g: g.nlargest(10, col)["fwd"].mean() - g["fwd"].mean(), include_groups=False)
    return float(ics.mean()), float(ics.mean() / ics.std() * np.sqrt(len(ics))), float(top.mean())


if __name__ == "__main__":
    w = load()
    panel = build_panel(w)
    print(f"panel: {len(panel):,} stock-months, {panel['date'].nunique()} months", flush=True)
    base = replace(PP(), start="2021-01-01")
    eq, tr = backtest(w, base)
    s = stats(eq, tr, base.capital)
    print(f"{'momentum (RS) — current':26s} CAGR {s['cagr']:+.1%} Sharpe {s['sharpe']:.2f} DD {s['max_dd']:.0%} | "
          + " ".join(f"{k[1:]}:{v:+.0%}" for k, v in s.items() if k.startswith("y")), flush=True)
    for mode in ("expanding", "rolling36", "static"):
        pred = walk_forward(panel, mode)
        ic, t, top = ic_report(pred, "score")
        ic_rs, t_rs, top_rs = ic_report(pred, "rs")
        sc = pred.pivot(index="date", columns="symbol", values="score").reindex(index=w["days"], columns=w["close"].columns)
        w2 = dict(w)
        w2["ml"] = sc
        eq, tr = backtest(w2, replace(base, score="ml"))
        s = stats(eq, tr, base.capital)
        print(f"ML {mode:23s} CAGR {s['cagr']:+.1%} Sharpe {s['sharpe']:.2f} DD {s['max_dd']:.0%} | "
              + " ".join(f"{k[1:]}:{v:+.0%}" for k, v in s.items() if k.startswith("y"))
              + f"\n   rank IC {ic:+.3f} (t {t:+.1f}), top-10 excess/month {top:+.2%} | momentum IC {ic_rs:+.3f} (t {t_rs:+.1f}), top-10 excess {top_rs:+.2%}", flush=True)
        pred.to_parquet(RES / f"ml_pred_{mode}.parquet")
