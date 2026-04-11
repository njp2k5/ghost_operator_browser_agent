from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from tool_registry.registry import register_tool

DEFAULT_LIMIT = 5
MAX_LIMIT = 20
NAVIGATION_TIMEOUT_MS = 20000
WAIT_TIMEOUT_MS = 8000
DEFAULT_MARKETPLACE = "www.amazon.in"
ASIN_PATH_PATTERN = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)
ASIN_TOKEN_PATTERN = re.compile(r"\b([A-Z0-9]{10})\b", re.IGNORECASE)
WORD_PATTERN = re.compile(r"[a-z0-9]+")
QUERY_STOP_TERMS = {
    "amazon",
    "india",
    "in",
    "of",
    "to",
    "the",
    "and",
    "for",
    "with",
    "on",
    "show",
    "find",
    "get",
    "give",
    "search",
    "top",
    "option",
    "options",
    "item",
    "items",
    "one",
    "two",
    "three",
    "four",
    "five",
    "laptop",
    "laptops",
    "under",
    "below",
    "within",
    "less",
    "than",
    "lakh",
    "lakhs",
    "crore",
    "crores",
    "rupee",
    "rupees",
    "rs",
    "budget",
    "buy",
    "price",
    "best",
    "product",
}


tool_definition = {
    "name": "amazon_search",
    "description": "Search Amazon products by keyword and return top listings",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Product keyword to search on Amazon",
            },
            "limit": {
                "type": "number",
                "description": "Maximum number of products to return",
                "default": DEFAULT_LIMIT,
            },
            "marketplace": {
                "type": "string",
                "description": "Amazon domain (for example: www.amazon.in or www.amazon.com)",
                "default": DEFAULT_MARKETPLACE,
            },
        },
        "required": ["query"],
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


def _normalize_marketplace(value: Any) -> str:
    if value is None:
        return DEFAULT_MARKETPLACE

    marketplace = str(value).strip().lower()
    if not marketplace:
        return DEFAULT_MARKETPLACE

    marketplace = marketplace.replace("https://", "").replace("http://", "").strip("/")
    if "." not in marketplace or " " in marketplace:
        raise ValueError("'marketplace' must be a valid domain like 'www.amazon.in'")

    return marketplace


def _extract_asin(text: str) -> str:
    if not text:
        return ""

    path_match = ASIN_PATH_PATTERN.search(text)
    if path_match:
        return path_match.group(1).upper()

    token_match = ASIN_TOKEN_PATTERN.search(text)
    if token_match:
        return token_match.group(1).upper()

    return ""


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in WORD_PATTERN.findall(query.lower()):
        if token.isdigit():
            continue
        if len(token) < 3:
            continue
        if token in QUERY_STOP_TERMS:
            continue
        terms.append(token)
    return terms


def _title_match_count(title: str, terms: list[str]) -> int:
    if not terms:
        return 0
    title_tokens = set(WORD_PATTERN.findall(title.lower()))
    return sum(1 for term in terms if term in title_tokens)


def _to_absolute_amazon_url(href: str, marketplace: str) -> str:
    resolved_href = href.strip()
    if not resolved_href or resolved_href.startswith("javascript:"):
        return ""

    if resolved_href.startswith("/sspa/click"):
        parsed = urlparse(resolved_href)
        qs = parse_qs(parsed.query)
        ad_url = qs.get("url", [""])[0]
        if ad_url:
            resolved_href = unquote(ad_url)

    if resolved_href.startswith("//"):
        resolved_href = f"https:{resolved_href}"

    if resolved_href.startswith("/"):
        return f"https://{marketplace}{resolved_href}"

    if resolved_href.startswith("http://") or resolved_href.startswith("https://"):
        return resolved_href

    return ""


def _canonical_amazon_product_url(href: str, marketplace: str, fallback_asin: str = "") -> str:
    absolute_url = _to_absolute_amazon_url(href, marketplace)
    if not absolute_url:
        if fallback_asin:
            return f"https://{marketplace}/dp/{fallback_asin}"
        return ""

    parsed = urlparse(absolute_url)
    host = parsed.netloc.lower()
    if "amazon." not in host:
        return ""

    asin = _extract_asin(parsed.path)
    if not asin:
        asin = _extract_asin(parsed.query)
    if not asin:
        asin = fallback_asin
    if not asin:
        return ""

    return f"https://{marketplace}/dp/{asin}"


