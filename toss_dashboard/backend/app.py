"""Dashboard + engine host. One process = one Toss token (a second process would revoke it).

    python app.py                      # http://localhost:5050
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from trader.config import DATA_DIR, SETTINGS
from trader.engine import Engine

DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"
TOKEN = os.getenv("TRADER_DASHBOARD_TOKEN", "").strip()


def token_ok(supplied: str | None) -> bool:
    return not TOKEN or (supplied is not None and secrets.compare_digest(supplied, TOKEN))


async def require_token(request: Request) -> None:
    if not token_ok(request.headers.get("x-token") or request.query_params.get("token")):
        raise HTTPException(status_code=401, detail="bad token")


def setup_logging() -> None:
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), RotatingFileHandler(DATA_DIR / "logs" / "trader.log", maxBytes=10_000_000, backupCount=5)):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class Host:
    engine: Engine | None = None
    task: asyncio.Task | None = None

    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> None:
        if not self.running():
            self.engine = Engine(SETTINGS)
            self.task = asyncio.create_task(self.engine.run())

    async def stop(self) -> None:
        if self.engine and self.running():
            await self.engine.stop()
            await asyncio.wait_for(self.task, timeout=30)


host = Host()


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    if os.getenv("TRADER_AUTOSTART", "true").lower() == "true":
        host.start()
    yield
    await host.stop()


app = FastAPI(title="Toss Auto Trader", lifespan=lifespan)
api = Depends(require_token)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def status() -> dict:
    if host.engine is None:
        return {"running": False, "mode": SETTINGS.mode}
    snap = host.engine.snapshot()
    snap["running"] = host.running()
    return snap


@app.get("/healthz")
async def healthz():
    return {"ok": True, "running": host.running()}


@app.get("/api/status", dependencies=[api])
async def api_status():
    return status()


@app.post("/api/start", dependencies=[api])
async def api_start():
    host.start()
    return {"running": True}


@app.post("/api/stop", dependencies=[api])
async def api_stop():
    await host.stop()
    return {"running": False}


@app.post("/api/pause", dependencies=[api])
async def api_pause():
    if host.engine:
        host.engine.paused = True
    return {"paused": True}


@app.post("/api/resume", dependencies=[api])
async def api_resume():
    if host.engine:
        host.engine.paused = False
    return {"paused": False}


@app.post("/api/flatten", dependencies=[api])
async def api_flatten():
    if host.engine and host.running():
        await host.engine.flatten()
    return {"flattened": True}


@app.websocket("/ws")
async def ws(socket: WebSocket):
    if not token_ok(socket.query_params.get("token")):
        await socket.close(code=4401)
        return
    await socket.accept()
    try:
        while True:
            await socket.send_json(status())
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError):
        pass


if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        return FileResponse(DIST / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("TRADER_HOST", "0.0.0.0"), port=SETTINGS.port, log_level="warning")
