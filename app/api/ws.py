from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from core.websocket_manager import manager
from services.llm_service import llm_service
from services.memory_service import memory_service
from tool_registry.executor import execute_tool
from tool_registry.registry import list_tools

router = APIRouter()

CHAT_SYSTEM_PROMPT = "WhatsApp AI assistant"

TOOL_ROUTER_PROMPT_TEMPLATE = """
You are a tool router for a WhatsApp assistant.
Decide whether a tool should be used for the user message.

Return STRICT JSON only in this exact shape:
{{
    "use_tool": true or false,
    "tool": "tool_name_or_empty",
    "params": {{"key": "value"}},
    "reason": "short reason"
}}

Available tools:
{tools_json}

Rules:
- Use `amazon_search` for Amazon product discovery, price checks, and product comparisons.
- For `amazon_search`, set params.query to the main product query text.
- You may set params.limit (1-20) and params.marketplace (for example: www.amazon.in).
- If no tool is needed, return use_tool=false and tool="".
"""

TOOL_RESPONSE_PROMPT = """
You are a WhatsApp AI assistant.
You already have the tool result JSON.
Reply conversationally and clearly.

Rules:
- If tool call succeeded, show concise top results with title, price, rating, and URL.
- If tool call failed, explain the issue briefly and suggest what user can try next.
- Keep the response compact and WhatsApp-friendly.
"""


def _format_amazon_tool_reply(tool_result: dict[str, Any]) -> str:
    if not tool_result.get("success"):
        error = str(tool_result.get("error") or "Amazon search failed")
        return f"I could not fetch Amazon products right now: {error}"

    raw_results = tool_result.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        return "I could not find matching Amazon products for that query."

    query = str(tool_result.get("query") or "your query").strip()

    lines: list[str] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue

        title = str(item.get("title") or "Product").strip()
        url = str(item.get("url") or "").strip()
        price = str(item.get("price") or "").strip()
        rating = str(item.get("rating") or "").strip()

        if not url.startswith("http"):
            continue
        if "amazon." not in url.lower():
            continue
        if url in seen_urls:
            continue

        seen_urls.add(url)
        meta_parts = [value for value in [price, rating] if value]
        meta_text = ", ".join(meta_parts) if meta_parts else "price/rating not available"

        lines.append(f"{len(lines) + 1}. **{title}** - {meta_text} - {url}")

    if not lines:
        return "I found products but could not build valid Amazon product links for this query."

    return (
        f"Here are the top {len(lines)} products I found on Amazon India for '{query}':\n\n"
        + "\n".join(lines)
    )


def _build_tool_reply(tool_name: str, tool_result: dict[str, Any]) -> str:
    if tool_name == "amazon_search":
        return _format_amazon_tool_reply(tool_result)
    return ""


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None

    # Handle fenced outputs like ```json ... ```
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or start >= end:
        return None

    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None

    return parsed if isinstance(parsed, dict) else None


def _parse_incoming_message(raw_text: str) -> str:
    text = raw_text.strip()
    if not text:
        return ""

    # Support both JSON payloads (from node-app) and plain text (from Postman UI).
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text

    if isinstance(parsed, dict):
        return str(parsed.get("message") or parsed.get("data") or "").strip()

    if isinstance(parsed, str):
        return parsed.strip()

    return text


def _safe_router_decision(message: str) -> dict[str, Any]:
    tool_defs = list_tools()
    compact_defs = [
        {
            "name": item.get("name"),
            "description": item.get("description"),
            "input_schema": item.get("input_schema", {}),
        }
        for item in tool_defs
    ]

    router_prompt = TOOL_ROUTER_PROMPT_TEMPLATE.format(
        tools_json=json.dumps(compact_defs, ensure_ascii=False)
    )
    raw = llm_service.generate(
        [
            {"role": "system", "content": router_prompt},
            {"role": "user", "content": message},
        ],
        temperature=0.0,
        max_tokens=220,
    )

    parsed = _extract_json_object(raw)
    if parsed is None:
        return {"use_tool": False, "tool": "", "params": {}, "reason": "invalid router output"}

    use_tool = bool(parsed.get("use_tool", False))
    tool = str(parsed.get("tool", "") or "").strip()
    params = parsed.get("params", {})
    if not isinstance(params, dict):
        params = {}

    # Fallback: if router picks amazon_search without query, use full user message.
    if tool == "amazon_search" and not str(params.get("query", "")).strip():
        params["query"] = message

    return {
        "use_tool": use_tool,
        "tool": tool,
        "params": params,
        "reason": str(parsed.get("reason", "")).strip(),
    }

@router.websocket("/ws/{sender}")
async def websocket_endpoint(websocket: WebSocket, sender: str):
    await manager.connect(sender, websocket)

    try:
        while True:
            raw_text = await websocket.receive_text()
            message = _parse_incoming_message(raw_text)
            if not message:
                await manager.send(
                    sender,
                    "Please send text or JSON like {\"message\": \"your query\"}.",
                )
                continue

            # store user msg
            memory_service.append(sender, "user", message)

            history = memory_service.get_history(sender)

            decision = _safe_router_decision(message)

            if decision.get("use_tool") and decision.get("tool"):
                tool_name = str(decision["tool"])
                tool_params = decision.get("params", {})
                tool_result = await execute_tool(tool_name, tool_params)
                reply = _build_tool_reply(tool_name, tool_result)

                if not reply:
                    messages = [
                        {"role": "system", "content": TOOL_RESPONSE_PROMPT},
                        *history,
                        {
                            "role": "system",
                            "content": "Tool result JSON:\n"
                            + json.dumps(tool_result, ensure_ascii=False),
                        },
                    ]

                    reply = llm_service.generate(messages, temperature=0.2, max_tokens=450)
            else:
                # prepend system
                messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}] + history

                # LLM call
                reply = llm_service.generate(messages)

            # store reply
            memory_service.append(sender, "assistant", reply)

            # send response
            await manager.send(sender, reply)

    except WebSocketDisconnect:
        manager.disconnect(sender)