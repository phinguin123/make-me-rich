"""Optimal split between the scalp sleeve and the swing sleeve.

Parametrized by money, as fractions of the account:
  e = scalp pot (each scalp trade uses 100% of it, so e is also the trade size)
  g = swing pot, and 1 - e - g idle cash (earns nothing)
Holding scalp cash that trades at 50% is the same as a smaller pot at 100% plus idle cash, so
this grid covers every setting of TRADER_SCALP_CAPITAL / _POSITION_FRAC / TRADER_SWING_CAPITAL.

Evaluated on (1) the 2021-2026 history with yearly rebalancing to target, (2) 4,000 block-
bootstrap paths of 1 and 3 years (21-day blocks of the joint daily returns), (3) bootstrap
re-optimization (how stable the "best" mix is), and (4) haircuts: what if the scalp or swing
edge is half as good live as in the backtest.

    python -m trader.research.allocation_opt
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import DATA_DIR
from .allocation import ALLOWANCE, TAX, sleeve_returns

CACHE = DATA_DIR / "research" / "allocation_daily.parquet"
GRID = np.round(np.arange(0, 1.0001, 0.05), 2)


def load() -> pd.DataFrame:
    if CACHE.exists():
        return pd.read_parquet(CACHE)
    df = sleeve_returns()
    df.to_parquet(CACHE)
    return df


def paths(rs: np.ndarray, rw: np.ndarray, e: float, g: float, year_len: int = 252) -> np.ndarray:
    """Account value (start 1.0) for arrays of daily returns shaped (paths, days); yearly rebalance."""
    n, T = rs.shape
    out = np.empty((n, T))
    v = np.ones(n)
    for a in range(0, T, year_len):
        b = min(T, a + year_len)
        S = np.cumprod(1 + rs[:, a:b], axis=1)
        W = np.cumprod(1 + rw[:, a:b], axis=1)
        out[:, a:b] = v[:, None] * (e * S + g * W + (1 - e - g))
        v = out[:, b - 1]
    return out


def after_tax_cagr(eq: np.ndarray, years: np.ndarray, capital: float = 12400.0) -> float:
    """Single history: 22% of each calendar year's gain above the allowance, paid at year end."""
    val, paid = capital, 0.0
    start_val = capital
    vals = eq * capital
    for y in np.unique(years):
        idx = np.flatnonzero(years == y)
        end = vals[idx[-1]] - paid
        tax = TAX * max(0.0, end - start_val - ALLOWANCE)
        paid += tax
        start_val = end - tax
    return (start_val / capital) ** (252 / len(eq)) - 1


def hist_metrics(eq: np.ndarray, years: np.ndarray) -> dict:
    r = np.diff(np.concatenate([[1.0], eq])) / np.concatenate([[1.0], eq[:-1]])
    yearly = []
    prev = 1.0
    for y in np.unique(years):
        end = eq[np.flatnonzero(years == y)[-1]]
        yearly.append(end / prev - 1)
        prev = end
    return {"CAGR": eq[-1] ** (252 / len(eq)) - 1, "after tax": after_tax_cagr(eq, years),
            "Sharpe": r.mean() / r.std() * np.sqrt(252) if r.std() else 0.0,
            "maxDD": (eq / np.maximum.accumulate(eq) - 1).min(), "worst yr": min(yearly)}


