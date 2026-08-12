"""
vcp_scanner/kiwoom_client.py

Async-safe Kiwoom REST wrappers.

Design goals
------------
* Never raise on non-zero return_code — scanners need graceful degradation.
* Automatic cont-yn / next-key pagination for large result sets.
* Configurable retry with exponential backoff.
* All network sleeps are async (never block the event loop).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .config import KIWOOM_RATE_LIMIT_SLEEP

log = logging.getLogger(__name__)

# return_codes that indicate a valid (possibly empty) response
GOOD_RETURN_CODES: frozenset[int] = frozenset({0, 3, 20})


async def safe_post(
    bot,
    endpoint: str,
    api_id: str,
    data: dict[str, Any],
    *,
    extra_headers: dict[str, str] | None = None,
    retries: int = 2,
    sleep: float = KIWOOM_RATE_LIMIT_SLEEP,
) -> dict[str, Any]:
    """
    POST to a Kiwoom REST endpoint.

    Returns the raw JSON body dict on success.
    Returns ``{"return_code": -1, "return_msg": "<reason>"}`` on failure so
    callers can check ``return_code`` without try/except.
    """
    for attempt in range(retries + 1):
        try:
            try:
                headers = bot.api.headers(api_id)
            except TypeError:
                headers = bot.api.headers()

            if extra_headers:
                headers.update(extra_headers)

            res = await bot.api.post(endpoint, api_id, headers, data)

            body: dict = res.json() if hasattr(res, "json") else res
            if not isinstance(body, dict):
                body = {"return_code": -1, "return_msg": f"Unexpected type: {type(body)}"}

            await asyncio.sleep(sleep)
            return body

        except Exception as exc:
            wait = sleep * (attempt + 2)
            if attempt < retries:
                log.debug("safe_post %s attempt %d failed (%s); retrying in %.2fs", api_id, attempt, exc, wait)
                await asyncio.sleep(wait)
            else:
                log.warning("safe_post %s %s exhausted retries: %s", api_id, data, exc)
                return {"return_code": -1, "return_msg": str(exc)}

    return {"return_code": -1, "return_msg": "exhausted retries"}


async def paginated_post(
    bot,
    endpoint: str,
    api_id: str,
    data: dict[str, Any],
    *,
    max_pages: int = 20,
    sleep: float = KIWOOM_RATE_LIMIT_SLEEP,
) -> list[dict]:
    """
    Follow Kiwoom's cont-yn / next-key pagination scheme and return all rows
    from every page as a flat list of dicts.

    Stops early when the server returns cont-yn != 'Y' or the page limit is hit.
    """
    all_rows: list[dict] = []
    cont_yn  = ""
    next_key = ""

    for page in range(max_pages):
        extra: dict[str, str] = {}
        if cont_yn == "Y" and next_key:
            extra = {"cont-yn": "Y", "next-key": next_key}

        body = await safe_post(bot, endpoint, api_id, data, extra_headers=extra, sleep=sleep)

        rc = body.get("return_code")
        if rc not in GOOD_RETURN_CODES and rc is not None:
            log.debug("paginated_post %s page=%d rc=%s — stopping", api_id, page, rc)
            break

        # Extract the first list-valued key in the body
        list_key = next((k for k, v in body.items() if isinstance(v, list)), None)
        rows = body.get(list_key, []) if list_key else []
        all_rows.extend(r for r in rows if isinstance(r, dict))

        # Pagination signal lives in response headers, which the kiwoom library
        # may surface as top-level dict keys or as attributes on the body object.
        cont_yn = (
            body.get("cont-yn", "")
            or body.get("cont_yn", "")
            or str(getattr(body, "cont_yn", ""))
        ).strip()
        next_key = (
            body.get("next-key", "")
            or body.get("next_key", "")
            or str(getattr(body, "next_key", ""))
        ).strip()

        if cont_yn != "Y":
            break

    return all_rows
