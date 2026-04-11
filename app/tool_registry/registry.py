from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

ToolRunFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

TOOL_REGISTRY: dict[str, dict[str, Any]] = {}


def register_tool(tool_def: dict[str, Any], run_fn: ToolRunFn) -> None:
    name = tool_def.get("name")
    if not name or not isinstance(name, str):
        raise ValueError("Tool definition must include a non-empty string 'name'")

    if name in TOOL_REGISTRY:
        raise ValueError(f"Tool '{name}' is already registered")

    TOOL_REGISTRY[name] = {
        "definition": tool_def,
        "run": run_fn,
    }


def get_tool(name: str) -> dict[str, Any] | None:
    return TOOL_REGISTRY.get(name)


def list_tools() -> list[dict[str, Any]]:
    return [entry["definition"] for entry in TOOL_REGISTRY.values()]
