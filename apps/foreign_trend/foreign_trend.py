import html
import threading
import queue
import asyncio
from collections import deque
import json
import orjson
import sys
import logging
from logging.handlers import RotatingFileHandler
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import os

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = Path(__file__).resolve().parent
_BACKEND = _REPO_ROOT / "backend"
_OUTPUT = _REPO_ROOT / "output" / "foreign_trend"
_OUTPUT.mkdir(parents=True, exist_ok=True)
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

try:
    import websockets
    import websockets.server as _ws_server
    _WEBSOCKETS_AVAILABLE = True
except ImportError:
    _WEBSOCKETS_AVAILABLE = False

from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QFrame,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QGroupBox,
    QComboBox,
    QPushButton,
    QRadioButton,
    QButtonGroup,
    QAbstractItemView,
    QSizePolicy,
    QShortcut,
    QTextEdit,
    QTabWidget,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont, QColor, QBrush, QKeySequence

from kiwoom import Bot, REAL

from chart_export_kiwoom import build_chart_export_for_timeframe

KST = ZoneInfo("Asia/Seoul")

# Fix for Windows asyncio loop bugs
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _resolve_target_ticker_and_qt_argv(default_ticker: str = "078350") -> tuple[str, list[str]]:
    """First positional arg, if numeric, is the 6-digit KRX code; it is removed from argv for QApplication."""
    argv = list(sys.argv)
    if len(argv) >= 2:
        cand = argv[1].strip()
        if cand and not cand.startswith("-"):
            if cand.isdigit():
                padded = cand.zfill(6)
                ticker = padded[-6:] if len(padded) > 6 else padded
                return ticker, [argv[0]] + argv[2:]
    return default_ticker, argv


# 1. Configuration
def _load_repo_env(root: Path) -> None:
    env_path = root / ".env"
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


_load_repo_env(_REPO_ROOT)
APP_KEY = os.getenv("APP_KEY") or os.getenv("KIWOOM_APPKEY") or ""
APP_SECRET = os.getenv("APP_SECRET") or os.getenv("KIWOOM_SECRETKEY") or ""
if not APP_KEY or not APP_SECRET:
    raise SystemExit("Missing APP_KEY / APP_SECRET. Copy .env.example to .env.")
TARGET_TICKER, QT_APPLICATION_ARGV = _resolve_target_ticker_and_qt_argv("078350")

# --- "Skin" (only you know the map; keep this block private) ---
# Window chrome — looks like a generic scratchpad, not a terminal.
WINDOW_TITLE = "Draft log — local"
SESSION_MARK = f"S-{TARGET_TICKER[-4:]}"
# ref / delta / pct / vol in the header. Lane A = buy-side rank, B = sell-side rank.
# Member column uses BROKER_CODE_ENGLISH (extend as needed). Unknown codes -> "Member-xxx".
# Feed only exposes rank 1–5 per lane; merge + log below track churn vs that blind spot.

# ── Order book (0D) WebSocket bridge ─────────────────────────────────────────
# The Python bot receives Kiwoom 0D realtime messages and re-broadcasts them
# as plain JSON to any browser/React client connected to this local WS server.
# Set to None to disable the bridge entirely.
HOGA_WS_PORT: int | None = 8766
# Clients currently connected to the bridge.
_HOGA_WS_CLIENTS: set = set()

SNAPSHOT_LOG_ENABLE = True
SNAPSHOT_LOG_PATH = _OUTPUT / "relay_snapshots.jsonl"
# "every" = one JSON line per 0F (noisy). "churn" = only when top-5 signature changes, plus one seed line at start.
SNAPSHOT_LOG_MODE = "churn"

# General diagnostic log (rotating). This is separate from SNAPSHOT_LOG_PATH.
LOG_PATH = _OUTPUT / "foreign_trend.log"
LOG_LEVEL = logging.INFO
LOG_MAX_BYTES = 5_000_000
LOG_BACKUP_COUNT = 3
STALE_TICK_WARN_SEC = 15

