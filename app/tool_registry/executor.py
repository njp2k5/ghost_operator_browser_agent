from __future__ import annotations

from typing import Any

from tool_registry.registry import get_tool
from tool_registry import tools as _tools  # noqa: F401


def _format_error(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return text
    return f"{type(exc).__name__}: unspecified error"


async def execute_tool(name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    tool = get_tool(name)
    if tool is None:
        return {
            "success": False,
            "tool": name,
            "error": f"Tool '{name}' is not registered",
        }

    safe_params = params or {}

    try:
        result = await tool["run"](safe_params)
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "tool": name,
            "params": safe_params,
            "error": _format_error(exc),
        }

    if not isinstance(result, dict):
        return {
            "success": False,
            "tool": name,
            "params": safe_params,
            "error": "Tool returned a non-dict response",
        }

    return {
        "success": result.get("success", True),
        "tool": name,
        **result,
    }
