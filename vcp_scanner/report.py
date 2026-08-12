"""
vcp_scanner/report.py

Generate two output artifacts after a scan run:

  vcp_scorecard.parquet  — full ranked feature DataFrame for programmatic use
  report.md              — human-readable Markdown report with:
                             * candidate summary table
                             * score distribution by market type
                             * top score drivers
                             * backtest forward-return summary (if available)
                             * data-quality notes

Usage
-----
    from vcp_scanner.report import generate_report
    generate_report(ranked_df, backtest_df=None, output_dir=".")
"""
from __future__ import annotations

import textwrap
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

KST = ZoneInfo("Asia/Seoul")


# ── Formatting helpers ────────────────────────────────────────────────────────

def _pct_fmt(val: float | None, decimals: int = 2) -> str:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "N/A"
    return f"{val:+.{decimals}f}%"


def _md_table(df: pd.DataFrame) -> str:
    """Render a DataFrame as a GitHub-flavoured Markdown table."""
    if df.empty:
        return "_No data_\n"
    cols = list(df.columns)
    rows_str = df.astype(str)
    widths   = {c: max(len(c), rows_str[c].str.len().max()) for c in cols}
    header   = "| " + " | ".join(c.ljust(widths[c])              for c in cols) + " |"
    sep      = "| " + " | ".join("-" * widths[c]                 for c in cols) + " |"
    body     = [
        "| " + " | ".join(str(row[c]).ljust(widths[c]) for c in cols) + " |"
        for _, row in rows_str.iterrows()
    ]
    return "\n".join([header, sep] + body) + "\n"


# ── Section builders ──────────────────────────────────────────────────────────

def _top_candidates_section(ranked: pd.DataFrame, n: int = 10) -> str:
    cols = ["rank", "symbol", "name", "market_type", "last_close",
            "composite_score", "price_score", "liquidity_score",
            "flows_score", "leverage_score", "flow_spike_flag"]
    present = [c for c in cols if c in ranked.columns]
    top = ranked.head(n)[present].copy()
    for col in top.select_dtypes("number").columns:
        top[col] = top[col].round(4)
    return _md_table(top)


def _distribution_section(ranked: pd.DataFrame) -> str:
    score_cols = ["composite_score", "price_score", "liquidity_score",
                  "flows_score", "leverage_score"]
    present = [c for c in score_cols if c in ranked.columns]

    lines: list[str] = []
    groups: list[tuple[str, pd.DataFrame]] = [("All markets", ranked)]
    if "market_type" in ranked.columns:
        for mtype, gdf in ranked.groupby("market_type"):
            groups.append((str(mtype), gdf))

    for label, gdf in groups:
        lines.append(f"### {label} (n = {len(gdf)})\n")
        rows = []
        for col in present:
            s = pd.to_numeric(gdf[col], errors="coerce").dropna()
            if s.empty:
                continue
            rows.append({
                "Score":  col,
                "Mean":   f"{s.mean():.4f}",
                "Std":    f"{s.std():.4f}",
                "Min":    f"{s.min():.4f}",
                "P25":    f"{s.quantile(0.25):.4f}",
                "Median": f"{s.median():.4f}",
                "P75":    f"{s.quantile(0.75):.4f}",
                "Max":    f"{s.max():.4f}",
            })
        if rows:
            lines.append(_md_table(pd.DataFrame(rows)))
        lines.append("")

    return "\n".join(lines)


def _top_drivers_section(ranked: pd.DataFrame, top_n: int = 8) -> str:
    detail_cols = [c for c in ranked.columns if c.startswith("score_detail_")]
    if not detail_cols:
        return "_score\\_detail\\_\\* columns not found — run rank\\_candidates() first._\n"

    means = {
        c: float(pd.to_numeric(ranked[c], errors="coerce").mean())
        for c in detail_cols
    }
    sorted_drivers = sorted(means.items(), key=lambda x: x[1], reverse=True)

    rows = [
        {
            "Driver":            col.replace("score_detail_", "").replace("_", " "),
            "Mean contribution": f"{val:.4f}",
            "Weight share":      f"{val / sum(means.values()) * 100:.1f}%"
                                 if sum(means.values()) > 0 else "N/A",
        }
        for col, val in sorted_drivers[:top_n]
    ]
    return _md_table(pd.DataFrame(rows))


