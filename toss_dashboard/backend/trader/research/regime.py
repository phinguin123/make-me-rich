"""Which market signals preceded drawdowns? Forward SPY/SMH returns and drawdown odds
conditional on trend, breadth, rates, credit, volatility and dollar signals (2016-2026),
plus today's readings.

    python -m trader.research.regime
"""
import pandas as pd, numpy as np
from trader.config import DATA_DIR
R = DATA_DIR/"research"
etf = pd.read_parquet(R/"etf_daily.parquet"); px = etf.pivot(index="date", columns="symbol", values="close")
m = pd.read_parquet(R/"macro_daily.parquet")
st = pd.read_parquet(R/"daily_long.parquet", columns=["symbol","date","close","volume"])
st = st[st.date >= "2016-01-01"]
st["dv"] = st.close*st.volume
st = st.sort_values(["symbol","date"])
g = st.groupby("symbol")["close"]
st["sma200"] = g.transform(lambda s: s.rolling(200, min_periods=150).mean())
st["sma50"] = g.transform(lambda s: s.rolling(50).mean())
st["dv50"] = st.groupby("symbol")["dv"].transform(lambda s: s.rolling(50, min_periods=20).mean())
liq = st[(st.dv50 >= 20e6) & st.sma200.notna()]
breadth = pd.DataFrame({"b200": liq.assign(a=liq.close>liq.sma200).groupby("date").a.mean(),
                        "b50": liq.assign(a=liq.close>liq.sma50).groupby("date").a.mean()})
df = pd.DataFrame(index=px.index)
for s in ["SPY","SMH","QQQ","TLT","HYG"]: df[s] = px[s]
df = df.join(breadth.reindex(df.index).ffill()).join(m.reindex(df.index, method="ffill"))
spy, smh = df.SPY, df.SMH
f = pd.DataFrame(index=df.index)
for name, s in [("spy", spy), ("smh", smh)]:
    f[f"{name}_f21"] = s.shift(-21)/s-1; f[f"{name}_f63"] = s.shift(-63)/s-1
    fut_min = s[::-1].rolling(63, min_periods=20).min()[::-1].shift(-1)
    f[f"{name}_dd63"] = fut_min/s-1
sig = pd.DataFrame(index=df.index)
sig["SPY<200dma"] = spy < spy.rolling(200).mean()
sig["SPY<50dma"] = spy < spy.rolling(50).mean()
sig["SMH<50dma"] = smh < smh.rolling(50).mean()
sig["SMH>35% above 200dma"] = smh/smh.rolling(200).mean()-1 > 0.35
sig["breadth200<50% & SPY near high"] = (df.b200 < 0.5) & (spy >= 0.97*spy.rolling(252).max())
sig["breadth50 falling 20d>15pts"] = df.b50.diff(20) < -0.15
sig["10y +50bp in 63d"] = df.DGS10.diff(63) > 0.5
sig["10y > 5%"] = df.DGS10 > 5.0
sig["HY spread +50bp in 21d"] = df.BAMLH0A0HYM2.diff(21) > 0.5
sig["VIX > VIX3M (backwardation)"] = df["^VIX"] > df["^VIX3M"]
sig["VIX < 13 (complacency)"] = df["^VIX"] < 13
sig["dollar +3% in 63d"] = df["DX-Y.NYB"].pct_change(63) > 0.03
sig["MOVE > 120"] = df["^MOVE"] > 120
sig["curve 10y-3m < 0"] = df.T10Y3M < 0
start = "2016-06-01"
f, sig = f.loc[start:], sig.loc[start:]
rows=[]
base = {c: f[c].mean() for c in f}; p10 = (f.spy_dd63 <= -0.10).mean(); p15 = (f.smh_dd63 <= -0.15).mean()
for c in sig:
    on = sig[c].fillna(False).astype(bool)
    if on.sum() < 20: rows.append((c, int(on.sum()))); continue
    x = f[on]
    rows.append((c, int(on.sum()), x.spy_f21.mean(), x.spy_f63.mean(), (x.spy_dd63<=-0.10).mean(), x.smh_f21.mean(), x.smh_f63.mean(), (x.smh_dd63<=-0.15).mean()))
print(f"BASE (all days) n={len(f)}  SPY f21 {base['spy_f21']:+.2%} f63 {base['spy_f63']:+.2%} P(SPY -10% within 3m) {p10:.0%} | SMH f21 {base['smh_f21']:+.2%} f63 {base['smh_f63']:+.2%} P(SMH -15% in 3m) {p15:.0%}")
print(f"{'signal':34s} {'days':>5s} {'SPYf21':>7s} {'SPYf63':>7s} {'P-10%':>6s} {'SMHf21':>7s} {'SMHf63':>7s} {'P-15%':>6s}")
for r in rows:
    if len(r)==2: print(f"{r[0]:34s} {r[1]:5d}  (too few)"); continue
    print(f"{r[0]:34s} {r[1]:5d} {r[2]:+7.2%} {r[3]:+7.2%} {r[4]:6.0%} {r[5]:+7.2%} {r[6]:+7.2%} {r[7]:6.0%}")
last = df.iloc[-1]; sl = sig.iloc[-1]
print("\nCURRENT READINGS", df.index[-1].date())
print(f" SPY {last.SPY:.2f} vs 50dma {spy.rolling(50).mean().iloc[-1]:.2f} 200dma {spy.rolling(200).mean().iloc[-1]:.2f}; off 52w high {last.SPY/spy.rolling(252).max().iloc[-1]-1:+.1%}")
print(f" SMH {last.SMH:.2f}: {smh.iloc[-1]/smh.rolling(200).mean().iloc[-1]-1:+.1%} above 200dma, {smh.iloc[-1]/smh.rolling(50).mean().iloc[-1]-1:+.1%} vs 50dma; 3m return {smh.pct_change(63).iloc[-1]:+.1%}, 12m {smh.pct_change(252).iloc[-1]:+.1%}")
print(f" breadth: {last.b200:.0%} of liquid stocks above 200dma, {last.b50:.0%} above 50dma (20d change {df.b50.diff(20).iloc[-1]*100:+.0f} pts)")
print(f" 10y {last.DGS10:.2f}% (63d {df.DGS10.diff(63).iloc[-1]*100:+.0f}bp, 252d {df.DGS10.diff(252).iloc[-1]*100:+.0f}bp) 2y {last.DGS2:.2f}% curve10y-3m {last.T10Y3M:+.2f}")
print(f" HY spread {last.BAMLH0A0HYM2:.2f}% (21d {df.BAMLH0A0HYM2.diff(21).iloc[-1]*100:+.0f}bp; percentile since 2016 {(df.BAMLH0A0HYM2.loc[start:]<last.BAMLH0A0HYM2).mean():.0%})")
print(f" VIX {last['^VIX']:.1f} VIX3M {last['^VIX3M']:.1f} MOVE {last['^MOVE']:.0f} DXY {last['DX-Y.NYB']:.1f} (63d {df['DX-Y.NYB'].pct_change(63).iloc[-1]:+.1%}) oil {last['CL=F']:.1f} (63d {df['CL=F'].pct_change(63).iloc[-1]:+.1%}) gold {last['GC=F']:.0f} (63d {df['GC=F'].pct_change(63).iloc[-1]:+.1%})")
print(" signals ON now:", [c for c in sig if bool(sl[c])])
df.to_parquet(R/"regime_frame.parquet")