def boot_sample(rs: np.ndarray, rw: np.ndarray, years: int, n: int, block: int = 21, seed: int = 5):
    rng = np.random.default_rng(seed)
    T = 252 * years
    starts = rng.integers(0, len(rs) - block, size=(n, T // block + 1))
    idx = (starts[:, :, None] + np.arange(block)).reshape(n, -1)[:, :T]
    return rs[idx], rw[idx]


def boot_metrics(eq: np.ndarray) -> dict:
    r = eq[:, -1] - 1
    dd = (eq / np.maximum.accumulate(eq, axis=1) - 1).min(axis=1)
    return {"median": np.median(r), "p5": np.quantile(r, 0.05), "ploss": (r < 0).mean(), "dd_med": np.median(dd), "dd_p5": np.quantile(dd, 0.05)}


def frontier_table(rs, rw, years_idx, label: str) -> pd.DataFrame:
    """Fully invested line (g = 1 - e) on history + bootstrap."""
    b1s, b1w = boot_sample(rs, rw, 1, 4000)
    b3s, b3w = boot_sample(rs, rw, 3, 4000, seed=6)
    rows = []
    for e in GRID:
        g = round(1 - e, 2)
        h = hist_metrics(paths(rs[None, :], rw[None, :], e, g)[0], years_idx)
        m1, m3 = boot_metrics(paths(b1s, b1w, e, g)), boot_metrics(paths(b3s, b3w, e, g))
        rows.append({"scalp": e, "swing": g, **h, "1y med": m1["median"], "1y p5": m1["p5"], "1y P(loss)": m1["ploss"],
                     "1y DD p5": m1["dd_p5"], "3y med": m3["median"], "3y p5": m3["p5"], "3y DD p5": m3["dd_p5"],
                     "3y growth": np.median(np.log1p(paths(b3s, b3w, e, g)[:, -1] - 1)) / 3})
    t = pd.DataFrame(rows)
    t.attrs["label"] = label
    return t


def show(t: pd.DataFrame) -> None:
    print(f"\n=== {t.attrs['label']} (fully invested: swing = 1 - scalp; yearly rebalance) ===")
    fmt = t.copy()
    for c in ("scalp", "swing"):
        fmt[c] = fmt[c].map("{:.0%}".format)
    for c in ("CAGR", "after tax", "maxDD", "worst yr", "1y med", "1y p5", "1y DD p5", "3y med", "3y p5", "3y DD p5"):
        fmt[c] = fmt[c].map("{:+.1%}".format)
    fmt["Sharpe"] = fmt["Sharpe"].map("{:.2f}".format)
    fmt["1y P(loss)"] = fmt["1y P(loss)"].map("{:.0%}".format)
    print(fmt.drop(columns=["3y growth"]).to_string(index=False))
    best = {
        "max Sharpe (history)": t.loc[t["Sharpe"].idxmax()],
        "max median growth (bootstrap 3y, Kelly-like)": t.loc[t["3y growth"].idxmax()],
        "max 3y median with 5% worst-case 1y >= -10%": t[t["1y p5"] >= -0.10].sort_values("3y med").iloc[-1] if (t["1y p5"] >= -0.10).any() else None,
        "max 3y median with 5% worst-case 3y drawdown >= -30%": t[t["3y DD p5"] >= -0.30].sort_values("3y med").iloc[-1] if (t["3y DD p5"] >= -0.30).any() else None,
        "best bad case: max 3y 5th percentile": t.loc[t["3y p5"].idxmax()],
    }
    for k, row in best.items():
        if row is not None:
            print(f"  {k:55s} -> scalp {row['scalp']:.0%} / swing {row['swing']:.0%}")


def reoptimize(rs, rw, n: int = 2000) -> None:
    """How stable is the max-Sharpe and max-growth mix? Re-optimize on each bootstrap 3-year path."""
    bs, bw = boot_sample(rs, rw, 3, n, seed=9)
    sharpe, growth = np.empty((n, len(GRID))), np.empty((n, len(GRID)))
    for j, e in enumerate(GRID):
        eq = paths(bs, bw, e, 1 - e)
        r = np.diff(eq, axis=1) / eq[:, :-1]
        sharpe[:, j] = r.mean(axis=1) / r.std(axis=1) * np.sqrt(252)
        growth[:, j] = np.log(eq[:, -1])
    for name, arr in (("max Sharpe", sharpe), ("max growth", growth)):
        best = GRID[arr.argmax(axis=1)]
        q = np.quantile(best, [0.1, 0.25, 0.5, 0.75, 0.9])
        print(f"  {name:10s}: best scalp share across paths  10th {q[0]:.0%}  25th {q[1]:.0%}  median {q[2]:.0%}  75th {q[3]:.0%}  90th {q[4]:.0%}")


def main() -> None:
    df = load()
    rs = (df["orb"] + df["pre"]).to_numpy()  # scalp pot return per day at 100% per trade
    rw = df["swing"].to_numpy()
    years_idx = df.index.year.to_numpy()
    print(f"{df.index[0]:%Y-%m} .. {df.index[-1]:%Y-%m}")
    for name, x in (("scalp @100%", rs), ("swing", rw)):
        print(f"  {name:12s} mean {x.mean() * 252:+.1%}/yr  vol {x.std() * np.sqrt(252):.1%}  Sharpe {x.mean() / x.std() * np.sqrt(252):.2f}")
    print(f"  correlation {np.corrcoef(rs, rw)[0, 1]:+.3f}")
    mu, sd = np.array([rs.mean(), rw.mean()]) * 252, np.array([rs.std(), rw.std()]) * np.sqrt(252)
    rho = np.corrcoef(rs, rw)[0, 1]
    cov = np.array([[sd[0] ** 2, rho * sd[0] * sd[1]], [rho * sd[0] * sd[1], sd[1] ** 2]])
    w = np.linalg.solve(cov, mu)
    print(f"  mean-variance tangency (theory): scalp {w[0] / w.sum():.0%} / swing {w[1] / w.sum():.0%}")

    show(frontier_table(rs, rw, years_idx, "Backtest as is"))
    print("\nRe-optimizing on 2,000 bootstrap 3-year paths:")
    reoptimize(rs, rw)

    # haircuts: live edges are usually weaker than backtests; halve one sleeve's average daily return
    for label, a, b in (("Scalp edge halved", rs - rs.mean() / 2, rw), ("Swing edge halved", rs, rw - rw.mean() / 2),
                        ("Both halved", rs - rs.mean() / 2, rw - rw.mean() / 2)):
        t = frontier_table(a, b, years_idx, label)
        print(f"\n--- {label}: ", end="")
        print(" | ".join(f"{k} {t.loc[t[c].idxmax(), 'scalp']:.0%}" for k, c in (("max Sharpe", "Sharpe"), ("max growth", "3y growth"), ("best 3y bad case", "3y p5"))))
        sub = t[t["scalp"].isin([0.0, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0])]
        print("    " + "  ".join(f"scalp {r['scalp']:.0%}: CAGR {r['CAGR']:+.0%} Sh {r['Sharpe']:.2f} 3y p5 {r['3y p5']:+.0%}" for _, r in sub.iterrows()))

    # with idle cash allowed (e + g < 1): any better Sharpe? (Sharpe is unchanged by adding cash, so check frontier)
    print("\nWith idle cash allowed (scalp + swing <= 100%), max-Sharpe point:")
    best = (0, 0, -1)
    for e in GRID:
        for g in GRID:
            if e + g <= 1.0001 and e + g > 0:
                h = hist_metrics(paths(rs[None, :], rw[None, :], e, g)[0], years_idx)
                if h["Sharpe"] > best[2]:
                    best = (e, g, h["Sharpe"])
    print(f"  scalp {best[0]:.0%} / swing {best[1]:.0%} / cash {1 - best[0] - best[1]:.0%}: Sharpe {best[2]:.2f}")


if __name__ == "__main__":
    main()
