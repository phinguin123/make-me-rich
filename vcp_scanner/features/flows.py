"""
vcp_scanner/features/flows.py

Institutional and foreign flow features via two Kiwoom REST TRs:

  ka10058 — 투자자별일별매매종목요청
    Endpoint : POST /api/dostk/stkinfo
    Used for  : Universe-level pre-fetch (one call per investor type returns ALL
                stocks that investor type net-bought over the date range).
    Fields    : strt_dt, end_dt, trde_tp=2(순매수), mrkt_tp, invsr_tp, stex_tp=3(통합)

  ka10059 — 종목별투자자기관별요청
    Endpoint  : POST /api/dostk/stkinfo
    Used for  : Per-symbol daily detail (all investor buckets in one call per day).
    Fields    : dt, stk_cd, amt_qty_tp=2(수량), trde_tp=0(순매수/도), unit_tp=1(단주)

Flow values are normalised by ADV (average daily volume in shares) so that
different market-cap stocks are comparable on the same scale.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import numpy as np

from ..config import (
    FLOWS_SUB_WEIGHTS,
    INST_BUCKET_WEIGHTS,
    KST,
)
from ..kiwoom_client import GOOD_RETURN_CODES, paginated_post, safe_post

log = logging.getLogger(__name__)

# ── ka10058 investor-type → canonical flow key mapping ───────────────────────
# Keys in the returned flow dict always end with "_net_20d".
_KA10058_INVSR_MAP: dict[str, str] = {
    "9000": "foreign",
    "6000": "pension",
    "1000": "fininvest",
    "3000": "invtrust",
    "3100": "privequity",
    "2000": "insurance",
    "4000": "bank",
    "9999": "inst_total",
}

# ka10059 response field → canonical bucket code
_KA10059_FIELD_MAP: dict[str, str] = {
    "frgnr_invsr": "9000",   # 외국인
    "natfor":      "9000",   # alternate foreign field
    "natn":        "9000",   # alternate
    "fnnc_invt":   "1000",   # 금융투자
    "insrnc":      "2000",   # 보험
    "samo_fund":   "3100",   # 사모펀드
    "invtrt":      "3000",   # 투신
    "penfnd_etc":  "6000",   # 연기금
    "bank":        "4000",   # 은행
    "etc_fnnc":    "5000",   # 기타금융
    "orgn":        "9999",   # 기관계 (total)
}


def _to_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return None


def _flow_key(label: str, suffix: str) -> str:
    """e.g. ("foreign", "_net_20d") → "foreign_net_20d" """
    return f"{label}{suffix}"


# ── Universe-level pre-fetch (ka10058) ───────────────────────────────────────

async def prefetch_universe_flows(
    bot,
    strt_dt: str,
    end_dt: str,
    *,
    suffix: str = "_net_20d",
    sleep: float = 0.15,
) -> dict[str, dict[str, float]]:
    """
    Pre-fetch ka10058 for every tracked investor type across KOSPI + KOSDAQ
    (stex_tp=3 = 통합).  One API call per investor type per market segment.

    Parameters
    ----------
    suffix : key suffix appended to each label, e.g. ``"_net_20d"`` or
             ``"_net_5d"``.  Call twice with different date ranges and suffixes,
             then merge the dicts to get both windows in one lookup table.

    Returns
    -------
    {
        "005930": {"foreign_net_20d": 1_500_000, "pension_net_20d": 300_000, ...},
        "028300": {...},
        ...
    }

    Notes
    -----
    * trde_tp=2 (순매수): response lists stocks the investor type net-bought.
      The ``netslmt_qty`` field carries the net-buy magnitude in that context.
    """
    result: dict[str, dict[str, float]] = {}

    tracked = {k: v for k, v in _KA10058_INVSR_MAP.items() if k != "9999"}  # skip total

    for invsr_tp, label in tracked.items():
        for mrkt_tp in ("001", "101"):   # KOSPI, KOSDAQ
            rows = await paginated_post(
                bot,
                "/api/dostk/stkinfo",
                "ka10058",
                {
                    "strt_dt":  strt_dt,
                    "end_dt":   end_dt,
                    "trde_tp":  "2",          # 순매수
                    "mrkt_tp":  mrkt_tp,
                    "invsr_tp": invsr_tp,
                    "stex_tp":  "3",          # 통합 (KRX + NXT + SOR)
                },
                sleep=sleep,
            )

            for row in rows:
                sym = str(row.get("stk_cd", "")).strip()
                if not sym:
                    continue
                qty_raw = row.get("netslmt_qty") or row.get("netbuy_qty") or 0
                qty = _to_float(qty_raw) or 0.0
                sym_dict = result.setdefault(sym, {})
                key = _flow_key(label, suffix)
                sym_dict[key] = sym_dict.get(key, 0.0) + qty

    return result


# ── Per-symbol detailed flows (ka10059) ──────────────────────────────────────

async def fetch_symbol_flows(
    bot,
    symbol: str,
    days: int = 20,
    *,
    sleep: float = 0.12,
) -> dict[str, float | int | None]:
    """
    Fetch per-symbol investor-bucket flows via ka10059 for each of the last
    ``days`` calendar days.

    Returns
    -------
    {
        "foreign_net_20d":    <shares>,
        "foreign_net_5d":     <shares>,  # last 5 of the 20 days
        "pension_net_20d":    <shares>,
        "pension_net_5d":     <shares>,
        "fininvest_net_20d":  <shares>,
        "fininvest_net_5d":   <shares>,
        "invtrust_net_20d":   <shares>,
        "invtrust_net_5d":    <shares>,
        "privequity_net_20d": <shares>,
        "insurance_net_20d":  <shares>,
        "bank_net_20d":       <shares>,
        "inst_total_net_20d": <shares>,
        "days_with_data":     <int>,
    }
    """
    today = datetime.now(KST).date()

    # Initialise accumulators keyed by invsr_tp code
    totals:    dict[str, float] = {code: 0.0 for code in _KA10059_FIELD_MAP.values()}
    totals_5d: dict[str, float] = {code: 0.0 for code in _KA10059_FIELD_MAP.values()}
    days_with_data = 0

    for i in range(days):
        dt = (today - timedelta(days=i)).strftime("%Y%m%d")
        body = await safe_post(
            bot,
            "/api/dostk/stkinfo",
            "ka10059",
            {"dt": dt, "stk_cd": symbol, "amt_qty_tp": "2", "trde_tp": "0", "unit_tp": "1"},
            sleep=sleep,
        )

        rc = body.get("return_code")
        if rc not in GOOD_RETURN_CODES and rc is not None:
            continue

        list_key = next((k for k, v in body.items() if isinstance(v, list)), None)
        all_rows = body.get(list_key, []) if list_key else []
        day_rows = [
            r for r in all_rows
            if isinstance(r, dict) and str(r.get("dt", "")).strip() == dt
        ]
        if not day_rows:
            continue

        days_with_data += 1

        for row in day_rows:
            for field, code in _KA10059_FIELD_MAP.items():
                val = _to_float(row.get(field))
                if val is None:
                    continue
                totals[code]    = totals.get(code, 0.0) + val
                if i < 5:
                    totals_5d[code] = totals_5d.get(code, 0.0) + val

    # Map from invsr_tp codes to canonical label keys
    code_to_label = {v: k for k, v in _KA10058_INVSR_MAP.items()}  # code→invsr_tp
    label_map = {
        "9000": "foreign",
        "6000": "pension",
        "1000": "fininvest",
        "3000": "invtrust",
        "3100": "privequity",
        "2000": "insurance",
        "4000": "bank",
        "9999": "inst_total",
    }

    out: dict[str, float | int | None] = {"days_with_data": days_with_data}
    for code, label in label_map.items():
        out[f"{label}_net_20d"] = totals.get(code, 0.0)
        if label in ("foreign", "pension", "fininvest", "invtrust"):
            out[f"{label}_net_5d"] = totals_5d.get(code, 0.0)

    return out


# ── Flow score computation ────────────────────────────────────────────────────

def _sigmoid(x: float, k: float = 0.5) -> float:
    return float(1.0 / (1.0 + np.exp(-k * x)))


def compute_flow_scores(
    raw: dict[str, float | None],
    adv_shares: float | None,
    float_shares: int | None = None,
) -> dict[str, float]:
    """
    Normalise raw flow quantities (shares) by ADV and compute per-bucket scores
    plus a weighted composite ``flows_score``.

    Normalisation denominator priority: ADV → float_shares → raw shares (unscaled).

    Parameters
    ----------
    raw          : dict from fetch_symbol_flows() or prefetch_universe_flows()
    adv_shares   : average daily volume in shares over 20 sessions
    float_shares : float/issued share count (optional secondary denominator)

    Returns
    -------
    flow_foreign_norm, flow_pension_norm, flow_fininvest_norm, flow_invtrust_norm
    flow_inst_weighted_norm   — institution weighted composite (per INST_BUCKET_WEIGHTS)
    flow_foreign_score        — [0, 1]
    flow_pension_score        — [0, 1]
    flow_fininvest_score      — [0, 1]
    flow_invtrust_score       — [0, 1]
    flow_privequity_score     — [0, 1]
    flow_ignition_5d          — [0, 1]; measures acceleration in last 5 of 20 days
    flows_score               — weighted composite [0, 1]
    """
    # Choose normalisation denominator
    denom = 1.0
    if adv_shares and adv_shares > 0:
        denom = adv_shares
    elif float_shares and float_shares > 0:
        denom = float_shares

    def _norm(key: str) -> float:
        return (raw.get(key) or 0.0) / denom

    f_norm   = _norm("foreign_net_20d")
    pe_norm  = _norm("pension_net_20d")
    fi_norm  = _norm("fininvest_net_20d")
    it_norm  = _norm("invtrust_net_20d")
    pr_norm  = _norm("privequity_net_20d")
    in_norm  = _norm("insurance_net_20d")
    bk_norm  = _norm("bank_net_20d")

    # Weighted institutional composite
    W = INST_BUCKET_WEIGHTS
    inst_weighted = (
        W.get("6000", 0) * pe_norm
        + W.get("1000", 0) * fi_norm
        + W.get("3000", 0) * it_norm
        + W.get("3100", 0) * pr_norm
        + W.get("2000", 0) * in_norm
        + W.get("4000", 0) * bk_norm
    )

    # 5d ignition: check whether recent 5-day flow is accelerating
    # (Only available when fetched via ka10059; set to 0 from ka10058 prefetch)
    f5  = _norm("foreign_net_5d")
    pe5 = _norm("pension_net_5d")
    fi5 = _norm("fininvest_net_5d")
    it5 = _norm("invtrust_net_5d")
    ignition_raw = f5 + pe5 + fi5 + it5

    scores = {
        "flow_foreign_norm":       f_norm,
        "flow_pension_norm":       pe_norm,
        "flow_fininvest_norm":     fi_norm,
        "flow_invtrust_norm":      it_norm,
        "flow_inst_weighted_norm": inst_weighted,

        "flow_foreign_score":   _sigmoid(f_norm,       k=0.8),
        "flow_pension_score":   _sigmoid(pe_norm,      k=0.8),
        "flow_fininvest_score": _sigmoid(fi_norm,      k=0.8),
        "flow_invtrust_score":  _sigmoid(it_norm,      k=0.8),
        "flow_privequity_score":_sigmoid(pr_norm,      k=0.8),
        "flow_ignition_5d":     _sigmoid(ignition_raw, k=0.5),
    }

    SW = FLOWS_SUB_WEIGHTS
    scores["flows_score"] = float(
        SW["foreign"]   * scores["flow_foreign_score"]
        + SW["pension"]   * scores["flow_pension_score"]
        + SW["fininvest"] * scores["flow_fininvest_score"]
        + SW["invtrust"]  * scores["flow_invtrust_score"]
    )

    return scores
