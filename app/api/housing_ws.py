from __future__ import annotations

import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from tool_registry.executor import execute_tool

router = APIRouter()

DEFAULT_LIMIT = 5
MAX_LIMIT = 25


async def _send(ws: WebSocket, stage: str, message: str, **extra) -> None:
    payload: dict = {"stage": stage, "message": message}
    payload.update(extra)
    await ws.send_json(payload)


async def _recv(ws: WebSocket) -> str:
    data = await ws.receive_json()
    return str(data.get("data", "") or data.get("message", "")).strip()


@router.websocket("/ws/housing")
async def housing_websocket(websocket: WebSocket) -> None:
    client = websocket.client
    client_id = f"{client.host}:{client.port}" if client else "unknown"
    session_start = time.perf_counter()

    await websocket.accept()

    try:
        await _send(websocket, "init", "Connected to MagicBricks listings agent.")

        await _send(websocket, "prompt_city", "Enter city (for example: Bengaluru):")
        city = await _recv(websocket)
        if not city:
            await _send(websocket, "error", "City is required.")
            return

        await _send(
            websocket,
            "prompt_query",
            "Enter query/locality (for example: 2 bhk hsr layout). You can leave empty:",
        )
        query = await _recv(websocket)

        await _send(websocket, "prompt_purpose", "Purpose? rent or buy (default rent):")
        purpose_raw = await _recv(websocket)
        purpose = purpose_raw.lower() if purpose_raw else "rent"
        if purpose not in {"rent", "buy"}:
            purpose = "rent"

        await _send(
            websocket,
            "prompt_limit",
            f"How many listings? (default {DEFAULT_LIMIT}, max {MAX_LIMIT}):",
        )
        limit_raw = await _recv(websocket)
        try:
            limit = max(1, min(MAX_LIMIT, int(limit_raw)))
        except (TypeError, ValueError):
            limit = DEFAULT_LIMIT

        params = {
            "city": city,
            "query": query,
            "purpose": purpose,
            "limit": limit,
        }

        await _send(websocket, "searching", f"Searching MagicBricks listings in {city}...")

        result = await execute_tool("housing_listings", params)

        if result.get("success"):
            await _send(
                websocket,
                "results",
                f"Found {result.get('count', 0)} listing(s).",
                **result,
            )
        else:
            error_message = result.get("error") or "Failed to fetch housing listings."
            await _send(
                websocket,
                "error",
                error_message,
                **result,
            )

        total_s = time.perf_counter() - session_start
        await _send(websocket, "done", f"Session complete in {total_s:.1f}s")

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            await _send(websocket, "error", f"Unexpected error: {exc}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass