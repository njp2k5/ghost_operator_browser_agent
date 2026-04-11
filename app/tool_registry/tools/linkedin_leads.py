from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from tool_registry.registry import register_tool

DEFAULT_LIMIT = 5
MAX_LIMIT = 25
NAVIGATION_TIMEOUT_MS = 15000
WAIT_TIMEOUT_MS = 5000


tool_definition = {
    "name": "linkedin_leads",
    "description": "Fetch LinkedIn profiles based on a search query",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for LinkedIn people results",
            },
            "limit": {
                "type": "number",
                "description": "Maximum number of profiles to return",
                "default": DEFAULT_LIMIT,
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


async def _extract_results(page, limit: int) -> list[dict[str, str]]:
    selectors = [
        'li.reusable-search__result-container',
        'li.search-results-container__occluded-item',
        'div[data-chameleon-result-urn]',
    ]

    located_selector = None
    for selector in selectors:
        try:
            await page.wait_for_selector(selector, timeout=WAIT_TIMEOUT_MS)
            located_selector = selector
            break
        except PlaywrightTimeoutError:
            continue

    if not located_selector:
        return []

    cards = await page.locator(located_selector).all()
    results: list[dict[str, str]] = []

    for card in cards:
        if len(results) >= limit:
            break

        name = ""
        headline = ""
        profile_url = ""

        anchor_selectors = [
            'a[href*="/in/"]',
            'span.entity-result__title-text a',
            'a.app-aware-link',
        ]
        for anchor_selector in anchor_selectors:
            anchor = card.locator(anchor_selector).first
            if await anchor.count() > 0:
                href = await anchor.get_attribute("href")
                text = (await anchor.inner_text()).strip()
                if href:
                    profile_url = href.split("?")[0]
                if text:
                    name = " ".join(text.split())
                if name or profile_url:
                    break

        headline_selectors = [
            '.entity-result__primary-subtitle',
            '.entity-result__summary',
            '.t-14.t-black.t-normal',
        ]
        for headline_selector in headline_selectors:
            headline_node = card.locator(headline_selector).first
            if await headline_node.count() > 0:
                text = (await headline_node.inner_text()).strip()
                if text:
                    headline = " ".join(text.split())
                    break

        if name or headline or profile_url:
            results.append(
                {
                    "name": name,
                    "headline": headline,
                    "profile_url": profile_url,
                }
            )

    return results[:limit]


async def run(params: dict[str, Any]) -> dict[str, Any]:
    query = str(params.get("query", "")).strip()
    if not query:
        return {
            "success": False,
            "error": "'query' is required",
            "results": [],
        }

    limit = _normalize_limit(params.get("limit"))
    search_url = (
        "https://www.linkedin.com/search/results/people/?keywords="
        f"{quote_plus(query)}"
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        page.set_default_timeout(WAIT_TIMEOUT_MS)

        try:
            await page.goto(search_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=WAIT_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass

            current_url = page.url
            if "/search/results/people/" not in current_url:
                await page.goto(search_url, wait_until="domcontentloaded")

            results = await _extract_results(page, limit)

            if not results:
                return {
                    "success": False,
                    "query": query,
                    "limit": limit,
                    "results": [],
                    "error": "No LinkedIn people results found. LinkedIn may require authentication or changed page selectors.",
                }

            return {
                "success": True,
                "query": query,
                "limit": limit,
                "count": len(results),
                "results": results,
            }
        except PlaywrightTimeoutError:
            return {
                "success": False,
                "query": query,
                "limit": limit,
                "results": [],
                "error": "Timed out while loading LinkedIn search results",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "query": query,
                "limit": limit,
                "results": [],
                "error": str(exc),
            }
        finally:
            await page.close()
            await browser.close()


register_tool(tool_definition, run)