def _backtest_section(bt: pd.DataFrame) -> str:
    if bt.empty:
        return "_No backtest data — run with `--backtest` flag after scan._\n"

    lines: list[str] = []
    groups: list[tuple[str, pd.DataFrame]] = [("All markets", bt)]
    if "market_type" in bt.columns:
        for mtype, gdf in bt.groupby("market_type"):
            groups.append((str(mtype), gdf))

    for label, gdf in groups:
        lines.append(f"### {label} (n = {len(gdf)})\n")
        valid = gdf[gdf.get("data_ok", pd.Series(True, index=gdf.index)) == True]

        horizon_rows = []
        for h in (1, 5, 10, 20):
            col = f"forward_ret_{h}d"
            if col not in valid.columns:
                continue
            s = pd.to_numeric(valid[col], errors="coerce").dropna()
            if s.empty:
                continue
            win_pct = f"{(s > 0).mean() * 100:.0f}%"
            dd_mean = ""
            if "max_dd_20d" in valid.columns and h == 20:
                dd = pd.to_numeric(valid["max_dd_20d"], errors="coerce").dropna()
                dd_mean = f"{dd.mean():.2f}%"
            horizon_rows.append({
                "Horizon": f"{h}d",
                "Mean ret":    _pct_fmt(s.mean()),
                "Median ret":  _pct_fmt(s.median()),
                "Win %":       win_pct,
                "Std":         _pct_fmt(s.std()),
                "Max DD (20d)": dd_mean if h == 20 else "",
            })
        if horizon_rows:
            lines.append(_md_table(pd.DataFrame(horizon_rows)))

        # VI / limit-up incidence
        vi_parts = []
        if "vi_count_20d" in valid.columns:
            vi = pd.to_numeric(valid["vi_count_20d"], errors="coerce").dropna()
            vi_parts.append(f"VI events: mean **{vi.mean():.1f}** days/signal (max {vi.max():.0f})")
        if "limit_up_count_20d" in valid.columns:
            lu = pd.to_numeric(valid["limit_up_count_20d"], errors="coerce").dropna()
            vi_parts.append(f"limit-up: mean **{lu.mean():.1f}** days/signal")
        if vi_parts:
            lines.append("\n_" + ";  ".join(vi_parts) + " in the 20-day forward window._\n")

        lines.append("")

    return "\n".join(lines)


def _quality_section() -> str:
    return (
        "* Dropped tickers and their reasons are logged to `dropped_tickers.log`.\n"
        "* VI/limit-up events are flagged when `(High − Low) / prev_close > 10 %`.\n"
        "* Wilder EMA ATR with spike-capping (4× 20-day median TR) is used in all price "
        "scores, reducing the distortion of single-session VI or limit-up days.\n"
        "* Flow scores are dampened by 20 % when the 5-day normalised foreign flow exceeds "
        "2.5× the 20-day baseline (theme-spike guard).\n"
        "* Split heuristic: overnight gaps > 40 % matching common KRX ratios are "
        "backward-adjusted before feature computation.\n"
    )


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_report(
    ranked: pd.DataFrame,
    backtest: pd.DataFrame | None = None,
    *,
    output_dir: str | Path = ".",
    report_filename: str = "report.md",
    scorecard_filename: str = "vcp_scorecard.parquet",
) -> None:
    """
    Write ``vcp_scorecard.parquet`` and ``report.md`` to *output_dir*.

    Parameters
    ----------
    ranked             : output of rank_candidates() (full feature DataFrame)
    backtest           : optional output of run_backtest(); may be None or empty
    output_dir         : directory where artifacts are written
    report_filename    : name for the Markdown report file
    scorecard_filename : name for the Parquet scorecard file
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ── Parquet scorecard ──────────────────────────────────────────────────────
    try:
        ranked.to_parquet(str(out / scorecard_filename), index=False)
        print(f"  Saved → {out / scorecard_filename}")
    except Exception as exc:
        print(f"  [WARN] Could not write parquet ({exc}). Install pyarrow: pip install pyarrow")
        fallback = (out / scorecard_filename).with_suffix(".csv")
        ranked.to_csv(str(fallback), index=False, encoding="utf-8-sig")
        print(f"  Saved fallback → {fallback}")

    # ── Markdown report ────────────────────────────────────────────────────────
    now   = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    bt    = backtest if backtest is not None else pd.DataFrame()
    n_kospi  = int((ranked.get("market_type", pd.Series()) == "KOSPI").sum()) if "market_type" in ranked.columns else "N/A"
    n_kosdaq = int((ranked.get("market_type", pd.Series()) == "KOSDAQ").sum()) if "market_type" in ranked.columns else "N/A"
    n_spike  = int(ranked.get("flow_spike_flag", pd.Series(0, index=ranked.index)).sum()) if "flow_spike_flag" in ranked.columns else "N/A"

    header = textwrap.dedent(f"""\
    # VCP Scanner Report

    _Generated: {now}_

    ## Scan Summary

    | Metric | Value |
    | ------ | ----- |
    | Total candidates ranked | {len(ranked)} |
    | KOSPI | {n_kospi} |
    | KOSDAQ | {n_kosdaq} |
    | Flow spike flags applied | {n_spike} |

    ---

    ## Top {min(10, len(ranked))} Candidates

    """)

    sections = {
        "## Score Distributions by Market Type\n\n":      _distribution_section(ranked),
        "## Top Score Drivers\n\n":                        _top_drivers_section(ranked),
        "## Backtest: Forward Return Summary\n\n":         _backtest_section(bt),
        "## Data-Quality & Robustness Notes\n\n":          _quality_section(),
    }

    md = header + _top_candidates_section(ranked) + "\n---\n\n"
    for heading, body in sections.items():
        md += heading + body + "\n---\n\n"

    report_path = out / report_filename
    report_path.write_text(md, encoding="utf-8")
    print(f"  Saved → {report_path}")
