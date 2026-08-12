"""
Kiwoom REST OHLCV → TSV for LLM paste (daily / 1m / 5m).
Primary fix: ka10080 with stk_cd "KRX:XXXXXX" often returns a single invalid row; use plain "XXXXXX" for the main series.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd
from zoneinfo import ZoneInfo

from kiwoom import Bot
from kiwoom.config.candle import PERIOD_TO_BODY_KEY, PERIOD_TO_DATA, PERIOD_TO_TIME_KEY, valid
from kiwoom.proc import candle as kiwoom_candle_proc

LOG = logging.getLogger("foreign_trend")
KST = ZoneInfo("Asia/Seoul")

CHART_EXPORT_DAILY_LOOKBACK_DAYS = 120
CHART_EXPORT_5M_LOOKBACK_DAYS = 7
CHART_EXPORT_1M_LOOKBACK_DAYS = 2
CHART_RSI_PERIOD = 14
CHART_SMA_WINDOWS: tuple[int, ...] = (5, 20, 60, 120)
CHART_VOLUME_KA10059_MATCH_TOL = 0.04
CHART_VOLUME_PARTIAL_SESSION_FRAC = 0.15
KA10059_ENDPOINT = "/api/dostk/stkinfo"
CHART_5M_SHIFT_OPEN_TO_BAR_END_MIN = 5
CHART_KA10080_UPD_STKPC_TP = "0"
CHART_MINUTE_VOL_CONSOLIDATE_EXCHANGES = True

CHART_COL_EN = {
    "일자": "date",
    "체결시간": "datetime_kst",
    "시가": "open",
    "고가": "high",
    "저가": "low",
    "종가": "close",
    "거래량": "volume_shares",
    "거래대금": "turnover_krw",
}


def _response_header_get(res, name_lc: str) -> str:
    for k, v in (getattr(res, "headers", None) or {}).items():
        if str(k).lower() == name_lc:
            return str(v or "").strip()
    return ""


def _parse_signed_float(val) -> float | None:
    if val is None:
        return None
    s = str(val).replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _ka10059_extract_row_list(body: dict) -> list[dict]:
    best: list[dict] = []
    for _k, v in body.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            if len(v) > len(best):
                best = v
    return best


def _ka10059_row_to_vals(row: dict) -> tuple[str, float | None, float | None]:
    raw_dt = row.get("dt") or row.get("date") or ""
    ymd = str(raw_dt).strip().replace("-", "")[:8]
    if len(ymd) < 8 or not ymd.isdigit():
        ymd = ""
    fr_v, ins_v = None, None
    for k in (
        "frgnr_ntbuy_qty",
        "frgn_ntbuy_qty",
        "frgnr_nt_buy_qty",
        "frgn_ntby_qty",
        "frgnr_ntby_qty",
    ):
        if k in row:
            fr_v = _parse_signed_float(row.get(k))
            break
    for k in (
        "istt_ntbuy_qty",
        "inst_ntbuy_qty",
        "orgn_ntbuy_qty",
        "istt_ntby_qty",
        "isttu_ntbuy_qty",
    ):
        if k in row:
            ins_v = _parse_signed_float(row.get(k))
            break
    return ymd, fr_v, ins_v


def _ka10059_row_daily_volume(row: dict) -> float | None:
    for k in ("trde_qty", "trde_qnt", "acml_trde_qty", "vol", "ntby_trde_qty", "acc_trde_qty"):
        if k in row and row.get(k) not in ("", None):
            v = _parse_signed_float(row.get(k))
            if v is not None:
                return v
    return None


async def fetch_ka10059_investor_and_volume(
    api,
    stk_cd: str,
    *,
    anchor_ymd: str | None = None,
) -> tuple[dict[str, tuple[float | None, float | None]], dict[str, float | None]]:
    merged: dict[str, tuple[float | None, float | None]] = {}
    vol_map: dict[str, float | None] = {}
    api_id = "ka10059"
    dt = anchor_ymd or datetime.now(KST).strftime("%Y%m%d")
    cont_yn, next_key = "N", ""
    page = 0
    while True:
        page += 1
        hdrs = api.headers(api_id, cont_yn=cont_yn, next_key=next_key)
        try:
            res = await api.request(
                KA10059_ENDPOINT,
                api_id,
                headers=hdrs,
                data={
                    "stk_cd": stk_cd,
                    "dt": dt,
                    "amt_qty_tp": "2",
                    "trde_tp": "0",
                    "unit_tp": "1",
                },
            )
        except Exception as e:
            LOG.error("chart_export: ka10059 request failed page=%s dt=%s: %s", page, dt, e)
            break
        body = res.json()
        rc = body.get("return_code")
        if rc not in (0, None, 20):
            LOG.warning("chart_export: ka10059 return_code=%s page=%s", rc, page)
            break
        rows = _ka10059_extract_row_list(body)
        for row in rows:
            ymd, fv, iv = _ka10059_row_to_vals(row)
            if ymd:
                merged[ymd] = (fv, iv)
                dv = _ka10059_row_daily_volume(row)
                if dv is not None:
                    vol_map[ymd] = dv
        cy = _response_header_get(res, "cont-yn").upper()
        nk = _response_header_get(res, "next-key")
        if cy == "Y" and nk:
            cont_yn, next_key = "Y", nk
            continue
        break
    LOG.info(
        "chart_export: ka10059 done pages=%s investor_dates=%s volume_dates=%s",
        page,
        len(merged),
        len(vol_map),
    )
    return merged, vol_map


def _reconcile_volume_with_ka10059(
    df: pd.DataFrame | None,
    *,
    kind_label: str,
    ka59_vol: dict[str, float | None],
) -> tuple[pd.DataFrame | None, str]:
    if df is None or df.empty or "거래량" not in df.columns or not ka59_vol:
        return df, "volume=chart trde_qty (no ka10059 volume map)"
    tol = CHART_VOLUME_KA10059_MATCH_TOL
    out = df.sort_index(kind="stable").copy()
    idx = pd.DatetimeIndex(out.index)
    ymds = idx.strftime("%Y%m%d")

    if kind_label == "daily":
        replaced = 0
        new_qty: list[float] = []
        for i, y in enumerate(ymds):
            ka = ka59_vol.get(y)
            if ka is not None and not (isinstance(ka, float) and pd.isna(ka)):
                new_qty.append(float(ka))
                replaced += 1
            else:
                new_qty.append(float(out["거래량"].iloc[i]))
        out["거래량"] = new_qty
        return (
            out,
            f"volume=ka10059 trde_qty when available ({replaced}/{len(out)} rows); else ka10081",
        )

    uniq = sorted(set(ymds), reverse=True)
    for ymd in uniq:
        ka = ka59_vol.get(ymd)
        if ka is None or (isinstance(ka, float) and pd.isna(ka)) or float(ka) <= 0:
            continue
        mask = ymds == ymd
        s = float(out.loc[mask, "거래량"].astype(float).sum())
        if s <= 0:
            continue
        if s < CHART_VOLUME_PARTIAL_SESSION_FRAC * float(ka):
            continue
        ka_f = float(ka)
        e1 = abs(s - ka_f) / ka_f
        e1k = abs(s * 1000.0 - ka_f) / ka_f
        if e1k < tol and e1k <= e1:
            out["거래량"] = out["거래량"].astype(float) * 1000.0
            msg = (
                f"volume=ka10080 trde_qty x1000 to match ka10059 "
                f"(day {ymd} sum_before={s:.0f} ka10059={ka_f:.0f})"
            )
            LOG.info("chart_export: %s", msg)
            return out, msg
        if e1 < tol:
            msg = (
                f"volume=ka10080 trde_qty single-share units "
                f"(day {ymd} sum={s:.0f} ka10059={ka_f:.0f})"
            )
            LOG.info("chart_export: %s", msg)
            return out, msg
        LOG.warning(
            "chart_export: volume=ka10080 raw (day %s sum=%.0f vs ka10059=%.0f; rel_err1=%.3f rel_err×1000=%.3f)",
            ymd,
            s,
            ka_f,
            e1,
            e1k,
        )
        return out, f"volume=ka10080 raw (day {ymd})"

    return out, "volume=ka10080 trde_qty (no ka10059 overlap for calibration)"


def _intraday_trde_qty_decimal_chonju_to_shares(
    df: pd.DataFrame | None,
) -> tuple[pd.DataFrame | None, str]:
    if df is None or df.empty or "거래량" not in df.columns:
        return df, ""
    s = df["거래량"].astype(float)
    has_frac = (s.dropna() % 1).abs() > 1e-9
    if not has_frac.any():
        return df, ""
    out = df.copy()
    out["거래량"] = s * 1000.0
    return out, "precalc=trde_qty x1000 when any bar has fractional part (decimal 1000-share lots -> shares)"


def _add_close_smas(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "종가" not in df.columns:
        return df
    out = df.sort_index(kind="stable").copy()
    close = out["종가"].astype(float)
    for n in CHART_SMA_WINDOWS:
        out[f"ma{n}"] = close.rolling(window=n, min_periods=n).mean().round(2)
    return out


def _add_rsi_wilder(df: pd.DataFrame, period: int) -> pd.DataFrame:
    if df is None or df.empty or "종가" not in df.columns:
        return df
    out = df.copy()
    close = out["종가"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_g = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_g / avg_l
    rsi = 100.0 - (100.0 / (1.0 + rs))
    out["rsi14"] = rsi.replace([float("inf"), float("-inf")], float("nan")).round(2)
    return out


def _clean_numeric_series(s: pd.Series) -> pd.Series:
    if s.dtype == object or getattr(s.dtype, "name", "") == "string":
        s = s.astype(str).str.replace(",", "", regex=False).str.strip()
        s = s.replace({"nan": pd.NA, "None": pd.NA, "": pd.NA})
    return pd.to_numeric(s, errors="coerce")


def _sanitize_chart_export_numbers(out: pd.DataFrame) -> pd.DataFrame:
    out = out.copy()
    if "volume_shares" in out.columns:
        v = _clean_numeric_series(out["volume_shares"])
        out["volume_shares"] = v.round().astype("Int64")
    if "turnover_krw" in out.columns:
        t = _clean_numeric_series(out["turnover_krw"])
        out["turnover_krw"] = t.round().astype("Int64")
    return out


def _chart_stk_cd_base(code: str) -> str:
    s = str(code).strip()
    if ":" in s:
        s = s.split(":", 1)[1]
    if s.endswith("_NX"):
        s = s[:-3]
    if s.endswith("_AL"):
        s = s[:-3]
    return s


def _ka10080_row_trde_qty_float(row: dict) -> float:
    v = row.get("trde_qty")
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return 0.0


def _ka10080_volume_merge_usable(body: dict, list_key: str) -> bool:
    """
    Kiwoom valid() rejects some NXT pages (e.g. one row with volume but empty cur_prc).
    For venue merge we only need trde_qty keyed by cntr_tm.
    """
    rows = body.get(list_key)
    if not isinstance(rows, list) or not rows:
        return False
    if len(rows) >= 2:
        return True
    r0 = rows[0]
    if _ka10080_row_trde_qty_float(r0) != 0.0:
        return True
    cp = r0.get("cur_prc")
    if cp is not None and str(cp).strip().replace(",", "") not in ("", "0"):
        return True
    return False


def _nxt_minute_stk_cd_attempts(base: str) -> list[tuple[str, str]]:
    b = _chart_stk_cd_base(base)
    return [
        (f"NXT:{b}_NX", "NXT:code_NX"),
        (f"{b}_NX", "code_NX"),
    ]


async def _fetch_minute_chart_for_venue_merge(
    api,
    *,
    tic_scope: str,
    start_ymd: str | None,
    attempts: list[tuple[str, str]],
    venue_label: str,
) -> dict | None:
    """Try alternate stk_cd spellings; return first response with mergeable minute rows."""
    key = PERIOD_TO_BODY_KEY["stock"]["min"]
    for stk_cd, tag in attempts:
        try:
            b = await fetch_stock_minute_chart(api, stk_cd, tic_scope, start_ymd)
            n = len(b.get(key) or [])
            if _ka10080_volume_merge_usable(b, key):
                LOG.info(
                    "chart_export: %s volume merge source rows=%s stk_cd=%s (%s)",
                    venue_label,
                    n,
                    stk_cd,
                    tag,
                )
                return b
            LOG.info(
                "chart_export: %s skip (no mergeable rows) rows=%s stk_cd=%s (%s)",
                venue_label,
                n,
                stk_cd,
                tag,
            )
        except Exception as e:
            LOG.info("chart_export: %s fetch failed stk_cd=%s (%s): %s", venue_label, stk_cd, tag, e)
    return None


def _merge_ka10080_volume_rows(primary: dict, extra_bodies: list[dict], list_key: str) -> dict:
    vol_by_tm: dict[str, float] = {}
    for body in (primary, *extra_bodies):
        for r in body.get(list_key) or []:
            tm = r.get("cntr_tm")
            if tm is None or tm == "":
                continue
            k = str(tm)
            vol_by_tm[k] = vol_by_tm.get(k, 0.0) + _ka10080_row_trde_qty_float(r)
    out = dict(primary)
    new_rows: list[dict] = []
    for r in primary.get(list_key) or []:
        tm = r.get("cntr_tm")
        nr = dict(r)
        if tm is not None and str(tm) in vol_by_tm:
            nr["trde_qty"] = vol_by_tm[str(tm)]
        new_rows.append(nr)
    out[list_key] = new_rows
    return out


def _shift_minute_chart_index_bar_end_kst(
    df: pd.DataFrame | None, *, delta_minutes: int
) -> pd.DataFrame | None:
    if df is None or df.empty or delta_minutes <= 0:
        return df
    out = df.sort_index(kind="stable").copy()
    out.index = out.index + pd.Timedelta(minutes=delta_minutes)
    return out


def _dataframe_to_llm_tsv(
    df: pd.DataFrame,
    *,
    ticker: str,
    kind_label: str,
    investor_by_ymd: dict[str, tuple[float | None, float | None]] | None = None,
    ka59_daily_volume_by_ymd: dict[str, float | None] | None = None,
) -> str:
    if df is None or df.empty:
        return f"(no rows)\t{ticker}\t{kind_label}\n"
    df_adj = df
    pre_vol_notes: list[str] = []
    if kind_label in ("1min", "5min"):
        df_adj, precalc = _intraday_trde_qty_decimal_chonju_to_shares(df_adj)
        if precalc:
            pre_vol_notes.append(precalc)
            LOG.info("chart_export: %s", precalc)
    df_work, vol_note = _reconcile_volume_with_ka10059(
        df_adj,
        kind_label=kind_label,
        ka59_vol=ka59_daily_volume_by_ymd or {},
    )
    if pre_vol_notes:
        vol_note = "\t".join(pre_vol_notes + [vol_note])
    if df_work is None or df_work.empty:
        return f"(no rows)\t{ticker}\t{kind_label}\n"
    out = _add_close_smas(df_work)
    out = _add_rsi_wilder(out, CHART_RSI_PERIOD)
    out = out.reset_index()
    out = out.rename(columns={k: v for k, v in CHART_COL_EN.items() if k in out.columns})
    out = _sanitize_chart_export_numbers(out)
    time_cols = [c for c in ("date", "datetime_kst") if c in out.columns]
    tc = time_cols[0] if time_cols else None
    if kind_label == "5min" and tc and "volume_shares" in out.columns:
        tser = pd.to_datetime(out[tc], errors="coerce")
        max_d = tser.max()
        if pd.notna(max_d):
            d0 = max_d.date()
            for h, m in ((14, 20), (14, 25), (14, 30)):
                sel = (tser.dt.date == d0) & (tser.dt.hour == h) & (tser.dt.minute == m)
                if bool(sel.any()):
                    row = out.loc[sel].iloc[0]
                    LOG.info(
                        "chart_export: 5m HTS-check %04d-%02d-%02d %02d:%02d volume_shares=%s",
                        d0.year,
                        d0.month,
                        d0.day,
                        h,
                        m,
                        row.get("volume_shares"),
                    )
    if tc:
        out["_ymd"] = pd.to_datetime(out[tc], errors="coerce").dt.strftime("%Y%m%d")
    else:
        out["_ymd"] = ""
    inv = investor_by_ymd or {}
    fnb, inb = [], []
    for y in out["_ymd"]:
        key = str(y).strip() if y is not None and str(y) != "NaT" else ""
        fv, iv = inv.get(key, (None, None)) if key else (None, None)
        fnb.append(fv)
        inb.append(iv)
    out["foreign_net_buy_shares"] = fnb
    out["institution_net_buy_shares"] = inb
    out = out.drop(columns=["_ymd"], errors="ignore")
    ohlc = [c for c in ("open", "high", "low", "close") if c in out.columns]
    ma_cols = [f"ma{n}" for n in CHART_SMA_WINDOWS if f"ma{n}" in out.columns]
    rsi_cols = ["rsi14"] if "rsi14" in out.columns else []
    vol_cols = [c for c in ("volume_shares",) if c in out.columns]
    inv_cols = ["foreign_net_buy_shares", "institution_net_buy_shares"]
    turn_cols = [c for c in ("turnover_krw",) if c in out.columns]
    fixed_tail = ohlc + ma_cols + rsi_cols + vol_cols + inv_cols + turn_cols
    seen = set(time_cols)
    ordered: list[str] = list(time_cols)
    for c in fixed_tail:
        if c in out.columns and c not in seen:
            ordered.append(c)
            seen.add(c)
    for c in out.columns:
        if c not in seen:
            ordered.append(c)
            seen.add(c)
    out = out[ordered]
    out = out.iloc[::-1]
    clock_hint = (
        f"5m_datetime_kst=bar_end_KST (+{CHART_5M_SHIFT_OPEN_TO_BAR_END_MIN}m vs API open stamp)\t"
        if kind_label == "5min"
        else ""
    )
    meta = (
        f"# meta: ticker={ticker}\tseries={kind_label}\trows={len(out)}\t"
        f"row_order=newest_first\t"
        f"sma_on=close windows={CHART_SMA_WINDOWS}\t"
        f"rsi14=Wilder(period={CHART_RSI_PERIOD}) on close\t"
        f"foreign_inst_net=ka10059 daily shares; same value for all bars on that KST calendar day\t"
        f"{clock_hint}"
        f"{vol_note}\t"
        f"generated_kst={datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    return meta + out.to_csv(sep="\t", index=False, lineterminator="\n", na_rep="")


async def fetch_stock_minute_chart(api, code: str, tic_scope: str, start_ymd: str | None) -> dict:
    ctype, period = "stock", "min"
    endpoint = "/api/dostk/chart"
    api_id = "ka10080"
    data = dict(PERIOD_TO_DATA[ctype][period])
    data["stk_cd"] = code
    data["tic_scope"] = str(tic_scope)
    data["upd_stkpc_tp"] = CHART_KA10080_UPD_STKPC_TP
    ymd = len("YYYYMMDD")
    key = PERIOD_TO_BODY_KEY[ctype][period]
    time_key = PERIOD_TO_TIME_KEY[period]

    def should_continue(body: dict) -> bool:
        if not valid(body, period, ctype):
            return False
        if not start_ymd:
            return True
        chart = body[key]
        earliest = chart[-1][time_key][:ymd]
        return start_ymd <= earliest

    LOG.info(
        "chart_export: ka10080 request_until start stk_cd=%s tic_scope=%s start_ymd=%s",
        code,
        tic_scope,
        start_ymd,
    )
    body = await api.request_until(should_continue, endpoint, api_id, data=data)
    n = len(body.get(key) or [])
    LOG.info("chart_export: ka10080 done stk_cd=%s tic_scope=%s rows=%s", code, tic_scope, n)
    return body


async def fetch_stock_minute_chart_consolidated_volume(
    api, code: str, tic_scope: str, start_ymd: str | None
) -> dict:
    """
    OHLCV from plain6-digit stk_cd (KRX). Optionally add NXT bar volume at same cntr_tm (no SOR).
    Do NOT use KRX:XXXXXX as primary — Kiwoom often returns one invalid row and pagination stops.
    """
    key = PERIOD_TO_BODY_KEY["stock"]["min"]
    base = _chart_stk_cd_base(code)
    if not CHART_MINUTE_VOL_CONSOLIDATE_EXCHANGES:
        return await fetch_stock_minute_chart(api, base, tic_scope, start_ymd)

    body = await fetch_stock_minute_chart(api, base, tic_scope, start_ymd)
    if not valid(body, "min", "stock"):
        LOG.warning("chart_export: primary ka10080 invalid after plain stk_cd=%s", base)
        return body

    extras: list[dict] = []
    nxt_b = await _fetch_minute_chart_for_venue_merge(
        api,
        tic_scope=tic_scope,
        start_ymd=start_ymd,
        attempts=_nxt_minute_stk_cd_attempts(base),
        venue_label="NXT",
    )
    if nxt_b is not None:
        extras.append(nxt_b)

    if not extras:
        LOG.info("chart_export: ka10080 volume merge skipped (no NXT); using KRX/plain stk_cd only")
        return body
    merged = _merge_ka10080_volume_rows(body, extras, key)
    LOG.info(
        "chart_export: ka10080 volume = KRX/plain + NXT (summed trde_qty per cntr_tm)",
    )
    return merged


async def build_chart_export_for_timeframe(bot: Bot, ticker: str, timeframe: str) -> str:
    """timeframe: 'day' | 'min1' | 'min5'."""
    now = datetime.now(KST)
    end_ymd = now.strftime("%Y%m%d")
    LOG.info("chart_export: build start tf=%s code=%s end_ymd=%s", timeframe, ticker, end_ymd)
    parts = [
        "=== Kiwoom REST OHLCV (tabular; copy for LLM analysis) ===",
        f"stock_code={ticker}",
        "",
    ]
    try:
        inv_map: dict[str, tuple[float | None, float | None]] = {}
        ka59_vol: dict[str, float | None] = {}
        try:
            inv_map, ka59_vol = await fetch_ka10059_investor_and_volume(
                bot.api, ticker, anchor_ymd=end_ymd
            )
        except Exception as e_inv:
            LOG.error("chart_export: ka10059 investor merge skipped: %s", e_inv, exc_info=True)

        if timeframe == "day":
            start_ymd = (now - timedelta(days=CHART_EXPORT_DAILY_LOOKBACK_DAYS)).strftime("%Y%m%d")
            df = await bot.candle(ticker, "day", "stock", start=start_ymd, end=end_ymd)
            parts.append("--- DAILY (ka10081) ---")
            parts.append(
                _dataframe_to_llm_tsv(
                    df,
                    ticker=ticker,
                    kind_label="daily",
                    investor_by_ymd=inv_map,
                    ka59_daily_volume_by_ymd=ka59_vol,
                )
            )
        elif timeframe == "min5":
            start_ymd = (now - timedelta(days=CHART_EXPORT_5M_LOOKBACK_DAYS)).strftime("%Y%m%d")
            body = await fetch_stock_minute_chart_consolidated_volume(bot.api, ticker, "5", start_ymd)
            df = kiwoom_candle_proc.process(
                body, _chart_stk_cd_base(ticker), "min", "stock", start_ymd, None
            )
            if df is not None and not df.empty and CHART_5M_SHIFT_OPEN_TO_BAR_END_MIN:
                df = _shift_minute_chart_index_bar_end_kst(
                    df, delta_minutes=CHART_5M_SHIFT_OPEN_TO_BAR_END_MIN
                )
                LOG.info(
                    "chart_export: 5m datetime index +%sm (bar end KST)",
                    CHART_5M_SHIFT_OPEN_TO_BAR_END_MIN,
                )
            LOG.info("chart_export: 5m dataframe rows=%s", 0 if df is None else len(df))
            _5m_hdr = "--- 5-MINUTE (ka10080, tic_scope=5"
            if CHART_MINUTE_VOL_CONSOLIDATE_EXCHANGES:
                _5m_hdr += (
                    "; volume=KRX(plain stk_cd)+NXT trde_qty summed per cntr_tm "
                    "(NXT tries NXT:code_NX then code_NX)"
                )
            _5m_hdr += ") ---"
            parts.append(_5m_hdr)
            parts.append(
                _dataframe_to_llm_tsv(
                    df,
                    ticker=ticker,
                    kind_label="5min",
                    investor_by_ymd=inv_map,
                    ka59_daily_volume_by_ymd=ka59_vol,
                )
            )
        else:
            start_ymd = (now - timedelta(days=CHART_EXPORT_1M_LOOKBACK_DAYS)).strftime("%Y%m%d")
            body = await fetch_stock_minute_chart_consolidated_volume(bot.api, ticker, "1", start_ymd)
            df = kiwoom_candle_proc.process(
                body, _chart_stk_cd_base(ticker), "min", "stock", start_ymd, None
            )
            LOG.info("chart_export: 1m dataframe rows=%s", 0 if df is None else len(df))
            _1m_hdr = "--- 1-MINUTE (ka10080, tic_scope=1"
            if CHART_MINUTE_VOL_CONSOLIDATE_EXCHANGES:
                _1m_hdr += "; volume=KRX+NXT trde_qty summed per cntr_tm"
            _1m_hdr += ") ---"
            parts.append(_1m_hdr)
            parts.append(
                _dataframe_to_llm_tsv(
                    df,
                    ticker=ticker,
                    kind_label="1min",
                    investor_by_ymd=inv_map,
                    ka59_daily_volume_by_ymd=ka59_vol,
                )
            )
        out = "\n".join(parts) + "\n"
    except Exception as e:
        LOG.error("chart_export: failed tf=%s: %s", timeframe, e, exc_info=True)
        out = f"Error building chart export: {e}\n"
    LOG.info("chart_export: build finished tf=%s out_chars=%s", timeframe, len(out))
    return out