# RVOL = session cumulative volume (real-time FID 13 when present) / avg of prior full days' volume.
RVOL_AVG_LOOKBACK_DAYS = 20
RVOL_REFRESH_INTERVAL_SEC = 300
RVOL_AVG_DAILY: float | None = None
# REST snapshot of top-5 buy/sell brokers (ka10002) — keeps left table alive if 0F is sparse.
KA10002_POLL_SEC = 6
TAPE_MAX_ROWS = 400
# Stock program-trading realtime (0w): second tape tab; FIDs 210 / 211 (+ FID 20 time).
# Session program table: REST intraday (ka90008) + live 0w; allow full KST day + venues.
PROGRAM_TRADE_MAX_ROWS = 1500
PROGRAM_TRADE_REAL_REG = "0w"
KA90008_ENDPOINT = "/api/dostk/mrkcond"
KA90008_API_ID = "ka90008"
KA90008_LIST_KEY = "stk_tm_prm_trde_trnsn"
# ka90008 venue is selected by ``stk_cd`` suffix (per /api/dostk/mrkcond spec):
#   "039490"     = (behaves as 통합 KRX+NXT for this TR, NOT KRX-only)
#   "039490_NX"  = NXT only
#   "039490_AL"  = 통합 (KRX+NXT)
# The body ``stex_tp`` param is NOT honored here. We fetch the 통합 series (``_AL``)
# for display + combined seed and NXT-only (``_NX``) to derive KRX = 통합 − NXT,
# which then matches the per-venue live 0w stream (KRX item + NXT ``_NX`` item).
KA90008_SUFFIX_TOTAL = "_AL"
KA90008_SUFFIX_NXT = "_NX"
# How long to wait for the second venue's first live 0w FID 210 before emitting
# a combined row. Prevents the transient "KRX_live + NXT_seed" jump when only
# one venue has synced. For single-venue (KRX-only) stocks, the timeout bounds
# the UI latency until the first combined row appears.
PROGRAM_SYNC_TIMEOUT_SEC = 10
# 공매도 추이 (ka10014) — /api/dostk/shsa; REST-only, loaded at connect.
KA10014_ENDPOINT = "/api/dostk/shsa"
KA10014_API_ID = "ka10014"
KA10014_LIST_KEY = "shrts_trnsn"
SHORT_SELL_LOOKBACK_DAYS = 60
MEMBER_FOCUS_MAX_ROWS = 3000
SESSION_BROKER_EVENT_MAX = 50000
# Append each 0F-derived row to disk (KST calendar day) so restarts same day keep history.
SESSION_BROKER_CACHE_ENABLE = True
# Member panel: show every cached 0F row (no filter). Not a full official exchange tape since 09:00.
MEMBER_SHOW_ALL = "__ALL__"
COMBO_ALL_LABEL = "(all) - every 0F rank line in store"
# Live trades (0B): only show rows whose print size (|FID 15|) meets threshold; ring buffer refilters on change.
TAPE_MIN_PRINT_CHOICES = (0, 10, 100, 1000)


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("foreign_trend")
    if logger.handlers:
        return logger
    logger.setLevel(LOG_LEVEL)
    fmt = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(threadName)s %(message)s", "%Y-%m-%d %H:%M:%S")

    try:
        fh = RotatingFileHandler(LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        # If file handler fails, still keep console logging.
        pass

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logger.propagate = False
    return logger


LOG = _setup_logging()

# 3-digit KRX / Kiwoom participant codes -> English names only (verify against KOFIA if needed).
BROKER_CODE_ENGLISH = {
    "001": "KB Securities",
    "002": "Mirae Asset Securities",
    "003": "NH Investment & Securities",
    "005": "Mirae Asset Securities",
    "006": "Korea Investment & Securities",
    "007": "Daol Investment & Securities",
    "008": "Daishin Securities",
    "009": "Meritz Securities",
    "010": "Samsung Securities",
    "011": "eBest Investment & Securities",
    "012": "Shinyoung Securities",
    "014": "Hana Financial Investment",
    "015": "Hi Investment & Securities",
    "017": "IBK Investment & Securities",
    "018": "Hanwha Investment & Securities",
    "021": "Korea Asset Investment & Securities",
    "023": "Bookook Securities",
    "024": "Hyundai Motor Securities",
    "025": "Morgan Stanley",
    "030": "SK Securities",
    "035": "J.P. Morgan",
    "036": "Deutsche Bank",
    "039": "CLSA",
    "040": "Credit Suisse",
    "041": "UBS",
    "042": "Merrill Lynch",
    "043": "Goldman Sachs",
    "050": "Macquarie",
    "054": "Nomura",
    "055": "Daiwa Securities",
    "065": "Societe Generale",
    "078": "Shinhan Investment & Securities",
    "088": "Kyobo Securities",
    "089": "DB Financial Investment & Securities",
    "090": "Yuanta Securities Korea",
    "278": "Kiwoom Securities",
}


def normalize_member_code(raw: str) -> str | None:
    """Return a 3-digit member code, or None if the feed sent non-numeric / Korean-only text."""
    s = str(raw).strip()
    if not s or s == "000":
        return None
    if s.isdigit():
        tail = s[-3:] if len(s) > 3 else s
        return tail.zfill(3)
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 3:
        return digits[-3:].zfill(3)
    return None


def english_broker_label(code: str) -> str:
    c = str(code).zfill(3)
    return BROKER_CODE_ENGLISH.get(c, f"Member-{c}")


def parse_share_qty(val) -> int | None:
    s = str(val).replace("+", "").replace(",", "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def parse_signed_int_metric(val) -> int | None:
    """Signed quantity for 0w FIDs 210 / 211 (+ = buy-side net, - = sell-side net)."""
    s = str(val).replace(",", "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _ka90008_tm_sort_key(tm: str) -> tuple:
    """Sort key for ka90008 `tm` (digits only, pad)."""
    s = "".join(c for c in str(tm) if c.isdigit())
    if len(s) >= 6:
        return (0, s[:6], s[6:])
    return (1, s, "")


def _six_digit_stk_base(stk_cd: str) -> str:
    """Normalize to a 6-digit KRX code (strip KRX:/NXT:/SOR: and _AL/_NX)."""
    s = str(stk_cd).strip()
    if not s:
        return ""
    if ":" in s:
        s = s.split(":", 1)[1].strip()
    for suf in ("_AL", "_NX"):
        if s.upper().endswith(suf):
            s = s[: -len(suf)].strip()
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) >= 6:
        return digits[-6:].zfill(6)
    if s.isdigit():
        return s.zfill(6)[-6:]
    return ""


def _program_trade_reg_items(codes: list[str]) -> list[str]:
    """0w REG items: one per venue (KRX plain + NXT ``_NX``) per 6-digit base.

    Per Kiwoom 실시간시세 spec, 0w publishes per venue. SOR:_AL does not deliver
    0w pushes — must subscribe KRX and NXT separately and sum on the client.
    """
    out: list[str] = []
    for c in codes:
        base = _six_digit_stk_base(c)
        if len(base) != 6:
            continue
        out.append(base)
        out.append(f"{base}_NX")
    return out


def _program_venue_from_item(item: str) -> str:
    """'078350' -> 'KRX', '078350_NX' -> 'NXT'."""
    s = str(item or "").strip()
    if ":" in s:
        s = s.split(":", 1)[1].strip()
    if s.upper().endswith("_NX"):
        return "NXT"
    return "KRX"


def _normalize_kiwoom_next_key(raw) -> str:
    """Strip continuation key; empty / placeholder breaks bad pagination (e.g. '--')."""
    s = str(raw or "").strip()
    if not s or s in ("--", "-"):
        return ""
    if s.startswith("--"):
        s2 = s.lstrip("-").strip()
        return s2 if s2 else ""
    return s


def _collapse_kiwoom_signed_qty_str(val) -> str:
    """Fix doubled signs from API (e.g. '--1234', '+-100') for program qty fields."""
    raw = str(val).strip()
    if not raw:
        return raw
    t = raw.replace(",", "").replace(" ", "")
    if not t or t in ("-", "--", "+", "+-", "-+", "++--"):
        return raw
    i = 0
    neg = False
    while i < len(t) and t[i] in "+-":
        if t[i] == "-":
            neg = not neg
        i += 1
    body = t[i:]
    if not body:
        return "0"
    try:
        n = int(float(body))
        if neg:
            n = -n
        return str(n)
    except ValueError:
        return raw


async def _ka90008_paginate_raw(api, base_data: dict) -> list[dict]:
    """One ka90008 query with 연속조회; reuse one header dict like kiwoom ``request_until``."""
    acc: list[dict] = []
    headers: dict | None = None
    while True:
        res = await api.request(KA90008_ENDPOINT, KA90008_API_ID, headers=headers, data=base_data)
        body = res.json()
        rc = body.get("return_code")
        if rc not in (0, None, 20):
            LOG.warning(
                "ka90008: return_code=%s msg=%s (stk=%s stex=%s)",
                rc,
                body.get("return_msg"),
                base_data.get("stk_cd"),
                base_data.get("stex_tp"),
            )
            break
        chunk = body.get(KA90008_LIST_KEY)
        if isinstance(chunk, list):
            acc.extend(chunk)
        rh = res.headers
        cont = str(rh.get("cont-yn") or rh.get("Cont-Yn") or "N").strip().upper()
        if cont != "Y":
            break
        nk = _normalize_kiwoom_next_key(rh.get("next-key") or rh.get("Next-Key"))
        if not nk:
            LOG.warning(
                "ka90008: cont=Y but empty next-key (stk=%s stex=%s)",
                base_data.get("stk_cd"),
                base_data.get("stex_tp"),
            )
            break
        if headers is None:
            headers = api.headers(KA90008_API_ID)
        api.headers(KA90008_API_ID, cont_yn="Y", next_key=nk, headers=headers)
    return acc


def _ka90008_venue_timeline(raw: list[dict]) -> dict[str, int]:
    """Extract {tm: cumulative_net_qty} from a single-venue ka90008 response."""
    out: dict[str, int] = {}
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        tm = str(rec.get("tm", "")).strip()
        if not tm:
            continue
        q = parse_signed_int_metric(
            _collapse_kiwoom_signed_qty_str(rec.get("prm_netprps_qty", ""))
        )
        if q is None:
            continue
        out[tm] = q
    return out


async def fetch_ka90008_program_intraday(api, stk_cd: str, trade_date: str) -> tuple[list[dict], int, int]:
    """ka90008 intraday program-trading: fetch 통합 (``_AL``) + NXT (``_NX``); derive KRX.

    ``ka90008`` with a plain 6-digit ``stk_cd`` returns the already-integrated
    series, not KRX-only, so we use the explicit ``_AL`` form for the total.
    Display rows are the 통합 series directly. Seed values returned to the caller:
    ``last_krx = last_total − last_nxt`` (so the live 0w per-venue sum lines up).
    """
    base = _six_digit_stk_base(stk_cd)
    if len(base) != 6:
        LOG.warning("ka90008: cannot parse 6-digit base from stk_cd=%r", stk_cd)
        return [], 0, 0

    async def _fetch_one(suffix: str) -> list[dict]:
        data = {
            "amt_qty_tp": "2",
            "stk_cd": f"{base}{suffix}",
            "date": trade_date,
        }
        try:
            return await _ka90008_paginate_raw(api, data)
        except Exception as e:
            LOG.warning("ka90008: suffix=%r stk=%s: %s", suffix, base, e)
            return []

    total_raw, nxt_raw = await asyncio.gather(
        _fetch_one(KA90008_SUFFIX_TOTAL),
        _fetch_one(KA90008_SUFFIX_NXT),
    )
    total = _ka90008_venue_timeline(total_raw)
    nxt = _ka90008_venue_timeline(nxt_raw)

    # Display rows come from the 통합 series; delta is within that series.
    tm_sorted = sorted(total.keys(), key=_ka90008_tm_sort_key)
    acc: list[dict] = []
    last = 0
    have_prev = False
    for tm in tm_sorted:
        cur = total[tm]
        delta = cur - last if have_prev else cur
        acc.append(
            {
                "exec_time": tm,
                "net_qty": str(cur),
                "delta_net": str(delta),
            }
        )
        last = cur
        have_prev = True

    last_total = total[tm_sorted[-1]] if tm_sorted else 0
    nxt_sorted = sorted(nxt.keys(), key=_ka90008_tm_sort_key)
    last_nxt = nxt[nxt_sorted[-1]] if nxt_sorted else 0
    last_krx = last_total - last_nxt

    LOG.info(
        "ka90008: total(_AL)=%s (%s rows) NXT(_NX)=%s (%s rows) → derived KRX=%s",
        last_total,
        len(total),
        last_nxt,
        len(nxt),
        last_krx,
    )
    return acc, last_krx, last_nxt


async def fetch_ka10014_short_selling_trend(
    api,
    stk_cd: str,
    strt_dt: str,
    end_dt: str,
    tm_tp: str = "1",
) -> list[dict]:
    """ka10014 short-selling trend; tm_tp 1 = date range (strt_dt–end_dt)."""
    base_data = {
        "stk_cd": stk_cd,
        "tm_tp": tm_tp,
        "strt_dt": strt_dt,
        "end_dt": end_dt,
    }
    acc: list[dict] = []
    headers: dict | None = None
    while True:
        res = await api.request(KA10014_ENDPOINT, KA10014_API_ID, headers=headers, data=base_data)
        body = res.json()
        rc = body.get("return_code")
        if rc not in (0, None, 20):
            LOG.warning(
                "ka10014: return_code=%s msg=%s",
                rc,
                body.get("return_msg"),
            )
            break
        chunk = body.get(KA10014_LIST_KEY)
        if isinstance(chunk, list):
            acc.extend(chunk)
        rh = res.headers
        cont = str(rh.get("cont-yn") or rh.get("Cont-Yn") or "N").strip().upper()
        if cont != "Y":
            break
        nk = str(rh.get("next-key") or rh.get("Next-Key") or "").strip()
        headers = api.headers(KA10014_API_ID, cont_yn="Y", next_key=nk)

    out: list[dict] = []
    for rec in acc:
        if not isinstance(rec, dict):
            continue
        out.append(
            {
                "dt": str(rec.get("dt", "")).strip(),
                "close_pric": rec.get("close_pric", ""),
                "pred_pre_sig": rec.get("pred_pre_sig", ""),
                "pred_pre": rec.get("pred_pre", ""),
                "flu_rt": rec.get("flu_rt", ""),
                "trde_qty": rec.get("trde_qty", ""),
                "shrts_qty": rec.get("shrts_qty", ""),
                "ovr_shrts_qty": rec.get("ovr_shrts_qty", ""),
                "trde_wght": rec.get("trde_wght", ""),
                "shrts_trde_prica": rec.get("shrts_trde_prica", ""),
                "shrts_avg_pric": rec.get("shrts_avg_pric", ""),
            }
        )

    def _dt_key(d: str) -> str:
        s = "".join(c for c in str(d) if c.isdigit())
        return s[:8] if len(s) >= 8 else s

    out.sort(key=lambda r: _dt_key(r.get("dt", "")), reverse=True)
    return out


def broker_session_cache_path() -> Path:
    d = datetime.now(KST).strftime("%Y%m%d")
    return _OUTPUT / f"broker_0f_{TARGET_TICKER}_{d}.jsonl"


def load_broker_session_cache_rows() -> list[dict]:
    p = broker_session_cache_path()
    if not p.exists() or not SESSION_BROKER_CACHE_ENABLE:
        return []
    rows: list[dict] = []
    try:
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Broker session cache load: {e}")
    return rows


def append_broker_session_cache_row(row: dict) -> None:
    if not SESSION_BROKER_CACHE_ENABLE:
        return
    p = broker_session_cache_path()
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"Broker session cache append: {e}")


def parse_member_pick(raw: str) -> str | None:
    """Resolve 3-digit member code from combobox text or typed input."""
    s = raw.strip()
    if not s:
        return None
    sl = s.lower()
    if sl.startswith("(all)") or sl.startswith("(*all*)"):
        return MEMBER_SHOW_ALL
    if "—" in s:
        part = s.split("—", 1)[0].strip()
    elif " - " in s:
        part = s.split(" - ", 1)[0].strip()
    else:
        part = s
    code = normalize_member_code(part)
    if code:
        return code
    digits = "".join(ch for ch in part if ch.isdigit())
    if len(digits) >= 3:
        return digits[-3:].zfill(3)
    if part.isdigit() and 1 <= len(part) <= 3:
        return part.zfill(3)
    return None


# Latest tick snapshot (updated on 0B; broker rows attach this price / exchange time)
LATEST_TICK: dict = {
    "price": "",
    "change": "",
    "rate": "",
    "volume": "",
    "exec_time": "",  # FID 20, often HHMMSS
    "cum_vol": "",  # FID 13 session cumulative shares (when feed sends it)
}

# Diagnostics for stale feeds / UI visibility
LAST_0B_UTC: datetime | None = None
LAST_0F_UTC: datetime | None = None
LAST_0W_UTC: datetime | None = None

# Program trading (0w) per-venue latest cumulative 순매수수량 (FID 210).
# Combined (KRX+NXT) is recomputed on every venue update and pushed to the UI.
PROGRAM_LATEST_NET: dict[str, int] = {"KRX": 0, "NXT": 0}
PROGRAM_LAST_COMBINED: int = 0
PROGRAM_HAS_PREV_COMBINED: bool = False
# Gate combined emission until BOTH venues have sent a live 0w FID 210 (or the
# sync-window times out). Without this, first live update shows
# ``live_venueA + seed_venueB`` which briefly mis-reports combined whenever the
# seed's per-venue split disagrees with live (seed comes from ka90008 REST).
PROGRAM_SEEN_VENUES: set[str] = set()
PROGRAM_SYNC_DEADLINE_UTC: datetime | None = None

# Cumulative top-5 ranks (lanes A/B) — updated when code FIDs appear in a 0F message.
FULL_RANK_A: dict[int, str] = {i: "" for i in range(1, 6)}
FULL_RANK_B: dict[int, str] = {i: "" for i in range(1, 6)}
PREV_RANK_SIG: tuple[tuple[str, ...], tuple[str, ...]] | None = None
_SNAPSHOT_LOG_SEED_PENDING = True

# Filled inside run_bot_async for REST calls from the Qt GUI thread.
BOT_LOOP: asyncio.AbstractEventLoop | None = None
BOT_API = None
BOT_CHART_BOT: Bot | None = None
_LAST_KA10002_SIG: tuple | None = None


def _fid_map(data: dict) -> dict:
    """Kiwoom sometimes uses non-string JSON keys; normalize to str FIDs."""
    return {str(k): v for k, v in data.items()}


def merge_top5_from_message(data: dict) -> None:
    """Apply code-only deltas so we keep full rank state between partial 0F updates."""
    for i in range(1, 6):
        bc = str(140 + i)
        if bc in data:
            raw = str(data[bc]).strip()
            if raw in ("", "000"):
                FULL_RANK_A[i] = ""
            else:
                nc = normalize_member_code(raw)
                if nc is not None:
                    FULL_RANK_A[i] = nc

        sc_primary = str(160 + i)
        sc_alt = str(145 + i)
        if sc_primary in data:
            raw = str(data[sc_primary]).strip()
            if raw in ("", "000"):
                FULL_RANK_B[i] = ""
            else:
                nc = normalize_member_code(raw)
                if nc is not None:
                    FULL_RANK_B[i] = nc
        elif sc_alt in data:
            raw = str(data[sc_alt]).strip()
            if raw in ("", "000"):
                FULL_RANK_B[i] = ""
            else:
                nc = normalize_member_code(raw)
                if nc is not None:
                    FULL_RANK_B[i] = nc


def snapshot_rank_tuple() -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(FULL_RANK_A[i] for i in range(1, 6)),
        tuple(FULL_RANK_B[i] for i in range(1, 6)),
    )


def churn_vs_previous(
    prev: tuple[tuple[str, ...], tuple[str, ...]] | None,
    cur: tuple[tuple[str, ...], tuple[str, ...]],
) -> dict | None:
    if prev is None:
        return None
    ca = sum(1 for a, b in zip(prev[0], cur[0]) if a != b)
    cb = sum(1 for a, b in zip(prev[1], cur[1]) if a != b)
    if ca == 0 and cb == 0:
        return None
    parts = []
    if ca:
        parts.append(f"A{ca}")
    if cb:
        parts.append(f"B{cb}")
    return {"a_slots": ca, "b_slots": cb, "label": "·".join(parts)}


