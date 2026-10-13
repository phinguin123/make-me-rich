"""Insider cluster buying: a retail-capacity edge (mostly small/mid caps funds can't use).

Data: SEC "Insider Transactions Data Sets" (every Form 4, quarterly bulk files, free).
Signal: open-market purchases (code P) by officers/directors, known on the filing date.
  cluster - >= 3 distinct insiders bought within 30 days
  pair    - >= 2 insiders and >= $250k total within 30 days
  ceo     - CEO/CFO/President bought >= $100k
Trade: buy at the next open, hold N trading days, equal-weight slots, small-cap costs.

    python -m trader.research.insider download
    python -m trader.research.insider
"""
from __future__ import annotations

import io
import math
import sys
import time
import zipfile
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd
import requests

from ..config import DATA_DIR

RES = DATA_DIR / "research"
DIR = RES / "insider"
UA = {"User-Agent": "make-me-rich research bot (personal non-commercial use)"}
URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{y}q{q}_form345.zip"


def download(first=(2017, 4), last=(2026, 3)) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    y, q = first
    while (y, q) <= last:
        path = DIR / f"{y}q{q}.parquet"
        if not path.exists():
            r = requests.get(URL.format(y=y, q=q), headers=UA, timeout=120)
            if r.status_code == 200:
                parse(r.content).to_parquet(path)
                print(f"{y}q{q}: ok", flush=True)
            else:
                print(f"{y}q{q}: HTTP {r.status_code}", flush=True)
            time.sleep(1.0)
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)


