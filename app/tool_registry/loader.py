from __future__ import annotations


def load_tool_modules() -> None:
    """Import tool modules so they self-register via register_tool side effects."""
    __import__("tool_registry.tools.linkedin_leads")
    __import__("tool_registry.tools.amazon_search")
