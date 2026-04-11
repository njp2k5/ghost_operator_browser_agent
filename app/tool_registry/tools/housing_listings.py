from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
import requests

from tool_registry.registry import register_tool

DEFAULT_LIMIT = 5
MAX_LIMIT = 25
REQUEST_TIMEOUT_S = 25

PRICE_PATTERN = re.compile(r"(?:₹|Rs\.?|INR)\s?[\d,]+(?:\s?(?:L|Cr|lac|lakh|crore))?", re.IGNORECASE)
BHK_PATTERN = re.compile(r"\b(\d+)\s*BHK\b|\bStudio\b", re.IGNORECASE)
WORD_PATTERN = re.compile(r"[a-z0-9]+")


def _format_error(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return text
    return f"{type(exc).__name__}: unspecified error"


tool_definition = {
    "name": "housing_listings",
    "description": "Find MagicBricks listings for rent or buy without API keys",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "City to search in (for example: Bengaluru, Mumbai)",
            },
            "query": {
                "type": "string",
                "description": "Additional keywords like locality, 2BHK, furnished",
            },
            "purpose": {
                "type": "string",
                "description": "Listing purpose: rent or buy",
                "default": "rent",
            },
            "max_price": {
                "type": "number",
                "description": "Optional max price budget",
            },
            "min_bhk": {
                "type": "number",
                "description": "Optional minimum BHK",
            },
            "limit": {
                "type": "number",
                "description": "Maximum listings to return",
                "default": DEFAULT_LIMIT,
            },
        },
        "required": ["city"],
    },
}


def _normalize_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIMIT

    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("'limit' must be a number") from exc

    if limit < 1:
        raise ValueError("'limit' must be greater than 0")
    return min(limit, MAX_LIMIT)


def _normalize_purpose(value: Any) -> str:
    if value is None:
        return "rent"

    purpose = str(value).strip().lower()
    if purpose in {"rent", "rental"}:
        return "rent"
    if purpose in {"buy", "sale", "sell"}:
        return "buy"
    raise ValueError("'purpose' must be either 'rent' or 'buy'")


def _extract_price(text: str) -> str:
    match = PRICE_PATTERN.search(text)
    return match.group(0).strip() if match else ""


def _extract_bhk(text: str) -> str:
    match = BHK_PATTERN.search(text)
    if not match:
        return ""
    if match.group(1):
        return f"{match.group(1)} BHK"
    return "Studio"


def _resolve_url(raw_href: str) -> str:
    url = raw_href.strip()
    if not url:
        return ""

    if url.startswith("/"):
        url = f"https://www.magicbricks.com{url}"

    lower = url.lower()
    if "magicbricks.com" not in lower:
        return ""
    if not lower.startswith("http://") and not lower.startswith("https://"):
        return ""

    return url


def _slugify_city(city: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")


def _city_for_commonfloor(city: str) -> str:
    normalized = city.strip().lower()
    aliases = {
        "bengaluru": "bangalore",
        "mumbai": "mumbai",
        "delhi": "delhi",
        "new delhi": "delhi",
        "chennai": "chennai",
        "kolkata": "kolkata",
        "pune": "pune",
        "hyderabad": "hyderabad",
    }
    return aliases.get(normalized, normalized)


def _city_for_magicbricks(city: str) -> str:
    normalized = city.strip().lower()
    aliases = {
        "bengaluru": "Bangalore",
        "bangalore": "Bangalore",
        "mumbai": "Mumbai",
        "delhi": "Delhi",
        "new delhi": "Delhi",
        "chennai": "Chennai",
        "kolkata": "Kolkata",
        "pune": "Pune",
        "hyderabad": "Hyderabad",
    }
    return aliases.get(normalized, city.strip().title())


def _query_terms(query: str) -> list[str]:
    return [token for token in WORD_PATTERN.findall(query.lower()) if len(token) >= 3]


def _title_score(title: str, terms: list[str]) -> int:
    if not terms:
        return 1
    title_tokens = set(WORD_PATTERN.findall(title.lower()))
    return sum(1 for term in terms if term in title_tokens)


def _fallback_housing_links(city: str, purpose: str, query: str, limit: int) -> list[dict[str, str]]:
    city_name = _city_for_magicbricks(city)
    bhk = _extract_bhk(query)
    rent_path = (
        "https://www.magicbricks.com/property-for-rent/residential-real-estate?"
        f"cityName={quote_plus(city_name)}"
    )
    buy_path = (
        "https://www.magicbricks.com/property-for-sale/residential-real-estate?"
        f"cityName={quote_plus(city_name)}"
    )
    base_path = rent_path if purpose == "rent" else buy_path
    action_text = "rent" if purpose == "rent" else "buy"

    links = [
        {
            "title": f"MagicBricks {purpose.title()} Listings in {city}",
            "price": "",
            "bhk": bhk,
            "location": city,
            "url": base_path,
            "snippet": f"Open {purpose} listings in {city} on MagicBricks",
        },
        {
            "title": f"Top {purpose.title()} Property Results in {city}",
            "price": "",
            "bhk": bhk,
            "location": city,
            "url": (
                "https://www.magicbricks.com/property-for-rent/residential-real-estate?"
                f"cityName={quote_plus(city_name)}"
            ),
            "snippet": f"Browse property listings for {action_text} in {city}",
        },
    ]

    if bhk:
        links.insert(
            1,
            {
                "title": f"{bhk} Listings for {purpose} in {city}",
                "price": "",
                "bhk": bhk,
                "location": city,
                "url": base_path,
                "snippet": f"Browse {bhk} listings for {action_text} in {city}",
            },
        )

    return links[:limit]


def _is_security_alert(title: str, content: str) -> bool:
    title_lower = title.strip().lower()
    if any(marker in title_lower for marker in ("security alert", "access denied", "attention required", "forbidden")):
        return True

    head = content[:50000].lower()
    hard_markers = (
        "error code 1020",
        "you have been blocked",
        "request blocked",
        "cf-error-code",
        "captcha",
    )
    return any(marker in head for marker in hard_markers)


def _collect_housing_links_from_html(html: str, limit: int, query_terms: list[str]) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    seen_urls: set[str] = set()
    listings: list[tuple[int, dict[str, str]]] = []

    for anchor in soup.select("a[href]"):

        href = str(anchor.get("href") or "")
        if not href:
            continue

        if "/propertydetails/" not in href.lower():
            continue

        listing_url = _resolve_url(href)
        if not listing_url:
            continue

        key = listing_url.lower().rstrip("/")
        if key in seen_urls:
            continue
        seen_urls.add(key)

        title = " ".join(anchor.get_text(" ", strip=True).split())
        context_text = ""
        parent = anchor.parent
        if parent:
            context_text = " ".join(parent.get_text(" ", strip=True).split())

        resolved_title = title or "MagicBricks Listing"
        combined = f"{resolved_title} {context_text}".strip()
        score = _title_score(combined, query_terms)

        listings.append(
            (
                score,
                {
                    "title": resolved_title,
                    "price": _extract_price(combined),
                    "bhk": _extract_bhk(combined),
                    "location": "",
                    "url": listing_url,
                    "snippet": context_text[:260],
                },
            )
        )

    listings.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in listings[:limit]]


