"""
vcp_scanner/config.py
Market constants, env-var overrides, and scoring weights.
All tuneable parameters live here so operators can adjust without
touching algorithmic code.
"""
from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "output" / "vcp"


def _load_repo_env() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, val)


_load_repo_env()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Timezone ─────────────────────────────────────────────────────────────────
KST = ZoneInfo("Asia/Seoul")

# ── Kiwoom credentials (from .env: APP_KEY / APP_SECRET, or KIWOOM_* aliases)
APP_KEY = os.getenv("KIWOOM_APPKEY") or os.getenv("APP_KEY") or ""
APP_SECRET = os.getenv("KIWOOM_SECRETKEY") or os.getenv("APP_SECRET") or ""

# Seconds to sleep between each Kiwoom REST call (target ≤10 req/s)
KIWOOM_RATE_LIMIT_SLEEP: float = float(os.getenv("KIWOOM_RATE_SLEEP", "0.12"))

# ── Market-type liquidity and credit thresholds ───────────────────────────────
# KOSDAQ small caps get looser liquidity floor and wider credit tolerance.
KOSPI_MIN_TRADING_VALUE:  int   = 10_000_000_000   # 100억 KRW
KOSDAQ_MIN_TRADING_VALUE: int   =  5_000_000_000   #  50억 KRW
KOSPI_MAX_CREDIT_RATE:    float = 8.0
KOSDAQ_MAX_CREDIT_RATE:   float = 10.0

# ── Price-structure parameters ────────────────────────────────────────────────
MIN_HISTORY_ROWS:       int   = 200
ATR_LOOKBACK_PERIODS:   tuple = (5, 10, 20, 40)
VOL_PERCENTILE_WINDOW:  int   = 252          # rolling look-back for vol pct
POC_LOOKBACK_DAYS:      int   = 120
POC_MAX_DISTANCE_PCT:   float = 20.0         # price must be 0–20 % above POC
BREAKOUT_PROXIMITY_PCT: float = 30.0         # max % gap from 52-week high
RS_LOOKBACK_DAYS:       int   = 63           # ≈ 3-month Mansfield RS window

# ── Institutional bucket weights for weighted-flow score ──────────────────────
# Weights reflect reliability of each bucket as a "smart money" signal.
# Must reference the Kiwoom invsr_tp codes used in ka10058 / ka10059.
INST_BUCKET_WEIGHTS: dict[str, float] = {
    "6000": 0.35,   # 연기금   — pension funds (highest conviction)
    "1000": 0.25,   # 금융투자 — securities / fininvest
    "3000": 0.20,   # 투신     — investment trusts
    "3100": 0.10,   # 사모펀드 — private equity
    "2000": 0.07,   # 보험     — insurance
    "4000": 0.03,   # 은행     — banks (lowest smart-money weight)
}

# All investor type codes tracked (superset of the bucket weights)
ALL_INVESTOR_TYPES: dict[str, str] = {
    "8000": "개인",
    "9000": "외국인",
    "1000": "금융투자",
    "3000": "투신",
    "3100": "사모펀드",
    "5000": "기타금융",
    "4000": "은행",
    "2000": "보험",
    "6000": "연기금",
    "7000": "국가",
    "7100": "기타법인",
    "9999": "기관계",
}

# ── Top-level group scoring weights (must sum to 1.0) ────────────────────────
SCORE_WEIGHTS: dict[str, float] = {
    "price":     0.30,
    "liquidity": 0.20,
    "flows":     0.35,
    "leverage":  0.15,
}

# ── Price sub-weights (must sum to 1.0 within the price group) ───────────────
PRICE_SUB_WEIGHTS: dict[str, float] = {
    "atr_contraction": 0.30,
    "vol_percentile":  0.25,
    "breakout_prox":   0.25,
    "poc_distance":    0.10,
    "rs_score":        0.10,
}

# ── Flows sub-weights (must sum to 1.0 within the flows group) ───────────────
FLOWS_SUB_WEIGHTS: dict[str, float] = {
    "foreign":   0.40,
    "pension":   0.25,    # 연기금
    "fininvest": 0.20,    # 금융투자
    "invtrust":  0.15,    # 투신
}

# ── Leverage sub-weights (must sum to 1.0 within the leverage group) ─────────
LEVERAGE_SUB_WEIGHTS: dict[str, float] = {
    "level":     0.40,
    "delta_5d":  0.30,
    "delta_20d": 0.20,
    "zscore":    0.10,
}

# ── Hard gates (post-feature computation) ────────────────────────────────────
# Minimum 20-day average daily trading value in KRW (liquidity gate).
# Default 50억 — override with VCP_GATE_TURNOVER env var.
GATE_ADV_TURNOVER_KRW: int = int(os.getenv("VCP_GATE_TURNOVER", "5_000_000_000").replace("_", ""))

# Minimum foreign net-buy flow over the last 5 days normalised by ADV shares.
# 0.0 means any positive net foreign buying passes.
# Override with VCP_GATE_FOREIGN_RATIO (e.g. "0.05" = 5 % of ADV).
GATE_FOREIGN_FLOW_RATIO_5D: float = float(os.getenv("VCP_GATE_FOREIGN_RATIO", "0.0"))

# Maximum number of candidates in the final ranked output.
MAX_CANDIDATES: int = int(os.getenv("VCP_MAX_CANDIDATES", "20"))

# ── Debug / operational ───────────────────────────────────────────────────────
DEBUG_ENABLE: bool = os.getenv("VCP_DEBUG", "").strip().lower() in (
    "1", "true", "yes", "y", "on"
)
DEBUG_FORCE_FLOWS: bool = os.getenv("VCP_DEBUG_FORCE_FLOWS", "").strip().lower() in (
    "1", "true", "yes", "y", "on"
)
