"""Does Korean investor flow (수급) predict returns after 2026 trading costs?

Replicates the core claims of Daishin's "수급의 정석, 구조와 알파" (Apr 2026) on Toss data
(Nov 2019 - Oct 2026), long-only (Korean retail can't practically short):
  - standalone participant signals by size bucket (5/20-day net buying, 5/20-day holds)
  - Strategy 2: other corporations ∩ private equity, mid caps, 5D/5D
  - Strategy 3: financial investment 4 consecutive weeks in the top 20, large caps
  - pension net-sell screen, and momentum × pension-sell (proxy for Strategy 1, which
    needs paid analyst-revision data)
plus the Korea -> US semiconductor link (Samsung/Hynix flows vs SMH/MU next US session).

Costs: commission 0.015% per side + 0.20% sell tax (2026) + slippage (0.05% large, 0.15% mid/small per side).

    python -m trader.research.kr_flow_study
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import DATA_DIR

KR = DATA_DIR / "research" / "kr"
RES = DATA_DIR / "research"
PARTS = ["individual", "foreigner", "institution", "other_corp", "fin_inv", "insurance", "trust", "pef", "pension"]
BUCKETS = {"large": (1, 100), "mid": (101, 300), "small": (301, 800)}
START = "2020-02-01"
TOP = 50  # basket size for standalone signals


def cost_round_trip(bucket: str) -> float:
    slip = 0.0005 if bucket == "large" else 0.0015
    return 2 * 0.00015 + 0.0020 + 2 * slip


def load():
    d = pd.read_parquet(KR / "kr_daily.parquet")
    d["date"] = pd.to_datetime(d["date"])
    d = d.dropna(subset=["close"]).drop_duplicates(["symbol", "date"]).sort_values(["symbol", "date"])
    uni = pd.read_parquet(KR / "universe.parquet").set_index("symbol")
    w = {k: d.pivot(index="date", columns="symbol", values=k) for k in ["open", "close", "volume", *PARTS]}
    w["mcap"] = w["close"] * uni["shares"].reindex(w["close"].columns)
    w["mrank"] = w["mcap"].rank(axis=1, ascending=False)
    for p in PARTS:
        w[p] = w[p].fillna(0) * w["close"]  # net buy value (KRW)
    return w


def bucket_mask(w, b):
    lo, hi = BUCKETS[b]
    return (w["mrank"] >= lo) & (w["mrank"] <= hi)


def basket_returns(w, signal: pd.DataFrame, mask: pd.DataFrame, hold: int, top: int, ascending=False,
                   pick_mask: pd.DataFrame | None = None, min_names: int | None = None) -> pd.DataFrame:
    """Non-overlapping rebalances every `hold` days: signal at close t, trade open t+1 -> open t+1+hold.
    `mask` is the benchmark universe (size bucket); `pick_mask` optionally restricts the picks."""
    o = w["open"]
    days = o.index[o.index >= START]
    rows, prev = [], set()
    for i in range(0, len(days) - hold - 1, hold):
        t, t1, t2 = days[i], days[i + 1], days[i + 1 + hold]
        allowed = mask.loc[t] if pick_mask is None else (mask.loc[t] & pick_mask.loc[t])
        s = signal.loc[t].where(allowed).dropna()
        if len(s) < (top * 2 if min_names is None else min_names):
            continue
        pick = s.sort_values(ascending=ascending).index[:top]
        fwd = (o.loc[t2] / o.loc[t1] - 1).where(mask.loc[t])
        fwd = fwd.clip(-0.9, 3.0)
        univ = fwd.dropna()
        basket = fwd[pick].dropna()
        turnover = 1 - len(prev & set(pick)) / max(len(pick), 1)
        prev = set(pick)
        rows.append({"date": t1, "basket": basket.mean(), "universe": univ.mean(), "turnover": turnover, "n": len(basket)})
    return pd.DataFrame(rows, columns=["date", "basket", "universe", "turnover", "n"]).set_index("date")


def summarize(r: pd.DataFrame, hold: int, bucket: str) -> dict:
    if len(r) < 5:
        return {k: float("nan") for k in ("excess_gross_ann", "t", "hit", "turnover", "cost_ann", "excess_net_ann", "cagr_net", "universe_cagr", "mdd_net")}
    per_year = 252 / hold
    ex = r["basket"] - r["universe"]
    net_basket = r["basket"] - r["turnover"] * cost_round_trip(bucket)
    eq = (1 + net_basket).cumprod()
    years = len(r) / per_year
    return {
        "excess_gross_ann": ex.mean() * per_year,
        "t": ex.mean() / ex.std() * np.sqrt(len(ex)) if ex.std() > 0 else 0.0,
        "hit": (ex > 0).mean(),
        "turnover": r["turnover"].mean(),
        "cost_ann": r["turnover"].mean() * cost_round_trip(bucket) * per_year,
        "excess_net_ann": (ex - r["turnover"] * cost_round_trip(bucket)).mean() * per_year,
        "cagr_net": eq.iloc[-1] ** (1 / years) - 1 if years > 0 else 0.0,
        "universe_cagr": (1 + r["universe"]).cumprod().iloc[-1] ** (1 / years) - 1,
        "mdd_net": (eq / eq.cummax() - 1).min(),
    }


def fmt(name, s):
    return (f"{name:46s} excess gross {s['excess_gross_ann']:+6.1%}/yr (t {s['t']:+.1f}, hit {s['hit']:.0%}) | turnover {s['turnover']:.0%} "
            f"cost {s['cost_ann']:5.1%}/yr | NET excess {s['excess_net_ann']:+6.1%}/yr, CAGR {s['cagr_net']:+.1%} vs universe {s['universe_cagr']:+.1%}, MDD {s['mdd_net']:.0%}")


def main():
    w = load()
    print(f"KR panel: {w['close'].shape[1]} stocks, {w['close'].index.min().date()} .. {w['close'].index.max().date()}\n")
    flows = {p: {L: w[p].rolling(L, min_periods=L).sum() for L in (5, 20)} for p in PARTS}

    print("== 1. Standalone participant signals (long-only top 50 net BUY vs bucket equal-weight) ==")
    for b in BUCKETS:
        m = bucket_mask(w, b)
        for p in PARTS:
            for L, H in ((5, 5), (20, 20)):
                r = basket_returns(w, flows[p][L], m, H, TOP)
                print(fmt(f"{b:5s} {p:11s} {L}D buy / hold {H}D", summarize(r, H, b)))
        print()

    print("== 2. Pension NET SELL screen (top 50 most sold, 20D, monthly) ==")
    for b in ("large", "mid"):
        r = basket_returns(w, flows["pension"][20], bucket_mask(w, b), 20, TOP, ascending=True)
        print(fmt(f"{b} pension 20D net sell / hold 20D", summarize(r, 20, b)))

    print("\n== 3. Report strategies (long-only) ==")
    mid = bucket_mask(w, "mid")
    oc, pef = flows["other_corp"][5], flows["pef"][5]
    oc_top = oc.where(mid).rank(axis=1, ascending=False) <= 50
    pef_top = pef.where(mid).rank(axis=1, ascending=False) <= 50
    inter = (oc_top & pef_top)
    score2 = (oc.where(inter) + pef.where(inter))  # rank inside the intersection
    r2 = basket_returns(w, score2, mid, 5, 15, pick_mask=inter, min_names=1)
    print(fmt("S2 other corp ∩ PEF, mid caps, 5D/5D (<=15 names)", summarize(r2, 5, "mid")))
    large = bucket_mask(w, "large")
    fin_w = w["fin_inv"].rolling(5).sum()
    rk = fin_w.where(large).rank(axis=1, ascending=False) <= 20
    persist = rk & rk.shift(5).fillna(False).astype(bool) & rk.shift(10).fillna(False).astype(bool) & rk.shift(15).fillna(False).astype(bool)
    r3 = basket_returns(w, fin_w.where(persist), large, 5, 10, pick_mask=persist, min_names=1)
    print(fmt("S3 fin. investment 4 weeks in top 20, large, weekly", summarize(r3, 5, "large")))
    mom = w["close"].shift(21) / w["close"].shift(252) - 1
    for b in ("large", "mid"):
        m = bucket_mask(w, b)
        mom_top = mom.where(m).rank(axis=1, ascending=False) <= 50
        sell_top = flows["pension"][20].where(m).rank(axis=1, ascending=True) <= 50
        both = mom_top & sell_top
        r1 = basket_returns(w, mom.where(both), m, 20, 20, pick_mask=both, min_names=1)
        print(fmt(f"S1-proxy momentum ∩ pension sell, {b}, monthly", summarize(r1, 20, b)))
        rm = basket_returns(w, mom, m, 20, 20)
        print(fmt(f"   (momentum alone, {b}, monthly, top 20)", summarize(rm, 20, b)))

    print("\n== 4. Korea -> US semiconductors ==")
    etf = pd.read_parquet(RES / "etf_daily.parquet")
    us = pd.concat([etf, pd.read_parquet(RES / "daily_long.parquet", columns=["symbol", "date", "open", "close"])])
    us = us[us["symbol"].isin(["SMH", "MU", "NVDA", "SOXX"])].drop_duplicates(["symbol", "date"])
    uo = us.pivot(index="date", columns="symbol", values="open")
    uc = us.pivot(index="date", columns="symbol", values="close")
    oc_ret = uc / uo - 1  # US regular session (starts after the Korean close on the same date)
    gap = uo / uc.shift(1) - 1  # prior US close -> US open (overlaps the Korean session)
    semis = ["005930", "000660"]
    vol20 = w["volume"][semis].rolling(20).mean()
    f_norm = (w["foreigner"][semis] / w["close"][semis] / vol20).mean(axis=1)  # foreign net buy as % of normal volume
    kr_ret = (w["close"][semis] / w["open"][semis] - 1).mean(axis=1)
    df = pd.DataFrame({"kr_foreign": f_norm, "kr_session": kr_ret}).join(oc_ret.add_suffix("_us_oc"), how="inner").join(gap.add_suffix("_gap"), how="inner")
    df = df.loc[START:].dropna()
    for sig in ("kr_foreign", "kr_session"):
        for tgt in ("SMH_gap", "SMH_us_oc", "MU_us_oc", "NVDA_us_oc"):
            r = df[[sig, tgt]].corr(method="spearman").iloc[0, 1]
            print(f"  {sig:11s} vs {tgt:11s} rank corr {r:+.3f} (t {r * np.sqrt(len(df) - 2):+.1f}, n={len(df)})")
    q = df["kr_foreign"].quantile([0.2, 0.8])
    hi, lo = df[df["kr_foreign"] >= q.iloc[1]], df[df["kr_foreign"] <= q.iloc[0]]
    print(f"  SMH US session after strongest 20% foreign buying of Samsung+Hynix: {hi['SMH_us_oc'].mean():+.3%}/day "
          f"vs weakest 20%: {lo['SMH_us_oc'].mean():+.3%}/day; all days {df['SMH_us_oc'].mean():+.3%}")
    rule = np.where(df["kr_foreign"] >= q.iloc[1], df["SMH_us_oc"] - 0.002, 0.0)  # buy SMH at US open, sell at close, Toss 0.1%/side
    eq = (1 + pd.Series(rule, index=df.index)).cumprod()
    print(f"  Rule 'buy SMH at US open after heavy KR foreign buying, sell at close' after Toss fees: total {eq.iloc[-1] - 1:+.1%} over {len(df)} days")


if __name__ == "__main__":
    main()