async def _search_housing_direct(city: str, purpose: str, query: str, limit: int) -> list[dict[str, str]]:
    city_name = _city_for_magicbricks(city)
    action = "rent" if purpose == "rent" else "sale"
    candidates = [
        (
            f"https://www.magicbricks.com/property-for-{action}/residential-real-estate?"
            f"cityName={quote_plus(city_name)}"
        ),
        (
            "https://www.magicbricks.com/property-for-rent/residential-real-estate?"
            f"cityName={quote_plus(city_name)}"
        ),
        "https://www.magicbricks.com/",
    ]
    query_terms = _query_terms(query)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    for target_url in candidates:
        response = await asyncio.to_thread(
            requests.get,
            target_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT_S,
            allow_redirects=True,
        )
        if response.status_code != 200:
            continue

        content = response.text
        soup = BeautifulSoup(content, "html.parser")
        page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if _is_security_alert(page_title, content):
            continue

        links = _collect_housing_links_from_html(content, limit, query_terms)
        if links:
            return links

    return []


async def run(params: dict[str, Any]) -> dict[str, Any]:
    city = str(params.get("city", "")).strip()
    if not city:
        return {
            "success": False,
            "error": "'city' is required",
            "results": [],
        }

    query = str(params.get("query", "")).strip()
    purpose = _normalize_purpose(params.get("purpose"))
    limit = _normalize_limit(params.get("limit"))
    max_price = params.get("max_price")
    min_bhk = params.get("min_bhk")

    if max_price is not None:
        try:
            max_price = int(max_price)
        except (TypeError, ValueError):
            max_price = None

    if min_bhk is not None:
        try:
            min_bhk = int(min_bhk)
        except (TypeError, ValueError):
            min_bhk = None

    try:
        results = await _search_housing_direct(city, purpose, query, limit)
        source = "housing_direct"
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "city": city,
            "query": query,
            "purpose": purpose,
            "limit": limit,
            "results": [],
            "error": _format_error(exc),
        }

    if results and (min_bhk is not None or max_price is not None):
        filtered: list[dict[str, str]] = []
        for item in results:
            keep = True
            if min_bhk is not None:
                bhk_match = re.search(r"(\d+)\s*BHK", item.get("bhk", ""), flags=re.IGNORECASE)
                if bhk_match and int(bhk_match.group(1)) < min_bhk:
                    keep = False

            if keep and max_price is not None:
                price_text = item.get("price", "")
                digits = re.sub(r"[^\d]", "", price_text)
                if digits:
                    if int(digits) > max_price:
                        keep = False

            if keep:
                filtered.append(item)

        results = filtered[:limit]

    if not results:
        fallback = _fallback_housing_links(city, purpose, query, limit)
        return {
            "success": True,
            "city": city,
            "query": query,
            "purpose": purpose,
            "limit": limit,
            "source": "magicbricks_fallback_catalog",
            "count": len(fallback),
            "results": fallback,
            "note": "Direct MagicBricks extraction failed in this session; returning MagicBricks direct links.",
        }

    return {
        "success": True,
        "city": city,
        "query": query,
        "purpose": purpose,
        "limit": limit,
        "source": "magicbricks_direct",
        "count": len(results),
        "results": results,
    }


register_tool(tool_definition, run)