async def _extract_results(page, query: str, marketplace: str, limit: int) -> list[dict[str, str]]:
    cards = page.locator("div.s-main-slot div[data-component-type='s-search-result']")
    total_cards = await cards.count()
    terms = _query_terms(query)

    scored_results: list[tuple[int, dict[str, str]]] = []
    fallback_results: list[tuple[int, dict[str, str]]] = []

    for idx in range(total_cards):
        card = cards.nth(idx)

        title = ""
        product_url = ""
        price = ""
        rating = ""
        reviews_count = ""
        score = 0
        fallback_asin = _extract_asin((await card.get_attribute("data-asin") or "").strip())

        title_node = card.locator("a:has(h2) h2 span").first
        if await title_node.count() > 0:
            title_text = (await title_node.inner_text()).strip()
            title = " ".join(title_text.split())
            score = _title_match_count(title, terms)

        link_node = card.locator("a.a-link-normal:has(h2)").first
        if await link_node.count() == 0:
            link_node = card.locator("a:has(h2)").first

        if await link_node.count() > 0:
            href = await link_node.get_attribute("href")
            if href:
                product_url = _canonical_amazon_product_url(href, marketplace, fallback_asin=fallback_asin)

        if not product_url and fallback_asin:
            product_url = f"https://{marketplace}/dp/{fallback_asin}"

        price_node = card.locator("span.a-price span.a-offscreen").first
        if await price_node.count() > 0:
            price = (await price_node.inner_text()).strip()

        rating_node = card.locator("span.a-icon-alt").first
        if await rating_node.count() > 0:
            rating = (await rating_node.inner_text()).strip()

        reviews_node = card.locator("span.a-size-base.s-underline-text").first
        if await reviews_node.count() > 0:
            reviews_count = (await reviews_node.inner_text()).strip()

        if title and product_url:
            record = (
                score,
                {
                    "title": title,
                    "price": price,
                    "rating": rating,
                    "reviews_count": reviews_count,
                    "url": product_url,
                },
            )
            fallback_results.append(record)

            if terms:
                required_matches = max(1, (len(terms) + 1) // 2)
                if score >= required_matches:
                    scored_results.append(record)
            else:
                scored_results.append(record)

    selected_results = scored_results if scored_results else fallback_results
    selected_results.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in selected_results[:limit]]


async def run(params: dict[str, Any]) -> dict[str, Any]:
    query = str(params.get("query", "")).strip()
    if not query:
        return {
            "success": False,
            "error": "'query' is required",
            "results": [],
        }

    limit = _normalize_limit(params.get("limit"))
    marketplace = _normalize_marketplace(params.get("marketplace"))
    search_url = f"https://{marketplace}/s?k={quote_plus(query)}"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        page = await context.new_page()
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        page.set_default_timeout(WAIT_TIMEOUT_MS)

        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass

            if "validateCaptcha" in page.url:
                return {
                    "success": False,
                    "query": query,
                    "marketplace": marketplace,
                    "limit": limit,
                    "results": [],
                    "error": "Amazon presented a CAPTCHA. Retry later or run with a logged-in browser session.",
                }

            await page.wait_for_selector(
                "div.s-main-slot div[data-component-type='s-search-result']",
                timeout=WAIT_TIMEOUT_MS,
            )

            results = await _extract_results(page, query, marketplace, limit)

            if not results:
                return {
                    "success": False,
                    "query": query,
                    "marketplace": marketplace,
                    "limit": limit,
                    "results": [],
                    "error": "No Amazon listings found for this query.",
                }

            return {
                "success": True,
                "query": query,
                "marketplace": marketplace,
                "limit": limit,
                "count": len(results),
                "results": results,
            }
        except PlaywrightTimeoutError:
            return {
                "success": False,
                "query": query,
                "marketplace": marketplace,
                "limit": limit,
                "results": [],
                "error": "Timed out while loading Amazon search results",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "query": query,
                "marketplace": marketplace,
                "limit": limit,
                "results": [],
                "error": str(exc),
            }
        finally:
            await page.close()
            await context.close()
            await browser.close()


register_tool(tool_definition, run)