def append_snapshot_log(
    churn: dict | None,
    sig: tuple[tuple[str, ...], tuple[str, ...]],
) -> None:
    global _SNAPSHOT_LOG_SEED_PENDING
    if not SNAPSHOT_LOG_ENABLE:
        return
    mode = (SNAPSHOT_LOG_MODE or "churn").lower()
    if mode not in ("every", "churn"):
        mode = "churn"
    if mode == "churn":
        if churn is not None:
            event = "churn"
        elif _SNAPSHOT_LOG_SEED_PENDING:
            event = "seed"
            _SNAPSHOT_LOG_SEED_PENDING = False
        else:
            return
    else:
        event = "update"

    row = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "mark": SESSION_MARK,
        "event": event,
        "rank_A": [f"T{c}" if c else "" for c in sig[0]],
        "rank_B": [f"T{c}" if c else "" for c in sig[1]],
        "churn": churn,
        "r0": str(LATEST_TICK.get("price", "")),
    }
    try:
        with SNAPSHOT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"Snapshot log: {e}")


def parse_hoga_message(data: dict, item: str) -> dict:
    """Convert raw 0D FID map into a Kiwoom REAL envelope the React component understands.

    The React ``useOrderBook`` hook expects:
      { trnm: "REAL", data: [{ type: "0D", name: "주식호가잔량", item: "...", values: {...} }] }
    """
    return {
        "trnm": "REAL",
        "data": [
            {
                "type": "0D",
                "name": "주식호가잔량",
                "item": item,
                "values": data,
            }
        ],
    }


async def _hoga_ws_broadcast(payload: str) -> None:
    """Fire-and-forget broadcast to all connected React clients."""
    if not _HOGA_WS_CLIENTS:
        return
    dead: set = set()
    for ws_client in list(_HOGA_WS_CLIENTS):
        try:
            await ws_client.send(payload)
        except Exception:
            dead.add(ws_client)
    _HOGA_WS_CLIENTS.difference_update(dead)


async def _hoga_ws_handler(websocket) -> None:
    """Handle one React client connection — register/remove from broadcast set."""
    _HOGA_WS_CLIENTS.add(websocket)
    LOG.info("hoga_ws: client connected (total=%d)", len(_HOGA_WS_CLIENTS))
    try:
        # Keep the connection alive; ignore incoming messages (registration
        # is handled by the Python bot, not driven by the browser).
        async for _ in websocket:
            pass
    except Exception:
        pass
    finally:
        _HOGA_WS_CLIENTS.discard(websocket)
        LOG.info("hoga_ws: client disconnected (total=%d)", len(_HOGA_WS_CLIENTS))


async def run_hoga_ws_server() -> None:
    """Start the local WebSocket broadcast server for the React order-book UI."""
    if not _WEBSOCKETS_AVAILABLE:
        LOG.warning(
            "hoga_ws: 'websockets' package not installed — bridge disabled. "
            "Run: pip install websockets"
        )
        return
    if HOGA_WS_PORT is None:
        return
    try:
        async with _ws_server.serve(_hoga_ws_handler, "localhost", HOGA_WS_PORT):
            LOG.info("hoga_ws: bridge listening on ws://localhost:%d", HOGA_WS_PORT)
            await asyncio.Future()  # run forever
    except OSError as e:
        LOG.error("hoga_ws: could not start bridge on port %d: %s", HOGA_WS_PORT, e)


async def register_stock_and_broker_realtime(
    api,
    grp_no: str,
    codes: list[str],
    refresh: str = "1",
) -> None:
    """Register websocket 0B (trades), 0F (당일거래원 ranks), 0w (program trading).

    Kiwoom REAL quirk (verified live): one REG with type [\"0B\",\"0F\"] often delivers only 0B.
    Send 0B first, then a second REG for 0F (same grp_no, refresh). 0F-before-0B can yield no 0F.
    0w: **one** REG with 통합 종목 ``SOR:<6자리>_AL`` (matches 통합 REST). Multiple 0w REGs or
    mixed KRX/NXT/SOR subscriptions were unreliable.
    """
    assert len(codes) <= 100, f"Max 100 codes per group, got {len(codes)} codes."
    base = {"trnm": "REG", "grp_no": grp_no, "refresh": refresh}
    await api.socket.send({**base, "data": [{"item": codes, "type": ["0B"]}]})
    await api.socket.send({**base, "data": [{"item": codes, "type": ["0F"]}]})
    # 0D — 주식호가잔량 (order book depth, 10 levels each side)
    await api.socket.send({**base, "data": [{"item": codes, "type": ["0D"]}]})
    # 0w is published per venue — register both KRX (plain) and NXT (``_NX``);
    # SOR:<code>_AL does not deliver 0w pushes.
    prog_items = _program_trade_reg_items(codes)
    if prog_items:
        await api.socket.send(
            {**base, "data": [{"item": prog_items, "type": [PROGRAM_TRADE_REAL_REG]}]}
        )


def broker_events_from_message(data: dict) -> list[dict]:
    """Parse 0F broker-rank FIDs (string keys). Buy 141-145 / 151-155; sell 161-165/171-175 or 146-150/156-160.

    Many packets are **qty-only** (e.g. 151 present, 141 absent). After ``merge_top5_from_message``,
    use ``FULL_RANK_A`` / ``FULL_RANK_B`` for the member code (call merge before this).
    """
    out: list[dict] = []
    for i in range(1, 6):
        buy_code_k = str(140 + i)
        buy_qty_k = str(150 + i)

        if buy_code_k in data or buy_qty_k in data:
            raw = str(data.get(buy_code_k, "")).strip()
            code: str | None = None
            if raw not in ("", "000"):
                code = normalize_member_code(raw)
            if not code:
                slot = (FULL_RANK_A.get(i) or "").strip()
                code = slot if slot else None
            if not code:
                continue
            code = str(code).zfill(3)
            qty = data.get(buy_qty_k, "")
            label = english_broker_label(code)
            out.append({"side": "buy", "broker": label, "qty": qty, "code": code})

        sell_pairs = (
            (str(160 + i), str(170 + i)),
            (str(145 + i), str(155 + i)),
        )
        for sell_code_k, sell_qty_k in sell_pairs:
            if sell_code_k in data or sell_qty_k in data:
                raw = str(data.get(sell_code_k, "")).strip()
                code = None
                if raw not in ("", "000"):
                    code = normalize_member_code(raw)
                if not code:
                    slot = (FULL_RANK_B.get(i) or "").strip()
                    code = slot if slot else None
                if not code:
                    continue
                code = str(code).zfill(3)
                qty = data.get(sell_qty_k, "")
                label = english_broker_label(code)
                out.append({"side": "sell", "broker": label, "qty": qty, "code": code})
                break

    return out


def rank_snapshot_fallback_events(data: dict) -> list[dict]:
    """If delta parsing yields nothing, emit one row per filled rank slot (after merge)."""
    out: list[dict] = []
    for i in range(1, 6):
        code = (FULL_RANK_A.get(i) or "").strip()
        if not code:
            continue
        qty = data.get(str(150 + i), "")
        out.append(
            {
                "side": "buy",
                "broker": english_broker_label(code),
                "code": code.zfill(3),
                "qty": qty,
            }
        )
    for i in range(1, 6):
        code = (FULL_RANK_B.get(i) or "").strip()
        if not code:
            continue
        qty = data.get(str(170 + i), data.get(str(155 + i), ""))
        out.append(
            {
                "side": "sell",
                "broker": english_broker_label(code),
                "code": code.zfill(3),
                "qty": qty,
            }
        )
    return out


def _ka10002_signature(body: dict) -> tuple:
    parts: list[str] = []
    for i in range(1, 6):
        parts.append(str(body.get(f"buy_trde_ori_{i}", "")))
        parts.append(str(body.get(f"buy_trde_qty_{i}", "")))
        parts.append(str(body.get(f"sel_trde_ori_{i}", "")))
        parts.append(str(body.get(f"sel_trde_qty_{i}", "")))
    return tuple(parts)


def ka10002_to_queue_events(body: dict, local_ts: str, skip_session: bool = True) -> list[dict]:
    """Build foreign-style payloads from ka10002 (top-5 buy/sell org snapshot)."""
    ref = str(body.get("cur_prc", "")).strip()
    out: list[dict] = []
    for i in range(1, 6):
        c = str(body.get(f"buy_trde_ori_{i}", "")).strip()
        if not c or c == "000":
            continue
        code = c.zfill(3)
        out.append(
            {
                "type": "foreign",
                "side": "buy",
                "broker": english_broker_label(code),
                "code": code,
                "qty": body.get(f"buy_trde_qty_{i}", ""),
                "ref_price": ref,
                "krx_time_raw": "",
                "local_time": local_ts,
                "_skip_session": skip_session,
            }
        )
    for i in range(1, 6):
        c = str(body.get(f"sel_trde_ori_{i}", "")).strip()
        if not c or c == "000":
            continue
        code = c.zfill(3)
        out.append(
            {
                "type": "foreign",
                "side": "sell",
                "broker": english_broker_label(code),
                "code": code,
                "qty": body.get(f"sel_trde_qty_{i}", ""),
                "ref_price": ref,
                "krx_time_raw": "",
                "local_time": local_ts,
                "_skip_session": skip_session,
            }
        )
    return out


async def ka10002_fetch_member_rows(member_code: str) -> list[dict]:
    """REST lines for one org code in today’s top-5 snapshot (not tick-by-tick)."""
    if BOT_API is None:
        return []
    r = await BOT_API.request("/api/dostk/stkinfo", "ka10002", data={"stk_cd": TARGET_TICKER})
    body = r.json()
    if body.get("return_code") not in (0, None):
        return []
    mc = str(member_code).strip().zfill(3)
    ref = str(body.get("cur_prc", "")).strip()
    ts = datetime.now().strftime("%H:%M:%S")
    rows: list[dict] = []
    for i in range(1, 6):
        c = str(body.get(f"buy_trde_ori_{i}", "")).strip().zfill(3)
        if c == mc:
            rows.append(
                {
                    "side": "buy",
                    "broker": english_broker_label(mc),
                    "code": mc,
                    "qty": body.get(f"buy_trde_qty_{i}", ""),
                    "ref_price": ref,
                    "krx_time_raw": "",
                    "local_time": f"{ts}  ·  REST snapshot",
                }
            )
    for i in range(1, 6):
        c = str(body.get(f"sel_trde_ori_{i}", "")).strip().zfill(3)
        if c == mc:
            rows.append(
                {
                    "side": "sell",
                    "broker": english_broker_label(mc),
                    "code": mc,
                    "qty": body.get(f"sel_trde_qty_{i}", ""),
                    "ref_price": ref,
                    "krx_time_raw": "",
                    "local_time": f"{ts}  ·  REST snapshot",
                }
            )
    return rows


async def refresh_rvol_avg(bot: Bot) -> None:
    """Load daily bars and set global RVOL_AVG_DAILY (mean volume excluding today)."""
    global RVOL_AVG_DAILY
    try:
        start = (datetime.now() - timedelta(days=150)).strftime("%Y%m%d")
        df = await bot.candle(TARGET_TICKER, "day", "stock", start=start)
        if df is None or df.empty or "거래량" not in df.columns:
            return
        s = df["거래량"].astype(float)
        if len(s) < 2:
            return
        hist = s.iloc[:-1]
        if len(hist) >= RVOL_AVG_LOOKBACK_DAYS:
            RVOL_AVG_DAILY = float(hist.tail(RVOL_AVG_LOOKBACK_DAYS).mean())
        else:
            RVOL_AVG_DAILY = float(hist.mean())
    except Exception as e:
        print(f"RVOL baseline: {e}")


async def rvol_refresh_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(RVOL_REFRESH_INTERVAL_SEC)
        await refresh_rvol_avg(bot)


def format_krx_hhmmss(raw: str) -> str:
    s = str(raw).strip()
    if len(s) >= 6 and s[:6].isdigit():
        h, m, sec = s[:2], s[2:4], s[4:6]
        return f"{h}:{m}:{sec}"
    return ""


