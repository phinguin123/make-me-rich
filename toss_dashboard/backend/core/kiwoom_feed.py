"""Kiwoom US equity realtime feed (TR FE) on /api/us/websocket."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import websockets

BACKEND_ROOT = Path(__file__).resolve().parents[3] / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

LOG = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

US_WS_REAL = "wss://api.kiwoom.com:10000/api/us/websocket"
US_WS_MOCK = "wss://mockapi.kiwoom.com:10000/api/us/websocket"

TRADE_COLUMNS = ["timestamp", "price", "size"]
QUOTE_COLUMNS = ["timestamp", "bid_price", "bid_size", "ask_price", "ask_size"]


def _parse_num(raw: Any) -> float:
    s = str(raw or "").replace(",", "").replace(" ", "").replace("+", "")
    if not s or s == "-":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_signed_qty(raw: Any) -> float:
    s = str(raw or "").replace(",", "").replace(" ", "")
    if not s:
        return 0.0
    try:
        return abs(float(s))
    except ValueError:
        return 0.0


def _tick_timestamp(values: dict) -> pd.Timestamp:
    date_raw = "".join(ch for ch in str(values.get("22") or "") if ch.isdigit())
    time_raw = "".join(ch for ch in str(values.get("51020") or values.get("20") or "") if ch.isdigit()).zfill(6)[:6]
    now = datetime.now(KST)
    try:
        hh, mm, ss = int(time_raw[:2]), int(time_raw[2:4]), int(time_raw[4:6])
        if len(date_raw) == 8:
            ts = datetime(
                int(date_raw[:4]),
                int(date_raw[4:6]),
                int(date_raw[6:8]),
                hh,
                mm,
                ss,
                tzinfo=KST,
            )
            return pd.Timestamp(ts)
        return pd.Timestamp(now.replace(hour=hh, minute=mm, second=ss, microsecond=0))
    except ValueError:
        return pd.Timestamp(now)


class KiwoomWsFeed:
    """US Kiwoom websocket: LOGIN + REG type FE (미국주식 실시간 체결가)."""

    def __init__(
        self,
        ticker: str,
        appkey: str,
        secretkey: str,
        stex_tp: str = "ND",
        mock: bool = False,
    ) -> None:
        self.ticker = str(ticker or "SOXL").upper()
        self.appkey = appkey
        self.secretkey = secretkey
        self.stex_tp = [p.strip().upper() for p in str(stex_tp or "ND").split(",") if p.strip()] or ["ND"]
        self.uri = US_WS_MOCK if mock else US_WS_REAL
        self._ticks: deque[dict] = deque(maxlen=20_000)
        self._quote: Optional[dict] = None
        self._last_cum: Optional[float] = None
        self._fe_count = 0
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._async_stop: Optional[asyncio.Event] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._thread_main, name="kiwoom-us-fe", daemon=True)
        self._thread.start()
        LOG.info("Kiwoom US FE feed starting for %s/%s", self.ticker, ",".join(self.stex_tp))

    def stop(self) -> None:
        loop = self._loop
        stop = self._async_stop
        if loop is not None and stop is not None and loop.is_running():
            loop.call_soon_threadsafe(stop.set)

    def drain_ticks(self, count: int = 50) -> pd.DataFrame:
        rows: list[dict] = []
        with self._lock:
            while self._ticks and len(rows) < count:
                rows.append(self._ticks.popleft())
        if not rows:
            return pd.DataFrame(columns=TRADE_COLUMNS)
        return pd.DataFrame(rows)

    def latest_l1(self) -> pd.DataFrame:
        with self._lock:
            if self._quote is None:
                return pd.DataFrame(columns=QUOTE_COLUMNS)
            return pd.DataFrame([self._quote])

    def _ingest_fe(self, item: dict) -> None:
        values = item.get("values") or {}
        price = _parse_num(values.get("10"))
        last_size = _parse_signed_qty(values.get("15"))
        cum = _parse_num(values.get("13"))
        ts = _tick_timestamp(values)

        size = 0.0
        if self._last_cum is None:
            size = last_size
            if cum > 0:
                self._last_cum = cum
        else:
            if cum > self._last_cum:
                size = cum - self._last_cum
                self._last_cum = cum
            else:
                size = last_size

        if price > 0 and size > 0:
            with self._lock:
                self._ticks.append({"timestamp": ts, "price": price, "size": size})

        ask = _parse_num(values.get("27"))
        bid = _parse_num(values.get("28"))
        if bid > 0 and ask > 0:
            with self._lock:
                self._quote = {
                    "timestamp": ts,
                    "bid_price": bid,
                    "bid_size": 1.0,
                    "ask_price": ask,
                    "ask_size": 1.0,
                }

        self._fe_count += 1
        if self._fe_count <= 5 or self._fe_count % 50 == 0:
            LOG.info(
                "FE #%s %s px=%s size=%s cum=%s bid=%s ask=%s buffered=%s",
                self._fe_count,
                item.get("item"),
                price,
                size,
                cum,
                bid,
                ask,
                len(self._ticks),
            )

    async def _access_token(self) -> str:
        from kiwoom.config import MOCK, REAL
        from kiwoom.http.client import Client

        host = MOCK if self.uri == US_WS_MOCK else REAL
        http = Client(host, self.appkey, self.secretkey)
        await http.connect(self.appkey, self.secretkey)
        token = http.token()
        await http.close()
        if not token:
            raise RuntimeError("Kiwoom OAuth token is empty")
        return token

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception:
            LOG.exception("Kiwoom US FE feed crashed")

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._async_stop = asyncio.Event()
        token = await self._access_token()

        async with websockets.connect(self.uri) as websocket:
            await websocket.send(json.dumps({"trnm": "LOGIN", "token": token}))
            LOG.info("Kiwoom US FE LOGIN sent")

            recv_task = asyncio.create_task(self._receive(websocket))
            await asyncio.sleep(1)
            items = [{"jmcode": self.ticker, "stex_tp": tp} for tp in self.stex_tp]
            await websocket.send(
                json.dumps(
                    {
                        "trnm": "REG",
                        "grp_no": "1",
                        "refresh": "1",
                        "data": [{"item": items, "type": ["FE"]}],
                    }
                )
            )
            LOG.info("Kiwoom US FE REG %s %s", self.ticker, items)

            stop_task = asyncio.create_task(self._async_stop.wait())
            done, pending = await asyncio.wait(
                {recv_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                exc = task.exception() if not task.cancelled() else None
                if exc:
                    LOG.error("Kiwoom US FE task failed: %s", exc, exc_info=exc)
            for task in pending:
                task.cancel()

    async def _receive(self, websocket) -> None:
        while True:
            raw = await websocket.recv()
            try:
                response = json.loads(raw)
            except json.JSONDecodeError:
                continue

            trnm = response.get("trnm")
            if trnm == "LOGIN":
                if response.get("return_code") != 0:
                    raise RuntimeError(f"Kiwoom US WS login failed: {response.get('return_msg')}")
                LOG.info("Kiwoom US FE login ok")
                continue

            if trnm == "PING":
                await websocket.send(json.dumps(response) if not isinstance(response, str) else response)
                continue

            if trnm != "REAL":
                LOG.info("Kiwoom US WS %s: %s", trnm, response)
                continue

            for item in response.get("data") or []:
                if str(item.get("type") or "").upper() != "FE":
                    continue
                try:
                    self._ingest_fe(item)
                except Exception as exc:
                    LOG.error("FE ingest error: %s", exc)