def parse(blob: bytes) -> pd.DataFrame:
    z = zipfile.ZipFile(io.BytesIO(blob))
    rd = lambda n, cols: pd.read_csv(z.open(n), sep="\t", usecols=cols, dtype=str, on_bad_lines="skip", quoting=3)  # noqa: E731
    sub = rd("SUBMISSION.tsv", ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERTRADINGSYMBOL"])
    own = rd("REPORTINGOWNER.tsv", ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"])
    tr = rd("NONDERIV_TRANS.tsv", ["ACCESSION_NUMBER", "TRANS_CODE", "TRANS_SHARES", "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"])
    tr = tr[(tr["TRANS_CODE"] == "P") & (tr["TRANS_ACQUIRED_DISP_CD"] == "A")].copy()
    tr["value"] = pd.to_numeric(tr["TRANS_SHARES"], errors="coerce") * pd.to_numeric(tr["TRANS_PRICEPERSHARE"], errors="coerce")
    tr = tr.groupby("ACCESSION_NUMBER", as_index=False)["value"].sum()
    own = own.drop_duplicates("ACCESSION_NUMBER")
    df = tr.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER")
    df = df[df["DOCUMENT_TYPE"].isin(["4", "4/A"]) & (df["value"] > 0)]
    rel = df["RPTOWNER_RELATIONSHIP"].fillna("")
    df = df[rel.str.contains("Director|Officer", case=False)]  # exclude pure 10% holders (funds)
    df["date"] = pd.to_datetime(df["FILING_DATE"], format="%d-%b-%Y", errors="coerce")
    df["symbol"] = df["ISSUERTRADINGSYMBOL"].str.upper().str.strip()
    title = df["RPTOWNER_TITLE"].fillna("").str.upper()
    df["top_exec"] = title.str.contains(r"\bCEO\b|CHIEF EXECUTIVE|\bCFO\b|CHIEF FINANCIAL|PRESIDENT")
    return df[["date", "symbol", "RPTOWNERCIK", "value", "top_exec"]].rename(columns={"RPTOWNERCIK": "owner"}).dropna(subset=["date", "symbol"])


def signals(buys: pd.DataFrame) -> pd.DataFrame:
    """One row per (symbol, filing date) with trailing-30-day insider counts and value."""
    out = []
    for sym, g in buys.sort_values("date").groupby("symbol"):
        dates = g["date"].to_numpy()
        for d in np.unique(dates):
            win = g[(g["date"] > d - np.timedelta64(30, "D")) & (g["date"] <= d)]
            today = g[g["date"] == d]
            out.append((sym, d, win["owner"].nunique(), win["value"].sum(), bool((today["top_exec"] & (today["value"] >= 100_000)).any())))
    s = pd.DataFrame(out, columns=["symbol", "date", "n_insiders", "value30", "ceo_buy"])
    s["cluster"] = s["n_insiders"] >= 3
    s["pair"] = (s["n_insiders"] >= 2) & (s["value30"] >= 250_000)
    return s


@dataclass(frozen=True)
class IP:
    signal: str = "cluster"  # cluster | pair | ceo | any
    hold: int = 63
    slots: int = 15
    min_price: float = 3.0
    min_dollar_vol: float = 1e6
    commission: float = 0.001
    slip: float = 0.005
    capital: float = 5000.0
    start: str = "2018-03-01"


def load_prices():
    d = pd.read_parquet(RES / "daily_long.parquet", columns=["symbol", "date", "open", "close", "volume"])
    o = d.pivot(index="date", columns="symbol", values="open").astype("float32")
    c = d.pivot(index="date", columns="symbol", values="close").astype("float32")
    dv = (c * d.pivot(index="date", columns="symbol", values="volume")).rolling(20, min_periods=10).mean()
    return o, c, dv


def backtest(sig: pd.DataFrame, px, p: IP) -> tuple[pd.Series, pd.DataFrame]:
    o, c, dv = px
    days = c.index[c.index >= p.start]
    col = {s: i for i, s in enumerate(c.columns)}
    O, C, DV = o.loc[days].to_numpy(), c.loc[days].to_numpy(), dv.loc[days].to_numpy()
    mask = {"cluster": sig["cluster"], "pair": sig["pair"] | sig["cluster"], "ceo": sig["ceo_buy"], "any": sig["cluster"] | sig["pair"] | sig["ceo_buy"]}[p.signal]
    s = sig[mask & sig["symbol"].isin(col)]
    pos_day = np.searchsorted(days.to_numpy(), s["date"].to_numpy(), side="right")  # first trading day AFTER the filing date
    queue: dict[int, list[int]] = {}
    for t, sym in zip(pos_day, s["symbol"]):
        queue.setdefault(int(t), []).append(col[sym])
    cash, pos, curve, trades, last = p.capital, {}, [], [], {}
    for t in range(len(days)):
        # exits at the open after the holding period
        for i in [i for i, x in pos.items() if t - x["t"] >= p.hold]:
            px_ = O[t][i] if np.isfinite(O[t][i]) else last.get(i, pos[i]["entry"])
            proceeds = pos[i]["q"] * px_ * (1 - p.slip) * (1 - p.commission)
            cash += proceeds
            trades.append((days[t], c.columns[i], proceeds / pos[i]["cost"] - 1))
            del pos[i]
        equity = cash + sum(x["q"] * last.get(i, x["entry"]) for i, x in pos.items())
        for i in queue.get(t, []):
            if i in pos or len(pos) >= p.slots or t == 0:
                continue
            ref, liq = C[t - 1][i], DV[t - 1][i]
            if not (np.isfinite(O[t][i]) and np.isfinite(ref) and ref >= p.min_price and liq >= p.min_dollar_vol):
                continue
            amount = min(equity / p.slots, cash)
            if amount < 20:
                continue
            fill = O[t][i] * (1 + p.slip)
            pos[i] = {"q": amount / (fill * (1 + p.commission)), "entry": fill, "cost": amount, "t": t}
            cash -= amount
        for i in pos:
            if np.isfinite(C[t][i]):
                last[i] = float(C[t][i])
        curve.append((days[t], cash + sum(x["q"] * last.get(i, x["entry"]) for i, x in pos.items())))
    return pd.Series(dict(curve)), pd.DataFrame(trades, columns=["date", "symbol", "ret"])


def stats(eq: pd.Series, tr: pd.DataFrame, capital: float) -> dict:
    r = eq.pct_change().fillna(0)
    y = eq.resample("YE").last()
    y = y / y.shift(1).fillna(capital) - 1
    split = pd.Timestamp("2023-01-01")
    sh = lambda x: float(x.mean() / x.std() * np.sqrt(252)) if x.std() > 0 else 0.0  # noqa: E731
    return {"cagr": float((eq.iloc[-1] / capital) ** (252 / len(eq)) - 1), "sharpe": sh(r), "sh_is": sh(r[r.index < split]),
            "sh_oos": sh(r[r.index >= split]), "max_dd": float((eq / eq.cummax() - 1).min()), "trades": len(tr),
            "win": float((tr["ret"] > 0).mean()) if len(tr) else 0.0, "avg": float(tr["ret"].mean()) if len(tr) else 0.0,
            **{f"y{k.year}": round(float(v), 3) for k, v in y.items()}}


if __name__ == "__main__":
    if sys.argv[1:] == ["download"]:
        download()
        sys.exit()
    buys = pd.concat([pd.read_parquet(f) for f in sorted(DIR.glob("*.parquet"))], ignore_index=True)
    sig = signals(buys)
    sig.to_parquet(RES / "insider_signals.parquet")
    print(f"{len(buys):,} insider purchase filings; signals: cluster {sig.cluster.sum()}, pair {sig.pair.sum()}, ceo {sig.ceo_buy.sum()}")
    px = load_prices()
    for p in [IP(signal=s, hold=h, slots=n) for s in ("cluster", "pair", "ceo", "any") for h in (21, 63, 126) for n in (10, 20)]:
        eq, tr = backtest(sig, px, p)
        st = stats(eq, tr, p.capital)
        print(f"{p.signal:7s} hold {p.hold:3d} slots {p.slots:2d}: CAGR {st['cagr']:+.1%} Sh {st['sharpe']:.2f} (IS {st['sh_is']:.2f}/OOS {st['sh_oos']:.2f}) "
              f"DD {st['max_dd']:.0%} trades {st['trades']} win {st['win']:.0%} avg {st['avg']:+.1%} | "
              + " ".join(f"{k[1:]}:{v:+.0%}" for k, v in st.items() if k.startswith("y")), flush=True)
