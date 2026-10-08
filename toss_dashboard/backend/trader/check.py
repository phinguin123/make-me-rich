"""Read-only preflight: verifies credentials, IP allowlist, account and data feeds.

    python -m trader.check
"""
from __future__ import annotations

import asyncio
import json

import websockets

from .alpaca import AlpacaData
from .config import SETTINGS
from .toss.client import TossAPIError, TossClient
from .toss.stream import WS_URL


def ok(msg: str) -> None:
    print(f"  ✔ {msg}")


def bad(msg: str) -> None:
    print(f"  ✘ {msg}")


async def main() -> None:
    print(f"mode={SETTINGS.mode} capital=${SETTINGS.capital:,.0f} account_seq={SETTINGS.toss_account_seq}")
    async with TossClient() as c:
        print("Toss REST")
        try:
            await c.token()
            ok("token")
        except TossAPIError as exc:
            bad(f"token: {exc}  (403 access_denied = this machine's IP is not in the Toss allowlist)")
            return
        try:
            accts = await c.request("GET", "/api/v1/accounts", "ACCOUNT")
            ok(f"accounts: {[(a['accountSeq'], a['accountType']) for a in accts]}")
            ok(f"USD buying power: ${await c.buying_power('USD'):,.2f}")
            h = await c.holdings()
            ok(f"holdings: {', '.join(f'{i['symbol']} x{i['quantity']}' for i in h['items']) or 'none'}")
            for com in await c.commissions():
                ok(f"commission {com['marketCountry']}: {float(com['commissionRate']):.3%} (until {com['endDate']})")
            cal = await c.us_calendar()
            ok(f"US calendar today: {json.dumps(cal['today']['regularMarket'])}")
        except TossAPIError as exc:
            bad(str(exc))
        print("Toss WebSocket")
        try:
            async with websockets.connect(WS_URL, additional_headers={"Authorization": f"Bearer {await c.token()}"}) as ws:
                await ws.send(json.dumps([{"type": "trade:us", "codes": ["SPY"]}, {"type": "personal:order", "codes": [SETTINGS.toss_account_seq]}]))
                ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
                ok(f"subscribed {ack.get('subscribed')} rejected {ack.get('rejected')}")
                await ws.send("PING")
                ok("keepalive answered") if "pong" in await asyncio.wait_for(_skip_data(ws), 10) else bad("no pong")
        except Exception as exc:  # noqa: BLE001
            bad(f"websocket: {exc}")
    print("Alpaca data")
    try:
        async with AlpacaData() as api:
            snap = await api.snapshots(["SPY"])
            ok(f"IEX realtime SPY {snap['SPY']['latestTrade']['p']}")
    except Exception as exc:  # noqa: BLE001
        bad(f"alpaca: {exc}")


async def _skip_data(ws) -> str:
    while True:
        raw = await ws.recv()
        if "pong" in raw:
            return raw


if __name__ == "__main__":
    asyncio.run(main())
