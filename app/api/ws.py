from __future__ import annotations

import json
import re
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
from services.funclink_service import create_funclink_session

router = APIRouter()
AMAZON_ACCOUNT_ACTIVE_SESSIONS: set[str] = set()


def _parse_order_limit(text: str, default: int = 5, cap: int = 10) -> int:
    """Extract an explicit order count from the user's message."""
    m = re.search(r'(\d+)\s*orders?', text, re.IGNORECASE) or \
        re.search(r'(?:last|show|get|fetch|show\s+me)\s+(\d+)', text, re.IGNORECASE)
    return max(1, min(int(m.group(1)), cap)) if m else default

CHAT_SYSTEM_PROMPT = (
    "You are Franky, a Ghost Operator — an AI assistant powered by browser agents. "
    "You can search OLX, IRCTC trains, The Hindu news, MagicBricks properties, Amazon products, "
    "Amazon account orders, Practo doctors, and guide users through live browser tasks on any website "
    "via FuncLink (guided browser sessions). "
    "Be concise, friendly, and WhatsApp-native in tone. "
    "When a user wants to do something on a website, offer to generate a FuncLink guided session for them."
)

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
    {
        "name": "housing_search",
        "description": "Find property listings on MagicBricks for rent or buy in Indian cities.",
        "input_schema": {
            "properties": {
                "city": {"type": "string", "description": "City name (e.g. Bengaluru, Mumbai, Delhi)"},
                "query": {"type": "string", "description": "Keywords like locality, 2BHK, furnished", "default": ""},
                "purpose": {"type": "string", "description": "rent or buy", "default": "rent"},
                "limit": {"type": "integer", "description": "Number of listings (1-25)", "default": 5},
            },
            "required": ["city"],
        },
    },
    {
        "name": "amazon_account",
        "description": "Login to Amazon and fetch order history or track a specific order.",
        "input_schema": {
            "properties": {
                "user_input": {"type": "string", "description": "User's message for the current login/order step"},
                "command": {"type": "string", "description": "Optional: start, orders, order_status, logout"},
                "limit": {"type": "integer", "description": "Number of orders to return (1-10)", "default": 5},
            },
        },
    },
    {
        "name": "funclink_guide",
        "description": "Open a live guided browser session that walks the user through a task on any website (e.g. booking hotels, filling forms, searching flights).",
        "input_schema": {
            "properties": {
                "task": {"type": "string", "description": "Full natural-language description of what the user wants to do, including destination, dates, guests etc."},
                "target_url": {"type": "string", "description": "Root URL of the website to open (e.g. https://www.booking.com)"},
                "website_name": {"type": "string", "description": "Human-readable website name, e.g. Booking.com", "default": ""},
            },
            "required": ["task", "target_url"],
        },
    },
    {
        "name": "practo_search",
        "description": "Find doctors and specialists on Practo by city, speciality, and locality.",
        "input_schema": {
            "properties": {
                "city": {"type": "string", "description": "City name (e.g. Bengaluru, Mumbai, Delhi)"},
                "speciality": {"type": "string", "description": "Doctor speciality (e.g. dentist, dermatologist, cardiologist)", "default": ""},
                "locality": {"type": "string", "description": "Locality or area (e.g. HSR Layout, Bandra)", "default": ""},
                "limit": {"type": "integer", "description": "Number of doctors (1-20)", "default": 5},
            },
            "required": ["city"],
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
- For `amazon_account`, if the user asks for a specific number of orders (e.g. "last 10 orders", "show me 3 orders"), set params.limit to that number (max 10). Otherwise omit it.
- Use `amazon_search` for Amazon product discovery, price checks, and product comparisons.
  Set params.query to the product query. You may set params.limit (1-20) and params.marketplace.
- Use `olx_search` for OLX India listings, second-hand goods, used products, used cars/bikes, buy/sell.
  Set params.query to the search term and optionally params.limit.
- Use `irctc_search` for train travel, IRCTC ticket booking, PNR status, train schedules, fares.
  Set params.query to the full user query (preserve station names and dates).
- Use `hindu_news` for news queries, current events, headlines, sports, business, technology news.
  Set params.section to the best matching section from the available list. Optionally set params.limit.
- Use `housing_search` for property listings, apartments, flats, rent/buy queries, real estate in Indian cities.
  Set params.city to the target city. Set params.purpose to rent or buy. Set params.query for extra keywords.
- Use `practo_search` for doctor search, clinic search, specialist discovery, consultation fee queries.
  Set params.city to the city name. Set params.speciality to the type of doctor (dentist, dermatologist etc). Set params.locality if mentioned.
- Use `funclink_guide` when the user wants to be guided through a task on ANY external website — e.g.
  "guide me through booking a hotel on Booking.com", "help me search flights on MakeMyTrip",
  "walk me through buying on Flipkart", "book a cab on Ola", "open Airbnb and find a room".
  Set params.task to the full natural-language task (preserve destination, dates, guests, any details).
  Set params.target_url to the root URL of the website (e.g. https://www.booking.com).
  Set params.website_name to the friendly name (e.g. Booking.com).
- If no tool is needed (general chat, greetings, opinions, calculations), return use_tool=false and tool="".
"""

TOOL_RESPONSE_PROMPT = """
You are Franky, a Ghost Operator powered by browser agents.
You already have the tool result JSON.
Reply conversationally and clearly.

Rules:
- If tool call succeeded, show concise top results with title, price, rating, and URL.
- If tool call failed, explain the issue briefly and suggest what user can try next.
- Keep the response compact and WhatsApp-friendly.
- If relevant, remind the user you can open a live guided FuncLink browser session for them on any website.
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

    # Fallback: if router picks amazon_search without query, use full user message.
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


def _format_practo_reply(result: dict[str, Any]) -> str:
    if not result.get("success"):
        error = str(result.get("error") or "Could not fetch doctor listings.")
        city = str(result.get("city") or "").strip()
        hint = f" Try browsing https://www.practo.com directly for {city}." if city else ""
        return f"Could not find doctors right now: {error}.{hint}"

    doctors = result.get("results") or []
    if not doctors:
        return "No doctors found on Practo for your search. Try adjusting the city or speciality."

    city = str(result.get("city") or "your city").strip()
    speciality = str(result.get("speciality") or "").strip()
    header = f"🩺 Found {len(doctors)} doctor(s)"
    header += f" ({speciality})" if speciality else ""
    header += f" in {city}:"

    lines: list[str] = []
    for i, doc in enumerate(doctors, 1):
        name = str(doc.get("title") or doc.get("name") or "Doctor").strip()
        spec = str(doc.get("speciality") or "").strip()
        exp = str(doc.get("experience") or "").strip()
        fee = str(doc.get("fee") or "").strip()
        location = str(doc.get("location") or "").strip()
        clinic = str(doc.get("clinic") or "").strip()
        rec = str(doc.get("recommendation") or "").strip()
        url = str(doc.get("url") or "").strip()
        meta = " | ".join(v for v in [spec, exp, fee] if v) or "details on site"
        sub = " | ".join(v for v in [clinic, location] if v)
        line = f"{i}. *{name}*\n   {meta}"
        if sub:
            line += f"\n   {sub}"
        if rec:
            line += f"\n   👍 {rec}"
        if url:
            line += f"\n   {url}"
        lines.append(line)
    return header + "\n\n" + "\n\n".join(lines)


def _format_housing_reply(result: dict[str, Any]) -> str:
    if not result.get("success"):
        error = str(result.get("error") or "Could not fetch property listings.")
        city = str(result.get("city") or "").strip()
        hint = f" Try browsing https://www.magicbricks.com directly for {city}." if city else ""
        return f"No listings found: {error}.{hint}"

    listings = result.get("results") or []
    if not listings:
        return "No MagicBricks listings found for your search. Try adjusting the city or keywords."

    city = str(result.get("city") or "your city").strip()
    purpose = str(result.get("purpose") or "rent").strip()
    lines: list[str] = []
    for i, item in enumerate(listings, 1):
        title = str(item.get("title") or "Listing").strip()
        price = str(item.get("price") or "").strip()
        bhk = str(item.get("bhk") or "").strip()
        location = str(item.get("location") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        meta = " | ".join(v for v in [bhk, price, location] if v) or "details on site"
        line = f"{i}. *{title}*\n   {meta}"
        if snippet:
            line += f"\n   {snippet[:100]}{'...' if len(snippet) > 100 else ''}"
        if url:
            line += f"\n   {url}"
        lines.append(line)
    return f"🏠 Found {len(lines)} property listing(s) to {purpose} in {city}:\n\n" + "\n\n".join(lines)


def _format_amazon_account_reply(result: dict[str, Any]) -> str:
    reply = str(result.get("assistant_reply") or "").strip()
    orders = result.get("orders")
    if not isinstance(orders, list) or not orders:
        return reply or "Amazon agent responded but had no message."

    order_lines: list[str] = []
    for i, order in enumerate(orders, 1):
        oid = str(order.get("order_id") or "").strip()
        title = str(order.get("title") or "Item").strip()
        status = str(order.get("status") or "Status unavailable").strip()
        url = str(order.get("detail_url") or "").strip()
        line = f"{i}. #{oid} — *{title}*\n   Status: {status}"
        if url:
            line += f"\n   {url}"
        order_lines.append(line)

    orders_text = "\n\n".join(order_lines)
    return (reply + "\n\n" + orders_text).strip()


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
        await manager.send(sender, f"🔍 Got it! Looking up OLX India for *{query}*...")
        await manager.send(sender, "📡 Connecting to DuckDuckGo and scanning OLX listings...")
        try:
            results = await _fetch_olx_results(query, limit)
        except Exception as exc:  # noqa: BLE001
            return f"OLX search failed: {exc}"
        if results:
            await manager.send(sender, f"✅ Found {len(results)} listing(s)! Formatting results...")
        else:
            await manager.send(sender, "⚠️ No direct matches found, compiling what I have...")
        return _format_olx_reply(results, query)

    if tool_name == "irctc_search":
        query = str(params.get("query") or original_message).strip()
        limit = max(1, min(int(params.get("limit") or 5), 10))
        await manager.send(sender, f"🚂 On it! Searching IRCTC for *{query}*...")
        await manager.send(sender, "📡 Querying IRCTC via public rail database...")
        try:
            results = await _fetch_irctc_results(query, limit)
        except Exception:  # noqa: BLE001
            results = []
        if not results:
            await manager.send(sender, "🔄 Live search returned nothing, falling back to IRCTC catalog...")
            intent = _detect_intent(query)
            results = _fallback_irctc_results(query, intent, limit)
        else:
            await manager.send(sender, f"✅ Got {len(results)} IRCTC result(s)! Packaging them up...")
        return _format_irctc_reply(results, query)

    if tool_name == "hindu_news":
        section = str(params.get("section") or "national").strip().lower()
        limit = max(1, min(int(params.get("limit") or 5), 15))
        await manager.send(sender, f"📰 Fetching latest *{section}* headlines from The Hindu...")
        await manager.send(sender, "📡 Connecting to The Hindu RSS feed...")
        try:
            results = await _fetch_hindu_news(section, limit)
        except Exception as exc:  # noqa: BLE001
            return f"The Hindu news fetch failed: {exc}"
        if results:
            await manager.send(sender, f"✅ Pulled {len(results)} fresh article(s)! Formatting...")
        else:
            await manager.send(sender, "⚠️ Feed returned empty, The Hindu may be temporarily unavailable.")
        return _format_hindu_reply(results, section)

    if tool_name == "housing_search":
        city = str(params.get("city") or "").strip()
        if not city:
            return "I need a city name to search for property listings. For example: *find 2BHK rent in Bengaluru*."
        query = str(params.get("query") or "").strip()
        purpose = str(params.get("purpose") or "rent").strip().lower()
        limit = max(1, min(int(params.get("limit") or 5), 25))
        display_q = f"{query} {purpose} in {city}".strip()
        await manager.send(sender, f"🏠 Searching MagicBricks for *{display_q}*...")
        await manager.send(sender, "📡 Opening MagicBricks property listings page...")
        await manager.send(sender, "🔍 Scanning property cards and extracting prices...")
        result = await execute_tool("housing_listings", {"city": city, "query": query, "purpose": purpose, "limit": limit})
        count = result.get("count") or len(result.get("results") or [])
        if result.get("success") and count:
            await manager.send(sender, f"✅ Found {count} listing(s)! Putting them together...")
        else:
            await manager.send(sender, "⚠️ Live scrape returned nothing, loading fallback catalog...")
        return _format_housing_reply(result)

    if tool_name == "amazon_account":
        AMAZON_ACCOUNT_ACTIVE_SESSIONS.add(sender)
        await manager.send(sender, "🛒 Starting Amazon account agent...")
        await manager.send(sender, "🌐 Opening headless browser and navigating to Amazon login...")
        limit = max(1, min(int(params.get("limit") or 5), 10))
        result = await execute_tool(
            "amazon_account",
            {"session_id": sender, "user_input": original_message, "command": params.get("command", ""), "limit": limit},
        )
        if not result.get("session_active", True):
            AMAZON_ACCOUNT_ACTIVE_SESSIONS.discard(sender)
        return _format_amazon_account_reply(result)

    if tool_name == "practo_search":
        city = str(params.get("city") or "").strip()
        if not city:
            return "I need a city name to search for doctors. For example: *find a dentist in Bengaluru*."
        speciality = str(params.get("speciality") or "").strip()
        locality = str(params.get("locality") or "").strip()
        limit = max(1, min(int(params.get("limit") or 5), 20))
        display = " | ".join(v for v in [speciality or "doctors", locality, city] if v)
        await manager.send(sender, f"🩺 Searching Practo for *{display}*...")
        await manager.send(sender, "📡 Connecting to Practo.com and scanning doctor listings...")
        await manager.send(sender, "🔍 Reading doctor profiles, experience, and fees...")
        result = await execute_tool(
            "practo_doctors",
            {"city": city, "speciality": speciality, "locality": locality, "limit": limit},
        )
        count = result.get("count") or len(result.get("results") or [])
        if result.get("success") and count:
            await manager.send(sender, f"✅ Found {count} doctor(s)! Putting the list together...")
        else:
            await manager.send(sender, "⚠️ Live scrape returned nothing, loading fallback Practo links...")
        return _format_practo_reply(result)

    if tool_name == "funclink_guide":
        task = str(params.get("task") or original_message).strip()
        target_url = str(params.get("target_url") or "").strip()
        website_name = str(params.get("website_name") or target_url).strip()
        if not target_url:
            return "I need to know which website you want to use. For example: *guide me through booking hotels on Booking.com*."
        await manager.send(sender, f"🌐 Creating a live guided session for *{website_name}*...")
        await manager.send(sender, "⚙️ FuncLink is building your step-by-step browser guide...")
        try:
            data = await create_funclink_session(
                user_id=sender,
                task=task,
                target_url=target_url,
            )
        except Exception as exc:  # noqa: BLE001
            return f"Could not create a guided session right now: {exc}. Please try again shortly."
        guided_url = str(data.get("url") or "").strip()
        step_count = data.get("step_count") or ""
        if not guided_url:
            return "FuncLink returned an empty link. Please try again."
        steps_note = f" ({step_count} guided steps)" if step_count else ""
        return (
            f"✅ Your guided session is ready{steps_note}!\n\n"
            f"👇 Open this link in your browser — it will walk you through the task step by step:\n"
            f"{guided_url}\n\n"
            f"_Task: {task}_"
        )

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
                await manager.send(sender, "🛒 Passing your message to Amazon account agent...")
                tool_result = await execute_tool(
                    tool_name,
                    {
                        "session_id": sender,
                        "user_input": message,
                        "limit": _parse_order_limit(message),
                    },
                )
                reply = _format_amazon_account_reply(tool_result)
                if not tool_result.get("session_active", True):
                    AMAZON_ACCOUNT_ACTIVE_SESSIONS.discard(sender)
            else:
                decision = _safe_router_decision(message)

                if decision.get("use_tool") and decision.get("tool"):
                    tool_name = str(decision["tool"])
                    tool_params = decision.get("params", {})

                    if tool_name in _INLINE_TOOL_NAMES:
                        # Inline WS tools — run sub-handler directly on this connection
                        reply = await _dispatch_inline_tool(sender, tool_name, tool_params, message)
                        if not reply:
                            reply = "I could not retrieve results for your query. Please try again."
                    else:
                        # Registry tools (e.g. amazon_search) — go through tool executor
                        if tool_name == "amazon_search":
                            _q = str(tool_params.get("query") or message).strip()
                            await manager.send(sender, f"🛒 Launching Amazon search agent for *{_q}*...")
                            await manager.send(sender, "🌐 Opening Amazon India in headless browser...")
                            await manager.send(sender, "🔎 Scanning product listings and extracting prices...")
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
                    # plain LLM chat
                    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}] + history
                    reply = llm_service.generate(messages)

            # store reply
            memory_service.append(sender, "assistant", reply)

            # send response
            await manager.send(sender, reply)

    except WebSocketDisconnect:
        manager.disconnect(sender)