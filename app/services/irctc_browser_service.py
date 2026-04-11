from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

BROWSER_TIMEOUT_MS = 25000
NAV_TIMEOUT_MS = 45000
PUBLIC_TIMEOUT_S = 20.0


class IRCTCBrowserService:
    async def _public_search_results(self, query: str, limit: int) -> list[dict[str, str]]:
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}&kl=in-en"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": "https://duckduckgo.com/",
        }

        async with httpx.AsyncClient(headers=headers, timeout=PUBLIC_TIMEOUT_S, follow_redirects=True) as client:
            response = await client.get(search_url)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")
        results: list[dict[str, str]] = []
        seen: set[str] = set()

        allowed_domains = (
            "irctc.co.in",
            "confirmtkt.com",
            "railyatri.in",
            "ixigo.com",
            "railmitra.com",
        )

        for node in soup.select("div.result"):
            if len(results) >= limit:
                break

            anchor = node.select_one("a.result__a") or node.select_one(".result__title a")
            if not anchor:
                continue

            raw_href = str(anchor.get("href") or "")
            parsed = urlparse(raw_href)
            href = parse_qs(parsed.query).get("uddg", [raw_href])[0]
            href_l = href.lower()
            if not any(domain in href_l for domain in allowed_domains):
                continue

            key = href_l.rstrip("/")
            if key in seen:
                continue
            seen.add(key)

            snippet = ""
            snippet_node = node.select_one(".result__snippet")
            if snippet_node:
                snippet = " ".join(snippet_node.get_text(" ", strip=True).split())

            results.append(
                {
                    "title": " ".join(anchor.get_text(" ", strip=True).split()),
                    "url": href,
                    "snippet": snippet,
                }
            )

        return results

    async def get_pnr_status_from_public_web(self, pnr: str) -> dict[str, Any]:
        query = f"pnr status {pnr}"
        results = await self._public_search_results(query, limit=5)
        if not results:
            raise RuntimeError("No public PNR pages found")

        merged_text = " ".join(item.get("snippet", "") for item in results)
        booking_match = re.search(r"booking status[:\-]?\s*([A-Z0-9\s/]+)", merged_text, flags=re.IGNORECASE)
        current_match = re.search(r"current status[:\-]?\s*([A-Z0-9\s/]+)", merged_text, flags=re.IGNORECASE)
        train_no = re.search(r"\b\d{5}\b", merged_text)

        return {
            "pnr": pnr,
            "train_number": train_no.group(0) if train_no else "",
            "booking_status": booking_match.group(1).strip() if booking_match else "",
            "current_status": current_match.group(1).strip() if current_match else "",
            "results": results,
        }

    async def search_trains_with_fare_public(
        self,
        from_station: str,
        to_station: str,
        date_of_journey: str,
        limit: int,
    ) -> list[dict[str, str]]:
        query = f"train fare {from_station} to {to_station} {date_of_journey}"
        results = await self._public_search_results(query, limit=max(limit, 5))

        mapped: list[dict[str, str]] = []
        for item in results:
            text = f"{item.get('title', '')} {item.get('snippet', '')}"
            fare_match = re.search(r"₹\s*[\d,]+", text)
            train_no_match = re.search(r"\b\d{5}\b", text)
            mapped.append(
                {
                    "train_number": train_no_match.group(0) if train_no_match else "",
                    "train_name": item.get("title", ""),
                    "fare": fare_match.group(0) if fare_match else "",
                    "details": item.get("snippet", ""),
                    "url": item.get("url", ""),
                }
            )

        return mapped[:limit]

    async def _open_page(self):
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-http2",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_timeout(BROWSER_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        return playwright, browser, context, page

    async def _goto_irctc_train_search(self, page) -> None:
        urls = [
            "https://www.irctc.co.in/nget/train-search",
            "https://www.irctc.co.in/",
        ]
        last_error: Exception | None = None
        for url in urls:
            try:
                await page.goto(url, wait_until="domcontentloaded")
                if url.endswith("/"):
                    await page.goto("https://www.irctc.co.in/nget/train-search", wait_until="domcontentloaded")
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue

        if last_error:
            raise RuntimeError(f"Failed to open IRCTC website: {last_error}") from last_error
        raise RuntimeError("Failed to open IRCTC website")

    async def _close_page(self, playwright, browser, context, page) -> None:
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await playwright.stop()
        except Exception:  # noqa: BLE001
            pass

    async def _accept_cookie_if_present(self, page) -> None:
        selectors = [
            "button:has-text('OK')",
            "button:has-text('I Agree')",
            "button:has-text('Accept')",
            "button:has-text('Allow')",
        ]
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if await button.count() > 0:
                    await button.click(timeout=2000)
                    return
            except Exception:  # noqa: BLE001
                continue

    async def _ensure_no_captcha_gate(self, page) -> None:
        content = (await page.content()).lower()
        if "captcha" in content and "enter captcha" in content:
            raise RuntimeError("IRCTC captcha encountered in headless mode")

    async def _click_first(self, page, selectors: list[str]) -> bool:
        for selector in selectors:
            try:
                element = page.locator(selector).first
                if await element.count() > 0:
                    await element.click()
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _fill_first(self, page, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            try:
                element = page.locator(selector).first
                if await element.count() == 0:
                    continue
                await element.click()
                await element.fill("")
                await element.type(value, delay=20)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _type_station_autocomplete(self, page, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            try:
                field = page.locator(selector).first
                if await field.count() == 0:
                    continue
                await field.click()
                await field.fill("")
                await field.type(value, delay=25)
                try:
                    await page.wait_for_timeout(600)
                    await field.press("ArrowDown")
                    await field.press("Enter")
                except Exception:  # noqa: BLE001
                    pass
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def get_pnr_status(self, pnr: str) -> dict[str, Any]:
        playwright, browser, context, page = await self._open_page()
        try:
            await self._goto_irctc_train_search(page)
            await self._accept_cookie_if_present(page)
            await self._ensure_no_captcha_gate(page)

            opened = await self._click_first(
                page,
                [
                    "text=PNR STATUS",
                    "button:has-text('PNR STATUS')",
                    "a:has-text('PNR STATUS')",
                    "div:has-text('PNR STATUS')",
                ],
            )
            if not opened:
                raise RuntimeError("Could not open PNR status section on IRCTC page")

            filled = await self._fill_first(
                page,
                [
                    "input[placeholder*='PNR']",
                    "input[aria-label*='PNR']",
                    "input[name*='pnr']",
                    "input[type='text']",
                ],
                pnr,
            )
            if not filled:
                raise RuntimeError("Could not find PNR input field")

            clicked = await self._click_first(
                page,
                [
                    "button:has-text('Get Status')",
                    "button:has-text('Check Status')",
                    "button:has-text('Search')",
                    "button:has-text('Submit')",
                ],
            )
            if not clicked:
                raise RuntimeError("Could not find PNR submit button")

            await page.wait_for_timeout(2500)
            await self._ensure_no_captcha_gate(page)

            text_candidates = [
                "section:has-text('PNR')",
                "div:has-text('Current Status')",
                "div:has-text('Booking Status')",
                "table",
                "body",
            ]
            result_text = ""
            for selector in text_candidates:
                try:
                    block = page.locator(selector).first
                    if await block.count() == 0:
                        continue
                    value = " ".join((await block.inner_text()).split())
                    if len(value) > 60 and ("status" in value.lower() or pnr in value):
                        result_text = value
                        break
                except Exception:  # noqa: BLE001
                    continue

            if not result_text:
                raise RuntimeError("PNR response could not be parsed from IRCTC page")

            booking_match = re.search(r"booking status[:\-]?\s*([^|,;]+)", result_text, flags=re.IGNORECASE)
            current_match = re.search(r"current status[:\-]?\s*([^|,;]+)", result_text, flags=re.IGNORECASE)
            train_no = re.search(r"\b\d{5}\b", result_text)

            return {
                "pnr": pnr,
                "train_number": train_no.group(0) if train_no else "",
                "booking_status": booking_match.group(1).strip() if booking_match else "",
                "current_status": current_match.group(1).strip() if current_match else "",
                "raw_text": result_text[:1800],
            }
        finally:
            await self._close_page(playwright, browser, context, page)

    async def search_trains_with_fare(
        self,
        from_station: str,
        to_station: str,
        date_of_journey: str,
        travel_class: str | None,
        limit: int,
    ) -> list[dict[str, str]]:
        playwright, browser, context, page = await self._open_page()
        try:
            await self._goto_irctc_train_search(page)
            await self._accept_cookie_if_present(page)
            await self._ensure_no_captcha_gate(page)

            source_filled = await self._type_station_autocomplete(
                page,
                [
                    "input[placeholder*='From*']",
                    "input[placeholder*='From']",
                    "input[aria-label*='From']",
                    "app-train-search input",
                ],
                from_station,
            )
            dest_filled = await self._type_station_autocomplete(
                page,
                [
                    "input[placeholder*='To*']",
                    "input[placeholder*='To']",
                    "input[aria-label*='To']",
                    "app-train-search input",
                ],
                to_station,
            )
            if not source_filled or not dest_filled:
                raise RuntimeError("Could not fill source/destination station fields")

            date_set = await self._fill_first(
                page,
                [
                    "input[placeholder*='Journey Date']",
                    "input[aria-label*='Journey Date']",
                    "input[placeholder*='Date']",
                ],
                date_of_journey,
            )
            if not date_set:
                try:
                    date_input = page.locator("input[placeholder*='Journey Date']").first
                    await date_input.evaluate("el => el.removeAttribute('readonly')")
                    await date_input.fill(date_of_journey)
                    date_set = True
                except Exception:  # noqa: BLE001
                    date_set = False

            if not date_set:
                raise RuntimeError("Could not set journey date")

            if travel_class:
                await self._click_first(
                    page,
                    [
                        "span:has-text('All Classes')",
                        "span:has-text('Class')",
                        "label:has-text('Class')",
                    ],
                )
                await self._click_first(page, [f"li:has-text('{travel_class.upper()}')", f"span:has-text('{travel_class.upper()}')"])

            searched = await self._click_first(
                page,
                [
                    "button:has-text('Search')",
                    "button:has-text('Find Trains')",
                    "button:has-text('Search Trains')",
                ],
            )
            if not searched:
                raise RuntimeError("Could not click train search button")

            await page.wait_for_timeout(3000)
            await self._ensure_no_captcha_gate(page)

            card_selectors = [
                "app-train-avl-enq",
                "div.train_avl_enq_box",
                "div.train-heading",
                "div.tbis-div",
            ]
            cards = []
            for selector in card_selectors:
                try:
                    loc = page.locator(selector)
                    count = await loc.count()
                    if count > 0:
                        cards = [loc.nth(i) for i in range(min(count, limit))]
                        break
                except Exception:  # noqa: BLE001
                    continue

            if not cards:
                raise RuntimeError("No train cards found on IRCTC page")

            results: list[dict[str, str]] = []
            for card in cards:
                try:
                    raw = " ".join((await card.inner_text()).split())
                    if not raw:
                        continue
                    train_number_match = re.search(r"\b\d{5}\b", raw)
                    fare_match = re.search(r"₹\s*[\d,]+", raw)

                    train_name = ""
                    try:
                        heading = card.locator("h5, h4, h3, .train-heading").first
                        if await heading.count() > 0:
                            train_name = " ".join((await heading.inner_text()).split())
                    except Exception:  # noqa: BLE001
                        pass

                    results.append(
                        {
                            "train_number": train_number_match.group(0) if train_number_match else "",
                            "train_name": train_name,
                            "fare": fare_match.group(0) if fare_match else "",
                            "details": raw[:800],
                        }
                    )
                except Exception:  # noqa: BLE001
                    continue

            return results[:limit]
        finally:
            await self._close_page(playwright, browser, context, page)


irctc_browser_service = IRCTCBrowserService()
