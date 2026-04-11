from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from core.websocket_manager import manager
from services.llm_service import llm_service
from services.memory_service import memory_service
from tool_registry.executor import execute_tool
from tool_registry.registry import list_tools

# Inline sub-handlers — pure async fetch functions imported directly
from api.linkedin_ws import _fetch_olx_results
from api.hindu_ws import _fetch_hindu_news
from api.irctc_ws import _fetch_irctc_results, _fallback_irctc_results, _detect_intent

router = APIRouter()
AMAZON_ACCOUNT_ACTIVE_SESSIONS: set[str] = set()

CHAT_SYSTEM_PROMPT = "WhatsApp AI assistant"

# ---------------------------------------------------------------------------
# Inline WS tools — handled in this module, NOT via the tool registry executor
# ---------------------------------------------------------------------------
_INLINE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "olx_search",
        "description": "Search OLX India listings for second-hand goods, vehicles, real estate, and jobs.",
        "input_schema": {
            "properties": {
                "query": {"type": "string", "description": "Product or listing search query"},
                "limit": {"type": "integer", "description": "Number of results (1-20)", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "irctc_search",
        "description": "Search IRCTC for trains, fares, PNR status, and ticket booking.",
        "input_schema": {
            "properties": {
                "query": {"type": "string", "description": "Full query including from/to stations and date if applicable"},
                "limit": {"type": "integer", "description": "Number of results (1-10)", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "hindu_news",
        "description": "Fetch latest news headlines from The Hindu newspaper by section.",
        "input_schema": {
            "properties": {
                "section": {
                    "type": "string",
                    "description": (
                        "Section name: national, international, business, sport, technology, "
                        "science, entertainment, education, opinion, cities, environment, health, "
                        "law, agriculture, life-and-style, real-estate, immigration, premium, front-page"
                    ),
                    "default": "national",
                },
                "limit": {"type": "integer", "description": "Number of articles (1-15)", "default": 5},
            },
        },
    },
]

_INLINE_TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in _INLINE_TOOLS)

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
- Use `amazon_account` when the user wants to login to Amazon, provide OTP,
    check order history, or ask order status.
- For `amazon_account`, include params.session_id as sender id and params.user_input
    as the latest user message.
- For `amazon_account`, prefer params.headless=false unless user explicitly asks for headless.
- Use `amazon_search` for Amazon product discovery, price checks, and product comparisons.
  Set params.query to the product query. You may set params.limit (1-20) and params.marketplace.
- Use `olx_search` for OLX India listings, second-hand goods, used products, used cars/bikes, buy/sell.
  Set params.query to the search term and optionally params.limit.
- Use `irctc_search` for train travel, IRCTC ticket booking, PNR status, train schedules, fares.
  Set params.query to the full user query (preserve station names and dates).
- Use `hindu_news` for news queries, current events, headlines, sports, business, technology news.
  Set params.section to the best matching section from the available list. Optionally set params.limit.
- If no tool is needed (general chat, greetings, opinions, calculations), return use_tool=false and tool="".
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
    if tool_name == "amazon_account":
        reply = str(tool_result.get("assistant_reply") or "").strip()
        if reply:
            orders = tool_result.get("orders")
            if isinstance(orders, list) and orders:
                lines = [reply, "", "Recent orders:"]
                for idx, item in enumerate(orders, start=1):
                    if not isinstance(item, dict):
                        continue
                    order_id = str(item.get("order_id") or "").strip()
                    status = str(item.get("status") or "Status unavailable").strip()
                    title = str(item.get("title") or "").strip()
                    detail_url = str(item.get("detail_url") or "").strip()

                    descriptor = f"{idx}. {status}"
                    if order_id:
                        descriptor = f"{idx}. {order_id} - {status}"
                    if title:
                        descriptor += f" - {title}"
                    if detail_url.startswith("http"):
                        descriptor += f" - {detail_url}"

                    lines.append(descriptor)
                return "\n".join(lines)
            return reply

        if not tool_result.get("success"):
            return str(tool_result.get("error") or "Amazon account action failed.")
        return "Amazon account action completed."

    if tool_name == "amazon_search":
        return _format_amazon_tool_reply(tool_result)
    return ""


def _looks_like_amazon_account_intent(message: str) -> bool:
    text = message.lower()
    keywords = [
        "start amazon",
        "amazon login",
        "login amazon",
        "my orders",
        "order status",
        "order history",
        "amazon account",
        "amazon orders",
        "otp",
        "amazon password",
    ]
    return any(keyword in text for keyword in keywords)


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
    # Merge registry tools (amazon_search etc.) with inline WS tools
    tool_defs = list_tools()
    compact_registry = [
        {
            "name": item.get("name"),
            "description": item.get("description"),
            "input_schema": item.get("input_schema", {}),
        }
        for item in tool_defs
    ]
    all_tools = compact_registry + _INLINE_TOOLS

    router_prompt = TOOL_ROUTER_PROMPT_TEMPLATE.format(
        tools_json=json.dumps(all_tools, ensure_ascii=False)
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

<<<<<<< HEAD
    # Fallback: ensure query params are populated from the raw user message when missing
=======
    if tool == "amazon_account" and not str(params.get("user_input", "")).strip():
        params["user_input"] = message

    # Fallback: if router picks amazon_search without query, use full user message.
>>>>>>> 33c1271 (added amazon login and order details)
    if tool == "amazon_search" and not str(params.get("query", "")).strip():
        params["query"] = message
    if tool == "olx_search" and not str(params.get("query", "")).strip():
        params["query"] = message
    if tool == "irctc_search" and not str(params.get("query", "")).strip():
        params["query"] = message

    return {
        "use_tool": use_tool,
        "tool": tool,
        "params": params,
        "reason": str(parsed.get("reason", "")).strip(),
    }


# ---------------------------------------------------------------------------
# Inline tool result formatters
# ---------------------------------------------------------------------------

def _format_olx_reply(results: list[dict[str, Any]], query: str) -> str:
    if not results:
        return f"No OLX listings found for '{query}'. Try a different search term."
    lines: list[str] = []
    for i, item in enumerate(results, 1):
        title = str(item.get("title") or "Listing").strip()
        price = str(item.get("price") or "").strip()
        location = str(item.get("location") or "").strip()
        url = str(item.get("url") or "").strip()
        meta_parts = [v for v in [price, location] if v]
        meta = " | ".join(meta_parts) if meta_parts else "details not available"
        line = f"{i}. *{title}* — {meta}"
        if url:
            line += f"\n   {url}"
        lines.append(line)
    return f"Found {len(lines)} OLX listing(s) for '{query}':\n\n" + "\n\n".join(lines)


def _format_irctc_reply(results: list[dict[str, Any]], query: str) -> str:
    if not results:
        return f"No IRCTC results found for '{query}'. Try https://www.irctc.co.in directly."
    lines: list[str] = []
    for i, item in enumerate(results, 1):
        title = str(item.get("title") or "Result").strip()
        snippet = str(item.get("snippet") or "").strip()
        url = str(item.get("url") or "").strip()
        line = f"{i}. *{title}*"
        if snippet:
            line += f"\n   {snippet[:150]}{'...' if len(snippet) > 150 else ''}"
        if url:
            line += f"\n   {url}"
        lines.append(line)
    return f"IRCTC results for '{query}':\n\n" + "\n\n".join(lines)


def _format_hindu_reply(results: list[dict[str, Any]], section: str) -> str:
    if not results:
        return f"No Hindu news articles found for section '{section}'."
    lines: list[str] = []
    for i, item in enumerate(results, 1):
        title = str(item.get("title") or "Article").strip()
        url = str(item.get("url") or "").strip()
        published = str(item.get("published") or "").strip()
        description = str(item.get("description") or "").strip()
        line = f"{i}. *{title}*"
        if published:
            line += f" ({published})"
        if description:
            line += f"\n   {description[:120]}{'...' if len(description) > 120 else ''}"
        if url:
            line += f"\n   {url}"
        lines.append(line)
    return f"Latest The Hindu news ({section}):\n\n" + "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Inline tool dispatcher — runs sub-handler on the same websocket connection
# ---------------------------------------------------------------------------

async def _dispatch_inline_tool(
    sender: str,
    tool_name: str,
    params: dict[str, Any],
    original_message: str,
) -> str:
    """Execute an inline WS tool and return a formatted reply string."""

    if tool_name == "olx_search":
        query = str(params.get("query") or original_message).strip()
        limit = max(1, min(int(params.get("limit") or 5), 20))
        await manager.send(sender, f"Searching OLX for '{query}'...")
        try:
            results = await _fetch_olx_results(query, limit)
        except Exception as exc:  # noqa: BLE001
            return f"OLX search failed: {exc}"
        return _format_olx_reply(results, query)

    if tool_name == "irctc_search":
        query = str(params.get("query") or original_message).strip()
        limit = max(1, min(int(params.get("limit") or 5), 10))
        await manager.send(sender, f"Searching IRCTC for '{query}'...")
        try:
            results = await _fetch_irctc_results(query, limit)
        except Exception:  # noqa: BLE001
            results = []
        if not results:
            intent = _detect_intent(query)
            results = _fallback_irctc_results(query, intent, limit)
        return _format_irctc_reply(results, query)

    if tool_name == "hindu_news":
        section = str(params.get("section") or "national").strip().lower()
        limit = max(1, min(int(params.get("limit") or 5), 15))
        await manager.send(sender, f"Fetching The Hindu {section} news...")
        try:
            results = await _fetch_hindu_news(section, limit)
        except Exception as exc:  # noqa: BLE001
            return f"The Hindu news fetch failed: {exc}"
        return _format_hindu_reply(results, section)

    return ""


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

            tool_name = ""
            tool_result: dict[str, Any] | None = None

            if sender in AMAZON_ACCOUNT_ACTIVE_SESSIONS:
                tool_name = "amazon_account"
                tool_result = await execute_tool(
                    tool_name,
                    {
                        "session_id": sender,
                        "user_input": message,
                    },
                )
            else:
                decision = _safe_router_decision(message)

                if not decision.get("use_tool") and _looks_like_amazon_account_intent(message):
                    decision = {
                        "use_tool": True,
                        "tool": "amazon_account",
                        "params": {
                            "session_id": sender,
                            "user_input": message,
                        },
                        "reason": "explicit amazon account intent",
                    }

                if decision.get("use_tool") and decision.get("tool"):
                    tool_name = str(decision["tool"])
                    tool_params = decision.get("params", {})
                    if not isinstance(tool_params, dict):
                        tool_params = {}

                    if tool_name == "amazon_account":
                        tool_params.setdefault("session_id", sender)
                        tool_params.setdefault("user_input", message)

                    tool_result = await execute_tool(tool_name, tool_params)

            if tool_name and tool_result is not None:
                if tool_name == "amazon_account":
                    if bool(tool_result.get("session_active", False)):
                        AMAZON_ACCOUNT_ACTIVE_SESSIONS.add(sender)
                    else:
                        AMAZON_ACCOUNT_ACTIVE_SESSIONS.discard(sender)

<<<<<<< HEAD
            if decision.get("use_tool") and decision.get("tool"):
                tool_name = str(decision["tool"])
                tool_params = decision.get("params", {})
=======
                reply = _build_tool_reply(tool_name, tool_result)
>>>>>>> 33c1271 (added amazon login and order details)

                if tool_name in _INLINE_TOOL_NAMES:
                    # Inline WS tools — run sub-handler directly on this connection
                    reply = await _dispatch_inline_tool(sender, tool_name, tool_params, message)
                    if not reply:
                        reply = "I could not retrieve results for your query. Please try again."
                else:
                    # Registry tools (e.g. amazon_search) — go through tool executor
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