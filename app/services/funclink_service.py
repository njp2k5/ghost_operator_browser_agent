from __future__ import annotations

import logging

import httpx

_LOG = logging.getLogger("ghost_operator.funclink")

FUNCLINK_BASE = "https://funclinkbackend-production.up.railway.app"


async def create_funclink_session(
    user_id: str,
    task: str,
    target_url: str,
) -> dict[str, str]:
    """
    Call FuncLink /generate-link and return the parsed response dict.

    Returns keys: url, token, step_count, from_memory
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    payload = {
        "user_id": user_id,
        "task": task,
        "target_url": target_url,
        "context": "",
    }
    _LOG.info("[funclink] generating link user=%s target=%s task=%r", user_id, target_url, task)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(f"{FUNCLINK_BASE}/generate-link", json=payload)
        r.raise_for_status()
        data = r.json()
    _LOG.info("[funclink] link ready url=%s token=%s", data.get("url"), data.get("token"))
    return data
