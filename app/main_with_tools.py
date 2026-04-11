from __future__ import annotations

from tool_registry.loader import load_tool_modules

# Preload tool modules before app routes call execute_tool.
load_tool_modules()

from main import app

__all__ = ["app"]
