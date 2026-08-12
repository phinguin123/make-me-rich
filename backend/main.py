"""
Kiwoom Dashboard — FastAPI backend
===================================
Architecture
────────────
  ┌────────────────────────────────────┐
  │  Kiwoom wss://api.kiwoom.com:10000 │  (real) or DummySimulator (dev)
  └──────────────┬─────────────────────┘
                 │ KiwoomBridge task
                 ▼
  ┌──────────────────────────────────────────────────┐
  │  FastAPI  /ws  ←──────────── broadcast_to_clients│
  │           /healthz                               │
  └──────────────────────────────────────────────────┘
                 │
                 ▼  WebSocket JSON frames
  ┌──────────────────────────────────────┐
  │  React frontend  (useOrderBook hook) │
  └──────────────────────────────────────┘

Messages sent to the frontend are plain Kiwoom REAL envelopes:
  { "trnm": "REAL", "data": [{ "type": "0D", "item": "044450", "values": {...} }] }

The frontend sends subscription requests:
  { "trnm": "REG", "data": [{ "item": ["044450"], "type": ["0D"] }] }
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Any

import orjson
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic_settings import BaseSettings

# ─────────────────────────────────────────────────────────────────────────────
# Settings (populated from environment / .env)
# ─────────────────────────────────────────────────────────────────────────────

class Settings(BaseSettings):
    app_key: str = "DUMMY"
    app_secret: str = "DUMMY"
    # Use the _AL (ATS/SOR) integrated code so real-time feeds combine KRX + NXT.
    # e.g. "017800_AL" instead of "017800".  If a plain code is supplied the
    # sor_ticker property below appends _AL automatically.
    target_ticker: str = "017800_AL"  # 017800 = 현대엘리베이터
    kiwoom_dummy: bool = True   # set KIWOOM_DUMMY=false to use live connection
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def sor_ticker(self) -> str:
        """Return the _AL (SOR) version of target_ticker regardless of input."""
        base = self.target_ticker.removesuffix("_AL")
        return f"{base}_AL"

    class Config:
        env_file = ".env"
        case_sensitive = False

settings = Settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
LOG = logging.getLogger("kiwoom_backend")

# ─────────────────────────────────────────────────────────────────────────────
# Client registry — thread-safe via the single asyncio event loop
# ─────────────────────────────────────────────────────────────────────────────

_clients: set[WebSocket] = set()


async def broadcast(payload: dict[str, Any]) -> None:
    """Send a message to every connected frontend client."""
    if not _clients:
        return
    raw = orjson.dumps(payload).decode()
    dead: set[WebSocket] = set()
    for ws in list(_clients):
        try:
            await ws.send_text(raw)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


# ─────────────────────────────────────────────────────────────────────────────
# Dummy order-book simulator (used when KIWOOM_DUMMY=true)
# ─────────────────────────────────────────────────────────────────────────────

def _make_dummy_values(base_price: int) -> dict[str, str]:
    """Generate a realistic-looking 10-level order book around base_price."""
    tick = 50  # ₩50 tick size
    values: dict[str, str] = {
        "21": time.strftime("%H%M%S"),
    }
    for i in range(1, 11):
        ask_p = base_price + tick * i
        bid_p = base_price - tick * i
        # Volumes: heavier near the spread, lighter at the edges (log-normal-ish)
        ask_v = max(1, int(random.lognormvariate(7 - i * 0.4, 0.6)))
        bid_v = max(1, int(random.lognormvariate(7 - i * 0.4, 0.6)))
        values[str(40 + i)] = str(ask_p)  # 매도호가 1~10: FIDs 41-50
        values[str(60 + i)] = str(ask_v)  # 매도호가수량:  FIDs 61-70
        values[str(50 + i)] = str(bid_p)  # 매수호가 1~10: FIDs 51-60
        values[str(70 + i)] = str(bid_v)  # 매수호가수량:  FIDs 71-80

    total_ask = sum(int(values[str(60 + i)]) for i in range(1, 11))
    total_bid = sum(int(values[str(70 + i)]) for i in range(1, 11))
    values["121"] = str(total_ask)  # 매도호가총잔량
    values["125"] = str(total_bid)  # 매수호가총잔량
    return values


async def dummy_simulator() -> None:
    """Simulates Kiwoom 0D pushes every 500 ms for UI development."""
    LOG.info("dummy_simulator: starting (KIWOOM_DUMMY=true)")
    ticker = settings.sor_ticker  # always use _AL (SOR combined) code
    base = 78_350  # starting mid-price

    while True:
        await asyncio.sleep(0.5)
        # Gentle random walk
        base += random.randint(-50, 50)
        base = max(50_000, min(base, 120_000))

        envelope = {
            "trnm": "REAL",
            "data": [
                {
                    "type": "0D",
                    "name": "주식호가잔량",
                    "item": ticker,
                    "values": _make_dummy_values(base),
                }
            ],
        }
        await broadcast(envelope)


# ─────────────────────────────────────────────────────────────────────────────
# Real Kiwoom bridge (enabled when KIWOOM_DUMMY=false)
# ─────────────────────────────────────────────────────────────────────────────

async def kiwoom_bridge() -> None:
    """
    Connects to Kiwoom Open API and forwards 0D (호가잔량), 0B (체결),
    0F (거래원), and 0w (프로그램매매) messages to frontend clients.

    ─── How to enable ───────────────────────────────────────────────────────
    1. Set KIWOOM_DUMMY=false in your .env
    2. Uncomment `# kiwoom>=<version>` in requirements.txt and rebuild
    3. The Bot / REAL import below will resolve automatically
    ──────────────────────────────────────────────────────────────────────────
    """
    try:
        from kiwoom import Bot, REAL  # type: ignore[import]
    except ImportError:
        LOG.error(
            "kiwoom_bridge: 'kiwoom' package not found. "
            "Add it to requirements.txt or set KIWOOM_DUMMY=true."
        )
        return

    sor_ticker = settings.sor_ticker  # e.g. "017800_AL" — SOR combined KRX + NXT

    async def on_real(msg: Any) -> None:
        try:
            raw_values = orjson.loads(getattr(msg, "values", "{}"))
            r_type = str(getattr(msg, "type", "")).upper()
            item = getattr(msg, "item", sor_ticker)
            name = getattr(msg, "name", "")
            envelope = {
                "trnm": "REAL",
                "data": [{"type": r_type, "name": name, "item": item, "values": raw_values}],
            }
            await broadcast(envelope)
        except Exception as exc:
            LOG.error("kiwoom_bridge: on_real error: %s", exc)

    async with Bot(host=REAL, appkey=settings.app_key, secretkey=settings.app_secret) as bot:
        for tr in ("0B", "0D", "0F", "0w"):
            bot.api.add_callback_on_real_data(real_type=tr, callback=on_real)

        await bot.connect()
        LOG.info("kiwoom_bridge: connected")
        await bot.run()

        # Subscribe with the _AL (SOR) code to receive combined KRX + NXT ticks.
        # A plain ticker code (e.g. "017800") only delivers KRX data and will
        # not match MTS which shows SOR-aggregated prices and execution strength.
        await bot.api.socket.send(
            {
                "trnm": "REG",
                "grp_no": "1",
                "refresh": "1",
                "data": [{"item": [sor_ticker], "type": ["0B", "0D", "0F"]}],
            }
        )
        LOG.info("kiwoom_bridge: subscribed to 0B, 0D, 0F for %s (SOR)", sor_ticker)

        while True:
            await asyncio.sleep(1)


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI application
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Start the Kiwoom bridge (or dummy) as a background task."""
    task_fn = dummy_simulator if settings.kiwoom_dummy else kiwoom_bridge
    LOG.info(
        "lifespan: starting backend (ticker=%s, dummy=%s)",
        settings.target_ticker,
        settings.kiwoom_dummy,
    )
    task = asyncio.create_task(task_fn())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Kiwoom Dashboard API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # Restrict in production — here we allow all origins for local dev comfort.
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health check (used by docker-compose healthcheck) ────────────────────────
@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "ticker": settings.sor_ticker}


# ── Main WebSocket endpoint ───────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    _clients.add(websocket)
    LOG.info("ws: client connected (total=%d)", len(_clients))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = orjson.loads(raw)
            except Exception:
                continue

            trnm = str(msg.get("trnm", "")).upper()

            if trnm == "REG":
                # Frontend is requesting a real-time subscription.
                # In dummy mode we ignore it (simulator broadcasts to everyone).
                # In live mode you'd forward it to the Kiwoom bridge here.
                LOG.debug("ws: REG from client: %s", msg)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        LOG.warning("ws: unexpected error: %s", exc)
    finally:
        _clients.discard(websocket)
        LOG.info("ws: client disconnected (total=%d)", len(_clients))