def _kst_today_dt_from_hhmmss(hhmmss: str) -> datetime | None:
    s = str(hhmmss).strip()
    if len(s) >= 6 and s[:6].isdigit():
        h, m, sec = int(s[:2]), int(s[2:4]), int(s[4:6])
        now = datetime.now(KST)
        return datetime(now.year, now.month, now.day, h, m, sec, tzinfo=KST)
    return None


# Ranks / broker rows must not sit behind thousands of 0B ticks (one FIFO was starving the 0F UI).
RANK_GUI_QUEUE = queue.Queue()
# Unbounded: a bounded queue + drop-on-full was discarding prints vs HTS. Memory grows if the UI stalls.
TICK_GUI_QUEUE = queue.Queue()
PROGRAM_GUI_QUEUE = queue.Queue()
SHORT_GUI_QUEUE = queue.Queue()


def _enqueue_tick_ui(payload: dict) -> None:
    TICK_GUI_QUEUE.put_nowait(payload)


def _enqueue_program_ui(payload: dict) -> None:
    PROGRAM_GUI_QUEUE.put_nowait(payload)


# Qt timer interval. Ticks are drained fully each pass (no per-pass cap) so nothing is skipped for display.
GUI_POLL_MS = 4


def tick_aggressor_side(data: dict) -> str:
    """Kiwoom 0B FID 15 is often signed: leading + = buy print, - = sell print."""
    v = str(data.get("15", "")).strip()
    if v.startswith("+"):
        return "buy"
    if v.startswith("-"):
        return "sell"
    return "neutral"


async def run_bot_async():
    global BOT_LOOP, BOT_API, _LAST_KA10002_SIG

    async with Bot(host=REAL, appkey=APP_KEY, secretkey=APP_SECRET) as bot:
        global BOT_CHART_BOT
        BOT_LOOP = asyncio.get_running_loop()
        BOT_API = bot.api
        BOT_CHART_BOT = bot
        LOG.info("bot: starting (ticker=%s)", TARGET_TICKER)

        async def ka10002_poll_loop() -> None:
            global _LAST_KA10002_SIG
            await asyncio.sleep(1)
            while True:
                try:
                    res = await bot.api.request(
                        "/api/dostk/stkinfo", "ka10002", data={"stk_cd": TARGET_TICKER}
                    )
                    body = res.json()
                    if body.get("return_code") not in (0, None):
                        continue
                    sig = _ka10002_signature(body)
                    if sig == _LAST_KA10002_SIG:
                        continue
                    _LAST_KA10002_SIG = sig
                    ts = datetime.now().strftime("%H:%M:%S")
                    for evt in ka10002_to_queue_events(body, ts, skip_session=True):
                        RANK_GUI_QUEUE.put(evt)
                except Exception as e:
                    LOG.error("ka10002 poll: %s", e, exc_info=True)
                await asyncio.sleep(KA10002_POLL_SEC)

        async def handle_real_data(msg):
            global PREV_RANK_SIG, LAST_0B_UTC, LAST_0F_UTC, LAST_0W_UTC
            global PROGRAM_LAST_COMBINED, PROGRAM_HAS_PREV_COMBINED
            global PROGRAM_SYNC_DEADLINE_UTC
            try:
                if not hasattr(msg, "values"):
                    return

                data = _fid_map(orjson.loads(msg.values))
                name = getattr(msg, "name", "")
                r_type = getattr(msg, "type", "")
                r_type_norm = str(r_type).strip().upper()

                if r_type_norm == "0D":
                    # 주식호가잔량 — broadcast to React order-book clients.
                    if _HOGA_WS_CLIENTS and HOGA_WS_PORT is not None:
                        r_item = getattr(msg, "item", TARGET_TICKER)
                        envelope = parse_hoga_message(data, r_item)
                        asyncio.ensure_future(
                            _hoga_ws_broadcast(json.dumps(envelope, ensure_ascii=False))
                        )

                if r_type_norm == "0W":
                    now_utc = datetime.now(timezone.utc)
                    LAST_0W_UTC = now_utc
                    venue = _program_venue_from_item(getattr(msg, "item", ""))
                    net_cum = parse_signed_int_metric(
                        _collapse_kiwoom_signed_qty_str(data.get("210", ""))
                    )
                    if net_cum is not None:
                        PROGRAM_LATEST_NET[venue] = net_cum
                        if venue not in PROGRAM_SEEN_VENUES:
                            PROGRAM_SEEN_VENUES.add(venue)
                            LOG.info(
                                "0w: first live %s FID 210=%s (combined seen=%s)",
                                venue,
                                net_cum,
                                sorted(PROGRAM_SEEN_VENUES),
                            )
                        if PROGRAM_SYNC_DEADLINE_UTC is None:
                            PROGRAM_SYNC_DEADLINE_UTC = now_utc + timedelta(
                                seconds=PROGRAM_SYNC_TIMEOUT_SEC
                            )

                    both_seen = "KRX" in PROGRAM_SEEN_VENUES and "NXT" in PROGRAM_SEEN_VENUES
                    deadline_passed = (
                        PROGRAM_SYNC_DEADLINE_UTC is not None
                        and now_utc >= PROGRAM_SYNC_DEADLINE_UTC
                    )
                    if not (both_seen or deadline_passed):
                        return

                    combined = PROGRAM_LATEST_NET["KRX"] + PROGRAM_LATEST_NET["NXT"]
                    if PROGRAM_HAS_PREV_COMBINED:
                        delta = combined - PROGRAM_LAST_COMBINED
                    else:
                        delta = combined
                        PROGRAM_HAS_PREV_COMBINED = True
                    PROGRAM_LAST_COMBINED = combined
                    _enqueue_program_ui(
                        {
                            "exec_time": data.get("20", ""),
                            "net_qty": str(combined),
                            "delta_net": str(delta),
                            "_venue": venue,
                            "_venue_net": PROGRAM_LATEST_NET[venue],
                        }
                    )

                if name == "주식체결" or r_type == "0B":
                    LAST_0B_UTC = datetime.now(timezone.utc)
                    cv = data.get("13", "")
                    if cv not in ("", None):
                        LATEST_TICK["cum_vol"] = str(cv).strip()
                    LATEST_TICK.update(
                        {
                            "price": data.get("10", ""),
                            "change": data.get("11", ""),
                            "rate": data.get("12", ""),
                            "volume": data.get("15", ""),
                            "exec_time": data.get("20", ""),
                        }
                    )
                    _enqueue_tick_ui(
                        {
                            "type": "tick",
                            "price": data.get("10", ""),
                            "change": data.get("11", ""),
                            "rate": data.get("12", ""),
                            "volume": data.get("15", ""),
                            "exec_time": data.get("20", ""),
                            "cum_vol": LATEST_TICK.get("cum_vol", ""),
                            "tick_side": tick_aggressor_side(data),
                        }
                    )

                if name in ("주식당일거래원", "주식거래원") or r_type in ("0F", "05"):
                    LAST_0F_UTC = datetime.now(timezone.utc)
                    merge_top5_from_message(data)
                    sig = snapshot_rank_tuple()
                    churn = churn_vs_previous(PREV_RANK_SIG, sig)
                    PREV_RANK_SIG = sig
                    append_snapshot_log(churn, sig)
                    if churn:
                        RANK_GUI_QUEUE.put({"type": "churn", **churn})

                    snap = {
                        "price": LATEST_TICK.get("price", ""),
                        "exec_time": LATEST_TICK.get("exec_time", ""),
                    }
                    local_ts = datetime.now().strftime("%H:%M:%S")
                    evs = broker_events_from_message(data)
                    if not evs:
                        evs = rank_snapshot_fallback_events(data)
                    for evt in evs:
                        RANK_GUI_QUEUE.put(
                            {
                                "type": "foreign",
                                "side": evt["side"],
                                "broker": evt["broker"],
                                "code": evt["code"],
                                "qty": evt["qty"],
                                "ref_price": snap["price"],
                                "krx_time_raw": snap["exec_time"],
                                "local_time": local_ts,
                            }
                        )

            except Exception as e:
                LOG.error("callback error: %s", e, exc_info=True)

        for r_type in ("0B", "0F", "05", "0w", "0D"):
            bot.api.add_callback_on_real_data(real_type=r_type, callback=handle_real_data)
        # Kiwoom dispatches REAL by exact `data.type` string; normalize common casing.
        _w = bot.api._callbacks.get("0W")
        if _w:
            bot.api._callbacks["0w"] = _w
        # Also normalise 0D casing just in case.
        _d = bot.api._callbacks.get("0d")
        if _d:
            bot.api._callbacks["0D"] = _d

        await bot.connect()
        LOG.info("bot: connected")
        await bot.run()
        LOG.info("bot: run() active")

        # Order-book WebSocket bridge (React UI on ws://localhost:<HOGA_WS_PORT>)
        asyncio.create_task(run_hoga_ws_server())

        await refresh_rvol_avg(bot)
        asyncio.create_task(rvol_refresh_loop(bot))
        asyncio.create_task(ka10002_poll_loop())

        ymd_kst = datetime.now(KST).strftime("%Y%m%d")
        global PROGRAM_LAST_COMBINED, PROGRAM_HAS_PREV_COMBINED
        try:
            hist_rows, last_krx, last_nxt = await fetch_ka90008_program_intraday(
                bot.api, TARGET_TICKER, ymd_kst
            )
            # Seed live state so the first 0w push computes KRX+NXT correctly
            # (without this, combined would start at 0 + first-venue-push).
            PROGRAM_LATEST_NET["KRX"] = last_krx
            PROGRAM_LATEST_NET["NXT"] = last_nxt
            PROGRAM_LAST_COMBINED = last_krx + last_nxt
            PROGRAM_HAS_PREV_COMBINED = True
            if hist_rows:
                PROGRAM_GUI_QUEUE.put_nowait(
                    {"type": "program_history_seed", "rows": hist_rows}
                )
                LOG.info(
                    "ka90008: queued %s combined program history rows (date=%s stk=%s "
                    "seed KRX=%s NXT=%s sum=%s)",
                    len(hist_rows),
                    ymd_kst,
                    TARGET_TICKER,
                    last_krx,
                    last_nxt,
                    PROGRAM_LAST_COMBINED,
                )
            else:
                LOG.info(
                    "ka90008: no program history rows (date=%s stk=%s)",
                    ymd_kst,
                    TARGET_TICKER,
                )
        except Exception as e:
            LOG.error("ka90008 history fetch: %s", e, exc_info=True)

        end_sh = datetime.now(KST).strftime("%Y%m%d")
        start_sh = (datetime.now(KST) - timedelta(days=SHORT_SELL_LOOKBACK_DAYS)).strftime("%Y%m%d")
        try:
            sh_rows = await fetch_ka10014_short_selling_trend(
                bot.api, TARGET_TICKER, start_sh, end_sh, tm_tp="1"
            )
            if sh_rows:
                SHORT_GUI_QUEUE.put_nowait({"type": "short_sell_seed", "rows": sh_rows})
                LOG.info(
                    "ka10014: queued %s short-selling rows (%s–%s stk=%s)",
                    len(sh_rows),
                    start_sh,
                    end_sh,
                    TARGET_TICKER,
                )
            else:
                LOG.info(
                    "ka10014: no short-selling rows (%s–%s stk=%s)",
                    start_sh,
                    end_sh,
                    TARGET_TICKER,
                )
        except Exception as e:
            LOG.error("ka10014 fetch: %s", e, exc_info=True)

        await asyncio.sleep(0.08)

        try:
            await register_stock_and_broker_realtime(
                bot.api,
                grp_no="1",
                codes=[TARGET_TICKER],
            )
            LOG.info(
                "stream: REG attached (0B, 0F, 0D on %s; %s on %s; hoga_ws=ws://localhost:%s)",
                TARGET_TICKER,
                PROGRAM_TRADE_REAL_REG,
                _program_trade_reg_items([TARGET_TICKER]),
                HOGA_WS_PORT,
            )
        except Exception as e:
            LOG.error("subscription error: %s", e, exc_info=True)

        while True:
            await asyncio.sleep(1)


def start_background_loop():
    try:
        asyncio.run(run_bot_async())
    except Exception as e:
        LOG.critical("bot: crashed: %s", e, exc_info=True)


class KiwoomApp(QMainWindow):
    chart_export_ready = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(1100, 780)
        self.setMinimumSize(900, 640)

        # Muted paper / ink — no traffic-light greens or reds.
        self.bg = "#16141c"
        self.card = "#1f1c28"
        self.muted = "#7a7389"
        self.text = "#d8d2e4"
        self.dim = "#5c566b"
        self.lane_a = "#b8a9d9"
        self.lane_b = "#7eb8b8"
        self.ref_muted = "#9a93a8"
        self.table_bg = "#242130"
        self.table_hdr = "#2f2a3d"
        self.table_sel = "#3d3550"
        self.border_muted = "#353047"
        self.tape_buy_fg = "#ffffff"
        self.tape_sell_fg = "#7e22ce"

        self._font_ui = QFont("Segoe UI", 10)
        self._font_sm = QFont("Segoe UI", 9)
        self._font_mono = QFont("Consolas", 9)
        self._font_hdr = QFont("Segoe UI", 9)
        self._font_hdr.setBold(True)

        self._max_tree_rows = 120
        self._member_filter: str | None = None
        self._session_broker_events: deque = deque(maxlen=SESSION_BROKER_EVENT_MAX)
        self._min_tape_qty = 0
        self._tape_ring: deque = deque(maxlen=TAPE_MAX_ROWS)
        self._program_ring: deque = deque(maxlen=PROGRAM_TRADE_MAX_ROWS)
        self._tape_scroll_ratio = 0.0
        self._tape_refreshing = False
        self._last_stale_warn_utc: datetime | None = None
        _loaded = load_broker_session_cache_rows()
        if len(_loaded) > SESSION_BROKER_EVENT_MAX:
            _loaded = _loaded[-SESSION_BROKER_EVENT_MAX :]
        for _r in _loaded:
            self._session_broker_events.append(_r)

        self.setStyleSheet(
            f"""
            QMainWindow {{ background-color: {self.bg}; }}
            QToolTip {{ background-color: {self.card}; color: {self.text}; border: 1px solid {self.border_muted}; }}
            """
        )

        self.chart_export_ready.connect(self._apply_chart_export_text)
        self.setup_ui()
        QTimer.singleShot(0, self.poll_queue)

    def _apply_table_style(self, table: QTableWidget) -> None:
        table.setAlternatingRowColors(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(26)
        table.setShowGrid(False)
        table.setStyleSheet(
            f"""
            QTableWidget {{
                background-color: {self.table_bg};
                color: {self.text};
                gridline-color: {self.border_muted};
                border: none;
                font-family: Consolas;
                font-size: 9pt;
            }}
            QTableWidget::item:selected {{
                background-color: {self.table_sel};
            }}
            QHeaderView::section {{
                background-color: {self.table_hdr};
                color: {self.muted};
                padding: 4px;
                border: none;
                font-family: Segoe UI;
                font-size: 9pt;
                font-weight: bold;
            }}
            """
        )

    def _cell(
        self,
        text: str,
        fg: str | None = None,
        align: Qt.AlignmentFlag = Qt.AlignLeft | Qt.AlignVCenter,
    ) -> QTableWidgetItem:
        it = QTableWidgetItem(str(text))
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
        it.setTextAlignment(int(align))
        # Default item background is often light; tape uses near-white for buys — without this, buy rows look empty.
        it.setBackground(QBrush(QColor(self.table_bg)))
        if fg:
            it.setForeground(QBrush(QColor(fg)))
        return it

    def _program_metric_cell(self, raw: str) -> QTableWidgetItem:
        disp = str(raw).strip()
        n = parse_signed_int_metric(disp)
        align = Qt.AlignRight | Qt.AlignVCenter
        if n is None:
            return self._cell(disp, align=align)
        if n > 0:
            return self._cell(disp, fg=self.tape_buy_fg, align=align)
        if n < 0:
            return self._cell(disp, fg=self.tape_sell_fg, align=align)
        return self._cell(disp, align=align)

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(0)

        header = QFrame()
        header.setStyleSheet(
            f"QFrame {{ background-color: {self.card}; border: 1px solid {self.border_muted}; }}"
        )
        header_l = QVBoxLayout(header)
        header_l.setContentsMargins(16, 12, 16, 12)
        outer.addWidget(header)
        outer.addSpacing(10)

        lbl_sess = QLabel(SESSION_MARK)
        lbl_sess.setFont(QFont("Segoe UI", 11))
        lbl_sess.setStyleSheet(f"color: {self.dim}; background: transparent; border: none;")
        header_l.addWidget(lbl_sess)

        self.lbl_metrics = QLabel("ref — · delta — · pct — · vol —")
        self.lbl_metrics.setFont(self._font_mono)
        self.lbl_metrics.setStyleSheet(f"color: {self.ref_muted}; background: transparent; border: none;")
        header_l.addWidget(self.lbl_metrics)
        header_l.addSpacing(6)

        self.lbl_rvol = QLabel("RVOL — (need session cum vol FID 13 + daily baseline)")
        self.lbl_rvol.setFont(self._font_sm)
        self.lbl_rvol.setStyleSheet(f"color: {self.dim}; background: transparent; border: none;")
        header_l.addWidget(self.lbl_rvol)
        header_l.addSpacing(4)

        row_meta = QHBoxLayout()
        row_meta.setSpacing(0)
        self.lbl_churn = QLabel("chg —")
        self.lbl_churn.setFont(self._font_mono)
        self.lbl_churn.setStyleSheet(f"color: {self.muted}; background: transparent; border: none;")
        row_meta.addWidget(self.lbl_churn)
        sep = QLabel("   ·   ")
        sep.setFont(self._font_mono)
        sep.setStyleSheet(f"color: {self.dim}; background: transparent; border: none;")
        row_meta.addWidget(sep)
        self.lbl_visibility = QLabel("visibility: ranks 1–5 · tail unseen")
        self.lbl_visibility.setFont(self._font_sm)
        self.lbl_visibility.setStyleSheet(f"color: {self.dim}; background: transparent; border: none;")
        row_meta.addWidget(self.lbl_visibility)
        row_meta.addStretch(1)
        header_l.addLayout(row_meta)

        def _hint_lbl(txt: str) -> QLabel:
            lb = QLabel(txt)
            lb.setFont(self._font_sm)
            lb.setWordWrap(True)
            lb.setMaximumWidth(1040)
            lb.setStyleSheet(f"color: {self.dim}; background: transparent; border: none;")
            return lb

        outer.addWidget(
            _hint_lbl(
                "What this uses: (1) Real-time 0B = each public trade print (time, ref, print size). "
                "Broker is NOT attached to each trade on this feed. "
                "(2) Real-time 0F = top-5 ranked buyers (lane A) and top-5 ranked sellers (lane B) with slot sizes — "
                "not the full market, and not a full time-and-sales by firm. "
                "(3) RVOL = session cumulative volume (FID 13 when sent) divided by average prior full-day volume from REST daily bars."
            )
        )
        outer.addSpacing(4)
        outer.addWidget(
            _hint_lbl(
                "Ranks: append-only. Double-click a rank row or use Member code + Show session (0F top-5 feed only, since app start). Esc clears ranks focus."
            )
        )
        outer.addSpacing(2)
        outer.addWidget(
            _hint_lbl(
                "Ranks table: Shift/Ctrl or drag to select · Ctrl+A all · Ctrl+C copy TSV · resize columns by dragging headers."
            )
        )
        outer.addSpacing(6)

        tape_gb = QGroupBox(
            "Tape — tab1: 0B · tab2: program (0w / ka90008) · tab3: short selling (ka10014 REST)"
        )
        tape_gb.setFont(self._font_sm)
        tape_gb.setStyleSheet(
            f"QGroupBox {{ color: {self.muted}; border: 1px solid {self.border_muted}; margin-top: 8px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}"
        )
        tape_outer = QVBoxLayout(tape_gb)
        tape_outer.setContentsMargins(6, 12, 6, 4)
        self._tape_tabs = QTabWidget()
        self._tape_tabs.setStyleSheet(
            f"""
            QTabWidget::pane {{ border: 1px solid {self.border_muted}; background: {self.table_bg}; }}
            QTabBar::tab {{
                background: {self.table_hdr};
                color: {self.muted};
                padding: 6px 14px;
                border: 1px solid {self.border_muted};
            }}
            QTabBar::tab:selected {{ color: {self.text}; background: {self.table_bg}; }}
            """
        )
        tab_0b = QWidget()
        tab_0b_l = QVBoxLayout(tab_0b)
        tab_0b_l.setContentsMargins(0, 8, 0, 0)
        tape_bar = QHBoxLayout()
        lbl_tmin = QLabel("Min print sz")
        lbl_tmin.setFont(self._font_sm)
        lbl_tmin.setStyleSheet(f"color: {self.dim}; background: transparent;")
        tape_bar.addWidget(lbl_tmin)
        tape_bar.addSpacing(6)
        self._min_tape_qty_group = QButtonGroup(self)
        for v in TAPE_MIN_PRINT_CHOICES:
            lab = "All" if v == 0 else f"≥{v}"
            rb = QRadioButton(lab)
            rb.setFont(self._font_sm)
            rb.setStyleSheet(
                f"QRadioButton {{ color: {self.text}; background: transparent; spacing: 4px; }}"
            )
            self._min_tape_qty_group.addButton(rb, int(v))
            if v == 0:
                rb.setChecked(True)
            tape_bar.addWidget(rb)
            tape_bar.addSpacing(10)
        tape_bar.addStretch(1)
        self._min_tape_qty_group.buttonClicked.connect(self._on_min_tape_qty_change)
        tab_0b_l.addLayout(tape_bar)
        tab_0b_l.addSpacing(4)
        self.tape = QTextEdit()
        self.tape.setReadOnly(True)
        self.tape.setAcceptRichText(True)
        self.tape.setLineWrapMode(QTextEdit.NoWrap)
        self.tape.setFont(self._font_mono)
        self.tape.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.tape.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tape.setStyleSheet(
            f"""
            QTextEdit {{
                background-color: {self.table_bg};
                border: 1px solid {self.border_muted};
                padding: 6px;
            }}
            """
        )
        self.tape.setFixedHeight(200)
        self.tape.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.tape.verticalScrollBar().valueChanged.connect(self._on_tape_scroll_value)
        tab_0b_l.addWidget(self.tape)
        self._tape_tabs.addTab(
            tab_0b,
            "Live trades (0B) — buy=white · sell=purple · min filter · queue lossless",
        )

        tab_0w = QWidget()
        tab_0w_l = QVBoxLayout(tab_0w)
        tab_0w_l.setContentsMargins(0, 8, 0, 0)
        lbl_pw = QLabel("ka90008 seed + live 0w. Newest at top.")
        lbl_pw.setFont(self._font_sm)
        lbl_pw.setWordWrap(True)
        lbl_pw.setStyleSheet(f"color: {self.dim}; background: transparent;")
        tab_0w_l.addWidget(lbl_pw)
        tab_0w_l.addSpacing(4)
        self.table_program = QTableWidget(0, 3)
        self.table_program.setHorizontalHeaderLabels(
            [
                "Time (FID 20 / local)",
                "순매수수량",
                "순매수수량증감",
            ]
        )
        self._apply_table_style(self.table_program)
        self.table_program.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table_program.setFocusPolicy(Qt.StrongFocus)
        for col, w in enumerate((160, 120, 120)):
            self.table_program.setColumnWidth(col, w)
            self.table_program.horizontalHeader().setSectionResizeMode(col, QHeaderView.Interactive)
        self.table_program.setMinimumHeight(200)
        self.table_program.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        tab_0w_l.addWidget(self.table_program)
        self._tape_tabs.addTab(tab_0w, "Program trading (0w)")
        tab_sh = QWidget()
        tab_sh_l = QVBoxLayout(tab_sh)
        tab_sh_l.setContentsMargins(0, 8, 0, 0)
        lbl_sh = QLabel(
            f"Short selling trend (ka10014, /api/dostk/shsa): last ~{SHORT_SELL_LOOKBACK_DAYS} calendar days, "
            "newest date first. Loaded once at connect (not live)."
        )
        lbl_sh.setFont(self._font_sm)
        lbl_sh.setWordWrap(True)
        lbl_sh.setStyleSheet(f"color: {self.dim}; background: transparent;")
        tab_sh_l.addWidget(lbl_sh)
        tab_sh_l.addSpacing(4)
        self._short_sell_columns = (
            "dt",
            "close_pric",
            "pred_pre_sig",
            "pred_pre",
            "flu_rt",
            "trde_qty",
            "shrts_qty",
            "ovr_shrts_qty",
            "trde_wght",
            "shrts_trde_prica",
            "shrts_avg_pric",
        )
        self.table_short = QTableWidget(0, len(self._short_sell_columns))
        self.table_short.setHorizontalHeaderLabels(
            [
                "Date",
                "Close",
                "vs",
                "Chg",
                "%",
                "Volume",
                "Short sh",
                "Cum short",
                "Wt%",
                "Short value",
                "Sh avg",
            ]
        )
        self._apply_table_style(self.table_short)
        self.table_short.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table_short.setFocusPolicy(Qt.StrongFocus)
        for col, w in enumerate((86, 72, 36, 72, 48, 84, 84, 84, 52, 84, 72)):
            self.table_short.setColumnWidth(col, w)
            self.table_short.horizontalHeader().setSectionResizeMode(col, QHeaderView.Interactive)
        self.table_short.setMinimumHeight(200)
        self.table_short.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        tab_sh_l.addWidget(self.table_short)
        self._tape_tabs.addTab(tab_sh, "Short selling (ka10014)")
        tape_outer.addWidget(self._tape_tabs)
        outer.addWidget(tape_gb)
        outer.addSpacing(6)

        chart_gb = QGroupBox(
            "OHLCV text — REST chart (ka10081 daily, ka10080 1m/5m). Load candles, Ctrl+A / Ctrl+C."
        )
        chart_gb.setFont(self._font_sm)
        chart_gb.setStyleSheet(
            f"QGroupBox {{ color: {self.muted}; border: 1px solid {self.border_muted}; margin-top: 8px; }}"
            f"QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}"
        )
        chart_outer = QVBoxLayout(chart_gb)
        chart_outer.setContentsMargins(6, 12, 6, 6)
        chart_bar = QHBoxLayout()
        self._rb_chart_daily = QRadioButton("Daily")
        self._rb_chart_5m = QRadioButton("5-minute")
        self._rb_chart_1m = QRadioButton("1-minute")
        for rb in (self._rb_chart_daily, self._rb_chart_5m, self._rb_chart_1m):
            rb.setFont(self._font_sm)
            rb.setStyleSheet(f"QRadioButton {{ color: {self.text}; background: transparent; }}")
        self._rb_chart_daily.setChecked(True)
        chart_bar.addWidget(self._rb_chart_daily)
        chart_bar.addWidget(self._rb_chart_5m)
        chart_bar.addWidget(self._rb_chart_1m)
        chart_bar.addSpacing(12)
        btn_chart = QPushButton("Load candles")
        btn_chart.setFont(self._font_sm)
        btn_chart.setStyleSheet(
            f"QPushButton {{ background-color: {self.table_hdr}; color: {self.text}; padding: 6px 12px; border: none; }}"
            f"QPushButton:hover {{ background-color: {self.table_sel}; }}"
        )
        btn_chart.clicked.connect(self._on_chart_export_clicked)
        chart_bar.addWidget(btn_chart)
        chart_bar.addStretch(1)
        chart_outer.addLayout(chart_bar)
        self.txt_chart_export = QTextEdit()
        self.txt_chart_export.setReadOnly(True)
        self.txt_chart_export.setFont(self._font_mono)
        self.txt_chart_export.setPlaceholderText('Click “Load candles” after the bot connects.')
        self.txt_chart_export.setStyleSheet(
            f"""
            QTextEdit {{
                background-color: {self.table_bg};
                color: {self.text};
                border: 1px solid {self.border_muted};
                padding: 6px;
            }}
            """
        )
        self.txt_chart_export.setFixedHeight(200)
        chart_outer.addWidget(self.txt_chart_export)
        outer.addWidget(chart_gb)
        outer.addSpacing(6)

        paned = QSplitter(Qt.Horizontal)
        paned.setStyleSheet(
            f"QSplitter::handle {{ background: {self.border_muted}; width: 5px; }}"
        )
        left = QWidget()
        right = QWidget()
        left.setStyleSheet(f"background-color: {self.bg};")
        right.setStyleSheet(f"background-color: {self.bg};")
        paned.addWidget(left)
        paned.addWidget(right)
        paned.setStretchFactor(0, 1)
        paned.setStretchFactor(1, 1)
        paned.setSizes([520, 360])
        outer.addWidget(paned, stretch=1)

        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(0, 0, 8, 0)
        rank_hdr = QHBoxLayout()
        lbl_rank = QLabel("Top-5 rank updates (0F)")
        lbl_rank.setFont(self._font_sm)
        lbl_rank.setStyleSheet(f"color: {self.muted}; background: transparent;")
        rank_hdr.addWidget(lbl_rank)
        rank_hdr.addStretch(1)
        left_l.addLayout(rank_hdr)
        left_l.addSpacing(2)

        table_wrap = QFrame()
        table_wrap.setStyleSheet(f"QFrame {{ background-color: {self.border_muted}; border: none; }}")
        tw_l = QVBoxLayout(table_wrap)
        tw_l.setContentsMargins(1, 1, 1, 1)
        self._tree_columns = ("time", "side", "broker", "qty", "price", "code")
        self.tree = QTableWidget(0, 6)
        self.tree.setHorizontalHeaderLabels(
            ["Time (local / exch)", "lane", "Member", "Qty", "ref @ cap", ""]
        )
        self._apply_table_style(self.tree)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setFocusPolicy(Qt.StrongFocus)
        self.tree.setColumnHidden(5, True)
        for col, w in enumerate((190, 52, 220, 96, 100, 0)):
            self.tree.setColumnWidth(col, w)
            self.tree.horizontalHeader().setSectionResizeMode(col, QHeaderView.Interactive)
        tw_l.addWidget(self.tree)
        left_l.addWidget(table_wrap, stretch=1)

        # IMPORTANT: Scope shortcuts to the ranks table only; otherwise Ctrl+C while focused on the tape
        # can incorrectly copy the ranks table selection.
        sc_sel_all = QShortcut(QKeySequence.SelectAll, self.tree)
        sc_sel_all.setContext(Qt.WidgetShortcut)
        sc_sel_all.activated.connect(self._tree_select_all)

        sc_copy = QShortcut(QKeySequence.Copy, self.tree)
        sc_copy.setContext(Qt.WidgetShortcut)
        sc_copy.activated.connect(self._tree_copy_selection)

        # Tape copy/select-all should operate on the tape when it has focus/selection.
        tape_sel_all = QShortcut(QKeySequence.SelectAll, self.tape)
        tape_sel_all.setContext(Qt.WidgetShortcut)
        tape_sel_all.activated.connect(self.tape.selectAll)

        tape_copy = QShortcut(QKeySequence.Copy, self.tape)
        tape_copy.setContext(Qt.WidgetShortcut)
        tape_copy.activated.connect(self.tape.copy)
        prog_sel_all = QShortcut(QKeySequence.SelectAll, self.table_program)
        prog_sel_all.setContext(Qt.WidgetShortcut)
        prog_sel_all.activated.connect(self.table_program.selectAll)
        prog_copy = QShortcut(QKeySequence.Copy, self.table_program)
        prog_copy.setContext(Qt.WidgetShortcut)
        prog_copy.activated.connect(self._program_table_copy_selection)
        sh_sel_all = QShortcut(QKeySequence.SelectAll, self.table_short)
        sh_sel_all.setContext(Qt.WidgetShortcut)
        sh_sel_all.activated.connect(self.table_short.selectAll)
        sh_copy = QShortcut(QKeySequence.Copy, self.table_short)
        sh_copy.setContext(Qt.WidgetShortcut)
        sh_copy.activated.connect(self._short_sell_table_copy_selection)
        esc_sc = QShortcut(QKeySequence(Qt.Key_Escape), self.tree)
        esc_sc.setContext(Qt.WidgetShortcut)
        esc_sc.activated.connect(self._clear_member_focus)
        self.tree.cellDoubleClicked.connect(self._on_rank_double_click)

        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(8, 0, 0, 0)
        member_bar = QHBoxLayout()
        lbl_m = QLabel("Member")
        lbl_m.setFont(self._font_sm)
        lbl_m.setStyleSheet(f"color: {self.dim}; background: transparent;")
        member_bar.addWidget(lbl_m)
        member_bar.addSpacing(8)
        _pick = sorted(BROKER_CODE_ENGLISH.keys(), key=lambda x: int(x))
        _combo_vals = [COMBO_ALL_LABEL] + [f"{c} - {BROKER_CODE_ENGLISH[c]}" for c in _pick]
        self.cmb_member = QComboBox()
        self.cmb_member.setEditable(True)
        self.cmb_member.addItems(_combo_vals)
        self.cmb_member.setFont(self._font_sm)
        self.cmb_member.setMinimumWidth(320)
        self.cmb_member.setStyleSheet(
            f"""
            QComboBox {{
                background-color: {self.table_bg};
                color: {self.text};
                border: 1px solid {self.border_muted};
                padding: 4px 8px;
            }}
            QComboBox::drop-down {{ border: none; }}
            """
        )
        self.cmb_member.lineEdit().setStyleSheet(f"background-color: {self.table_bg}; color: {self.text};")
        self.cmb_member.activated.connect(lambda _i: self._on_apply_member_filter(force=False))
        self.cmb_member.lineEdit().returnPressed.connect(lambda: self._on_apply_member_filter(force=True))
        member_bar.addWidget(self.cmb_member)
        member_bar.addSpacing(8)
        btn_style = (
            f"QPushButton {{ background-color: {self.table_hdr}; color: {self.text}; padding: 6px 12px; border: none; }}"
            f"QPushButton:hover {{ background-color: {self.table_sel}; }}"
        )
        btn_show = QPushButton("Show session")
        btn_show.clicked.connect(lambda: self._on_apply_member_filter(force=True))
        btn_show.setStyleSheet(btn_style)
        member_bar.addWidget(btn_show)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._clear_member_focus)
        btn_clear.setStyleSheet(btn_style)
        member_bar.addWidget(btn_clear)
        member_bar.addStretch(1)
        right_l.addLayout(member_bar)

        self.lbl_member_focus = QLabel(
            "Member panel: type code (e.g. 025) or pick a firm, then Show session. "
            "List = today’s 0F lines from this app (KST date file on disk + live). "
            "Not the exchange’s full official day unless the feed was running since the open."
        )
        self.lbl_member_focus.setFont(self._font_sm)
        self.lbl_member_focus.setWordWrap(True)
        self.lbl_member_focus.setMaximumWidth(520)
        self.lbl_member_focus.setStyleSheet(f"color: {self.muted}; background: transparent;")
        right_l.addWidget(self.lbl_member_focus)
        right_l.addSpacing(2)

        mf_wrap = QFrame()
        mf_wrap.setStyleSheet(f"QFrame {{ background-color: {self.border_muted}; border: none; }}")
        mf_l = QVBoxLayout(mf_wrap)
        mf_l.setContentsMargins(1, 1, 1, 1)
        self._mf_columns = ("time", "side", "broker", "qty", "price")
        self.tree_mf = QTableWidget(0, 5)
        self.tree_mf.setHorizontalHeaderLabels(
            ["Time (local / exch)", "lane", "Member", "Qty", "ref @ cap"]
        )
        self._apply_table_style(self.tree_mf)
        self.tree_mf.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree_mf.setFocusPolicy(Qt.NoFocus)
        for col, w in enumerate((160, 40, 180, 72, 72)):
            self.tree_mf.setColumnWidth(col, w)
            self.tree_mf.horizontalHeader().setSectionResizeMode(col, QHeaderView.Interactive)
        mf_l.addWidget(self.tree_mf)
        right_l.addWidget(mf_wrap, stretch=1)

        self._refresh_tape_text()
        self._refresh_program_table()
        self._populate_short_sell_table([])

    def _short_sell_table_copy_selection(self) -> None:
        sel = self.table_short.selectionModel().selectedRows()
        if not sel:
            rows = list(range(self.table_short.rowCount()))
        else:
            rows = sorted({ix.row() for ix in sel})
        if not rows:
            return
        headers = [
            self.table_short.horizontalHeaderItem(c).text()
            for c in range(self.table_short.columnCount())
        ]
        lines = ["\t".join(headers)]
        for r in rows:
            parts = []
            for c in range(self.table_short.columnCount()):
                it = self.table_short.item(r, c)
                parts.append(it.text() if it else "")
            lines.append("\t".join(parts))
        QApplication.clipboard().setText("\n".join(lines))

    def _populate_short_sell_table(self, rows: list[dict]) -> None:
        self.table_short.setRowCount(0)
        align_r = Qt.AlignRight | Qt.AlignVCenter
        align_l = Qt.AlignLeft | Qt.AlignVCenter
        for row in rows:
            r = self.table_short.rowCount()
            self.table_short.insertRow(r)
            for c, key in enumerate(self._short_sell_columns):
                raw = row.get(key, "")
                txt = str(raw).strip() if raw is not None else ""
                al = align_l if c in (0, 2) else align_r
                self.table_short.setItem(r, c, self._cell(txt, align=al))

    def _program_table_copy_selection(self) -> None:
        sel = self.table_program.selectionModel().selectedRows()
        if not sel:
            rows = list(range(self.table_program.rowCount()))
        else:
            rows = sorted({ix.row() for ix in sel})
        if not rows:
            return
        headers = [
            self.table_program.horizontalHeaderItem(c).text()
            for c in range(self.table_program.columnCount())
        ]
        lines = ["\t".join(headers)]
        for r in rows:
            parts = []
            for c in range(self.table_program.columnCount()):
                it = self.table_program.item(r, c)
                parts.append(it.text() if it else "")
            lines.append("\t".join(parts))
        QApplication.clipboard().setText("\n".join(lines))

    def _refresh_program_table(self) -> None:
        self.table_program.setRowCount(0)
        # deque is oldest→newest; show newest first.
        for local_ts, row in reversed(list(self._program_ring)):
            r = self.table_program.rowCount()
            self.table_program.insertRow(r)
            t_cell = self._time_cell(local_ts, str(row.get("exec_time", "")))
            self.table_program.setItem(r, 0, self._cell(t_cell))
            self.table_program.setItem(
                r, 1, self._program_metric_cell(str(row.get("net_qty", "")))
            )
            self.table_program.setItem(
                r, 2, self._program_metric_cell(str(row.get("delta_net", "")))
            )

    def _apply_chart_export_text(self, text: str) -> None:
        self.txt_chart_export.setPlainText(text)

    def _on_chart_export_clicked(self) -> None:
        lp = BOT_LOOP
        bot = BOT_CHART_BOT
        if lp is None or bot is None:
            self.txt_chart_export.setPlainText("Error: bot not connected yet.\n")
            LOG.warning(
                "chart_export: click ignored — BOT_LOOP=%s BOT_CHART_BOT=%s",
                lp,
                bot,
            )
            return
        if self._rb_chart_daily.isChecked():
            tf = "day"
        elif self._rb_chart_5m.isChecked():
            tf = "min5"
        else:
            tf = "min1"
        LOG.info("chart_export: UI click tf=%s ticker=%s", tf, TARGET_TICKER)
        self.txt_chart_export.setPlainText("Loading…")

        def _done(fut):
            try:
                text = fut.result()
            except Exception as e:
                LOG.error("chart_export: future exception tf=%s: %s", tf, e, exc_info=True)
                text = f"Error: {e}\n"
            try:
                self.chart_export_ready.emit(text)
            except Exception as e2:
                LOG.critical("chart_export: emit failed: %s", e2, exc_info=True)

        fut = asyncio.run_coroutine_threadsafe(
            build_chart_export_for_timeframe(bot, TARGET_TICKER, tf), lp
        )
        fut.add_done_callback(_done)

    def _tree_select_all(self) -> None:
        self.tree.selectAll()

    def _tree_copy_selection(self) -> None:
        sel = self.tree.selectionModel().selectedRows()
        if not sel:
            rows = list(range(self.tree.rowCount()))
        else:
            rows = sorted({ix.row() for ix in sel})
        if not rows:
            return
        disp_ix = [0, 1, 2, 3, 4]
        headers = [self.tree.horizontalHeaderItem(c).text() for c in disp_ix]
        lines = ["\t".join(headers)]
        for r in rows:
            parts = []
            for c in disp_ix:
                it = self.tree.item(r, c)
                parts.append(it.text() if it else "")
            lines.append("\t".join(parts))
        QApplication.clipboard().setText("\n".join(lines))

    def format_num(self, val_str):
        clean = str(val_str).replace("+", "").replace("-", "").strip()
        try:
            return f"{int(clean):,}"
        except Exception:
            return clean

    def _time_cell(self, local_ts: str, krx_raw: str) -> str:
        k = format_krx_hhmmss(krx_raw)
        if k:
            loc = str(local_ts).strip()
            return k if not loc else f"{loc}  ·  {k}"
        return str(local_ts).strip() or "—"

    def _trim_tree(self):
        while self.tree.rowCount() > self._max_tree_rows:
            self.tree.removeRow(self.tree.rowCount() - 1)

    def _tape_print_meets_min(self, data: dict) -> bool:
        m = self._min_tape_qty
        if m <= 0:
            return True
        q = parse_share_qty(data.get("volume", ""))
        if q is None:
            return True
        return abs(q) >= m

    def _on_min_tape_qty_change(self, _button) -> None:
        bid = self._min_tape_qty_group.checkedId()
        self._min_tape_qty = bid if bid >= 0 else 0
        self._refresh_tape_text()

    def _format_tape_line_parts(self, data: dict, local_ts: str) -> tuple[str, str]:
        exec_raw = str(data.get("exec_time", "")).strip()
        exch = format_krx_hhmmss(exec_raw)
        pr = str(data.get("price", "")).strip()
        ch = str(data.get("change", "")).strip()
        sign = "+" if "+" in ch else "-" if "-" in ch else ""
        vol = str(data.get("volume", "")).strip()
        cum = str(data.get("cum_vol", "")).strip()
        ref_disp = self.format_num(pr) if pr else "—"
        d_clean = ch.replace("+", "").replace("-", "").strip()
        try:
            delta_disp = f"{sign}{self.format_num(d_clean)}" if d_clean else "—"
        except Exception:
            delta_disp = ch or "—"
        side = str(data.get("tick_side", "neutral"))
        q_print = parse_share_qty(vol)
        if not vol or q_print is None:
            pr_sz = "—"
        else:
            mag = self.format_num(str(abs(q_print)))
            if side == "buy":
                pr_sz = f"+{mag}"
            elif side == "sell":
                pr_sz = f"-{mag}"
            else:
                pr_sz = f"+{mag}" if q_print >= 0 else f"-{mag}"
        cum_disp = self.format_num(cum) if cum else "—"
        # Show both exchange time (FID 20) and receipt time, so delays/missing timestamps are obvious.
        time_disp = f"{exch} ({local_ts})" if exch else f"— ({local_ts})"
        if not exch:
            LOG.warning("tick: missing exec_time fid20 (local_ts=%s payload=%s)", local_ts, data)
        else:
            try:
                local_dt = _kst_today_dt_from_hhmmss(local_ts.replace(":", ""))
                exch_dt = _kst_today_dt_from_hhmmss(exec_raw)
                if local_dt and exch_dt:
                    delay = (local_dt - exch_dt).total_seconds()
                    if delay >= 3:
                        LOG.info(
                            "tick: delay %ss (exch=%s local=%s price=%s vol=%s)",
                            int(delay),
                            exch,
                            local_ts,
                            pr,
                            vol,
                        )
            except Exception:
                LOG.debug("tick: delay calc failed\n%s", traceback.format_exc())
        body = f"{time_disp}\t{ref_disp}\t{delta_disp}\t{pr_sz}\t{cum_disp}"
        return side, body

    def _on_tape_scroll_value(self, _value: int) -> None:
        if self._tape_refreshing:
            return
        sb = self.tape.verticalScrollBar()
        mx = sb.maximum()
        self._tape_scroll_ratio = 0.0 if mx <= 0 else sb.value() / mx

    def _tape_set_placeholder_html(self, message: str) -> None:
        self._tape_refreshing = True
        self._tape_scroll_ratio = 0.0
        esc = html.escape(message)
        self.tape.setAcceptRichText(True)
        self.tape.setHtml(
            f'<div style="font-family:Consolas,monospace;font-size:9pt;color:{self.muted};">{esc}</div>'
        )
        self._tape_refreshing = False

    def _refresh_tape_text(self) -> None:
        if not self._tape_ring:
            self._tape_set_placeholder_html(
                "(No 0B prints in buffer yet — if the header ref/vol updates, ticks are arriving; "
                "if not, check Kiwoom stream / market hours.)"
            )
            return
        rows_html: list[str] = []
        for data, local_ts in reversed(self._tape_ring):
            if not self._tape_print_meets_min(data):
                continue
            side, body = self._format_tape_line_parts(data, local_ts)
            if side == "buy":
                color = self.tape_buy_fg
            elif side == "sell":
                color = self.tape_sell_fg
            else:
                color = self.muted
            rows_html.append(
                f'<span style="color:{color}; white-space:pre">{html.escape(body)}</span>'
            )
            if len(rows_html) >= TAPE_MAX_ROWS:
                break
        if not rows_html:
            self._tape_set_placeholder_html(
                f"(No prints meet min size ≥ {self._min_tape_qty} — select “All” or a lower min print sz.)"
            )
            return
        block = (
            f'<div style="font-family:Consolas,monospace;font-size:9pt;">'
            + "<br/>".join(rows_html)
            + "</div>"
        )
        self._tape_refreshing = True
        self.tape.setAcceptRichText(True)
        self.tape.setHtml(block)

        def _restore_tape_scroll() -> None:
            try:
                sb = self.tape.verticalScrollBar()
                mx = sb.maximum()
                if mx <= 0:
                    sb.setValue(0)
                else:
                    sb.setValue(int(round(self._tape_scroll_ratio * mx)))
            finally:
                self._tape_refreshing = False

        QTimer.singleShot(0, _restore_tape_scroll)

    def _row_fg(self, side: str) -> str:
        return self.lane_a if side == "buy" else self.lane_b

    def _insert_rank_row_only(self, payload: dict) -> None:
        side = payload["side"]
        fg = self._row_fg(side)
        side_txt = "A" if side == "buy" else "B"
        code = str(payload.get("code", "")).zfill(3)
        member_disp = (payload.get("broker") or "").strip() or english_broker_label(code)
        qty_disp = self.format_num(payload.get("qty", ""))
        pr = payload.get("ref_price", "")
        price_disp = self.format_num(pr) if pr not in ("", None) else "—"
        time_disp = self._time_cell(payload.get("local_time", ""), payload.get("krx_time_raw", ""))
        self.tree.insertRow(0)
        vals = (time_disp, side_txt, member_disp, qty_disp, price_disp, code)
        aligns = (
            Qt.AlignLeft | Qt.AlignVCenter,
            Qt.AlignCenter,
            Qt.AlignLeft | Qt.AlignVCenter,
            Qt.AlignRight | Qt.AlignVCenter,
            Qt.AlignRight | Qt.AlignVCenter,
            Qt.AlignLeft | Qt.AlignVCenter,
        )
        for col, (v, al) in enumerate(zip(vals, aligns)):
            self.tree.setItem(0, col, self._cell(v, fg=fg, align=al))

    def _add_foreign_row(self, payload: dict):
        self._insert_rank_row_only(payload)
        self._trim_tree()
        self._add_member_focus_row(payload)

    def _insert_mf_row(self, payload: dict) -> None:
        side = payload["side"]
        fg = self._row_fg(side)
        side_txt = "A" if side == "buy" else "B"
        code = str(payload.get("code", "")).zfill(3)
        member_disp = (payload.get("broker") or "").strip() or english_broker_label(code)
        qty_disp = self.format_num(payload.get("qty", ""))
        pr = payload.get("ref_price", "")
        price_disp = self.format_num(pr) if pr not in ("", None) else "—"
        time_disp = self._time_cell(payload.get("local_time", ""), payload.get("krx_time_raw", ""))
        self.tree_mf.insertRow(0)
        vals = (time_disp, side_txt, member_disp, qty_disp, price_disp)
        aligns = (
            Qt.AlignLeft | Qt.AlignVCenter,
            Qt.AlignCenter,
            Qt.AlignLeft | Qt.AlignVCenter,
            Qt.AlignRight | Qt.AlignVCenter,
            Qt.AlignRight | Qt.AlignVCenter,
        )
        for col, (v, al) in enumerate(zip(vals, aligns)):
            self.tree_mf.setItem(0, col, self._cell(v, fg=fg, align=al))
        while self.tree_mf.rowCount() > MEMBER_FOCUS_MAX_ROWS:
            self.tree_mf.removeRow(self.tree_mf.rowCount() - 1)

    def _add_member_focus_row(self, payload: dict):
        if not self._member_filter:
            return
        if self._member_filter == MEMBER_SHOW_ALL:
            self._insert_mf_row(payload)
            return
        code = str(payload.get("code", "")).zfill(3)
        if code != self._member_filter:
            return
        self._insert_mf_row(payload)

    def _set_combo_text(self, text: str) -> None:
        self.cmb_member.blockSignals(True)
        try:
            self.cmb_member.setCurrentText(text)
        finally:
            self.cmb_member.blockSignals(False)

    def _reset_member_panel(self, clear_combo: bool = True) -> None:
        self._member_filter = None
        self.lbl_member_focus.setText(
            "Member panel: type code or pick firm, Show session. "
            "Uses today’s cached 0F rows (KST) + live. Top-5 feed only."
        )
        self.lbl_member_focus.setStyleSheet(f"color: {self.muted}; background: transparent;")
        self.tree_mf.setRowCount(0)
        if clear_combo:
            self._set_combo_text("")

    def _on_apply_member_filter(self, force: bool = False):
        raw = self.cmb_member.currentText().strip()
        if not raw:
            self._reset_member_panel(clear_combo=False)
            return
        code = parse_member_pick(raw)
        if not code or code == "000":
            self._reset_member_panel(clear_combo=False)
            return
        self._apply_member_filter_code(code)

    def _apply_member_filter_code(self, code: str) -> None:
        if code == MEMBER_SHOW_ALL:
            self._member_filter = MEMBER_SHOW_ALL
            total_cached = len(self._session_broker_events)
            self.lbl_member_focus.setText(
                f"All members — {total_cached} line(s) in today’s store (0F rank updates only). "
                "This is not the exchange’s full official day unless the app + cache ran since the open."
            )
            self.lbl_member_focus.setStyleSheet(f"color: {self.muted}; background: transparent;")
            self.tree_mf.setRowCount(0)
            for p in self._session_broker_events:
                self._insert_mf_row(p)
            self._set_combo_text(COMBO_ALL_LABEL)
            return

        code = str(code).strip().zfill(3)
        if not code or code == "000":
            self._reset_member_panel(clear_combo=False)
            return
        self._member_filter = code
        label = english_broker_label(code)
        total_cached = len(self._session_broker_events)
        n_match = sum(
            1 for p in self._session_broker_events if str(p.get("code", "")).zfill(3) == code
        )
        hint = ""
        if total_cached == 0:
            hint = " No 0F rows in store yet — check live ranks left, or restart after fixing 0B-then-0F REG."
        elif n_match == 0:
            hint = " This member has no rows in the store yet (may not be in top-5 today). Try “(all)”."
        self.lbl_member_focus.setText(
            f"{label} ({code}) — {n_match} line(s) in today’s store ({total_cached} total 0F rows). "
            f"Top-5 feed only.{hint}"
        )
        self.lbl_member_focus.setStyleSheet(f"color: {self.muted}; background: transparent;")
        self.tree_mf.setRowCount(0)
        for p in self._session_broker_events:
            pc = str(p.get("code", "")).zfill(3)
            if pc == code:
                self._insert_mf_row(p)
        self._set_combo_text(f"{code} - {label}")

        def rest_job(c=code, lab=label, n_stream=n_match):
            lp = BOT_LOOP
            if lp is None:
                return
            try:
                fut = asyncio.run_coroutine_threadsafe(ka10002_fetch_member_rows(c), lp)
                rest = fut.result(timeout=15)
            except Exception as e:
                print(f"ka10002 member fetch: {e}")
                rest = []

            def ui():
                for r in rest:
                    self._insert_mf_row(r)
                self.lbl_member_focus.setText(
                    f"{lab} ({c}) — {n_stream} streamed + {len(rest)} REST snapshot row(s). "
                    "Per-broker tick-by-tick is not in Kiwoom 0B/ka10003; this is top-5 org volumes + live 0F."
                )
                self.lbl_member_focus.setStyleSheet(f"color: {self.muted}; background: transparent;")

            QTimer.singleShot(0, ui)

        threading.Thread(target=rest_job, daemon=True).start()

    def _update_rvol_label(self, data: dict):
        base = RVOL_AVG_DAILY
        cv_raw = str(data.get("cum_vol", "")).strip().replace(",", "")
        if base and cv_raw:
            try:
                rvol = float(cv_raw) / float(base)
                self.lbl_rvol.setText(
                    f"RVOL ~{rvol:.2f}x  · session cum {self.format_num(cv_raw)} ·  "
                    f"avg prior day {self.format_num(str(int(round(base))))}"
                )
                self.lbl_rvol.setStyleSheet(f"color: {self.muted}; background: transparent; border: none;")
            except (TypeError, ValueError):
                self.lbl_rvol.setText("RVOL — (could not parse cum vol or baseline)")
                self.lbl_rvol.setStyleSheet(f"color: {self.dim}; background: transparent; border: none;")
        elif base:
            self.lbl_rvol.setText(
                f"RVOL — (waiting for session cum vol FID 13)  ·  "
                f"avg prior day {self.format_num(str(int(round(base))))}"
            )
            self.lbl_rvol.setStyleSheet(f"color: {self.dim}; background: transparent; border: none;")
        else:
            self.lbl_rvol.setText("RVOL — (loading daily baseline…)")
            self.lbl_rvol.setStyleSheet(f"color: {self.dim}; background: transparent; border: none;")

    def _on_rank_double_click(self, row: int, _col: int):
        it = self.tree.item(row, 5)
        if not it:
            return
        code = str(it.text()).strip().zfill(3)
        if not code or code == "000":
            return
        self._apply_member_filter_code(code)

    def _clear_member_focus(self) -> None:
        self._reset_member_panel(clear_combo=True)

    def poll_queue(self):
        tick_batch: list = []
        try:
            while True:
                tick_batch.append(TICK_GUI_QUEUE.get_nowait())
        except queue.Empty:
            pass
        except Exception as e:
            LOG.error("gui: tick dequeue error: %s", e, exc_info=True)

        for data in tick_batch:
            local_ts = datetime.now().strftime("%H:%M:%S")
            self._tape_ring.append((dict(data), local_ts))
        if tick_batch:
            self._refresh_tape_text()

        prog_batch: list = []
        try:
            while True:
                prog_batch.append(PROGRAM_GUI_QUEUE.get_nowait())
        except queue.Empty:
            pass
        except Exception as e:
            LOG.error("gui: program trade dequeue error: %s", e, exc_info=True)
        if prog_batch:
            for item in prog_batch:
                if isinstance(item, dict) and item.get("type") == "program_history_seed":
                    rows = item.get("rows") or []
                    if rows:
                        self._program_ring.clear()
                        for r in rows:
                            rd = {
                                k: v
                                for k, v in r.items()
                                if k in ("exec_time", "net_qty", "delta_net")
                            }
                            self._program_ring.append(("", rd))
                else:
                    ts = datetime.now().strftime("%H:%M:%S")
                    self._program_ring.append((ts, dict(item)))
            self._refresh_program_table()

        short_batch: list = []
        try:
            while True:
                short_batch.append(SHORT_GUI_QUEUE.get_nowait())
        except queue.Empty:
            pass
        except Exception as e:
            LOG.error("gui: short-sell dequeue error: %s", e, exc_info=True)
        for item in short_batch:
            if isinstance(item, dict) and item.get("type") == "short_sell_seed":
                self._populate_short_sell_table(item.get("rows") or [])

        if tick_batch:
            data = tick_batch[-1]
            change_str = str(data["change"])
            sign = "+" if "+" in change_str else "-" if "-" in change_str else ""
            r0 = self.format_num(data["price"])
            d = f"{sign}{self.format_num(data['change'])}"
            rho = str(data["rate"]).strip()
            nu = self.format_num(data["volume"])
            self.lbl_metrics.setText(f"ref {r0}  ·  delta {d}  ·  pct {rho}%  ·  vol {nu}")
            self.lbl_metrics.setStyleSheet(f"color: {self.ref_muted}; background: transparent; border: none;")
            self._update_rvol_label(data)
        else:
            # If no GUI ticks are arriving, show stale status + log periodically so you can tell
            # whether we're receiving 0B prints at all (market hours vs subscription vs callback issues).
            now_utc = datetime.now(timezone.utc)
            last = LAST_0B_UTC
            if last is None:
                self.lbl_metrics.setText("ref — · delta — · pct — · vol —  (no 0B yet)")
                self.lbl_metrics.setStyleSheet(f"color: {self.dim}; background: transparent; border: none;")
            else:
                age = (now_utc - last).total_seconds()
                if age >= STALE_TICK_WARN_SEC:
                    self.lbl_metrics.setText(
                        f"ref {LATEST_TICK.get('price','—')}  ·  delta {LATEST_TICK.get('change','—')}  ·  "
                        f"pct {LATEST_TICK.get('rate','—')}%  ·  vol {LATEST_TICK.get('volume','—')}  "
                        f"(stale {int(age)}s)"
                    )
                    self.lbl_metrics.setStyleSheet(f"color: {self.dim}; background: transparent; border: none;")
                    if self._last_stale_warn_utc is None or (now_utc - self._last_stale_warn_utc).total_seconds() >= 30:
                        self._last_stale_warn_utc = now_utc
                        LOG.warning(
                            "stale: no 0B for %ss (last_0B=%s last_0F=%s gui_tick_q=%s gui_rank_q=%s)",
                            int(age),
                            LAST_0B_UTC.isoformat() if LAST_0B_UTC else None,
                            LAST_0F_UTC.isoformat() if LAST_0F_UTC else None,
                            getattr(TICK_GUI_QUEUE, "qsize", lambda: -1)(),
                            getattr(RANK_GUI_QUEUE, "qsize", lambda: -1)(),
                        )

        try:
            while True:
                data = RANK_GUI_QUEUE.get_nowait()

                if data["type"] == "foreign":
                    skip = bool(data.get("_skip_session"))
                    row = {k: v for k, v in data.items() if k not in ("type", "_skip_session")}
                    if not skip:
                        self._session_broker_events.append(row)
                        append_broker_session_cache_row(row)
                    self._add_foreign_row(row)

                elif data["type"] == "churn":
                    a_n = int(data.get("a_slots", 0) or 0)
                    b_n = int(data.get("b_slots", 0) or 0)
                    if a_n and b_n:
                        fg = self.text
                    elif a_n:
                        fg = self.lane_a
                    elif b_n:
                        fg = self.lane_b
                    else:
                        fg = self.muted
                    self.lbl_churn.setText(f"chg {data.get('label', '')}")
                    self.lbl_churn.setStyleSheet(f"color: {fg}; background: transparent; border: none;")

        except queue.Empty:
            pass
        except Exception as e:
            LOG.error("gui: rank dequeue/update error: %s", e, exc_info=True)
        finally:
            QTimer.singleShot(GUI_POLL_MS, self.poll_queue)


if __name__ == "__main__":
    bot_thread = threading.Thread(target=start_background_loop, daemon=True)
    bot_thread.start()

    qt_app = QApplication(QT_APPLICATION_ARGV)
    win = KiwoomApp()
    win.show()
    sys.exit(qt_app.exec_())
