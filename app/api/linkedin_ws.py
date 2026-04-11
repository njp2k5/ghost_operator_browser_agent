from __future__ import annotations

import logging
import re
import time
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

REQUEST_TIMEOUT = 30.0
DEFAULT_LIMIT = 5
MAX_LIMIT = 25

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(name)s  -  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("olx_ws")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _send(ws: WebSocket, stage: str, message: str, **extra) -> None:
    payload: dict = {"stage": stage, "message": message}
    payload.update(extra)
    await ws.send_json(payload)


async def _recv(ws: WebSocket) -> str:
    data = await ws.receive_json()
    return str(data.get("data", "") or data.get("message", "")).strip()


# ---------------------------------------------------------------------------
# OLX search via DuckDuckGo (OLX direct access is blocked by Cloudflare)
# ---------------------------------------------------------------------------

async def _fetch_olx_results(query: str, limit: int) -> list[dict[str, str]]:
    """Query DuckDuckGo HTML with site:olx.in and return up to `limit` listings."""
    search_url = (
        "https://html.duckduckgo.com/html/"
        f"?q=site%3Aolx.in+{quote_plus(query)}&kl=in-en"
    )
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

    log.info("Querying DuckDuckGo: site:olx.in %s", query)
    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT,
        http2=False,
    ) as client:
        try:
            resp = await client.get(search_url)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.error("DuckDuckGo fetch failed (%s): %s", type(exc).__name__, exc)
            raise RuntimeError(f"Search request failed ({type(exc).__name__}): {exc}") from exc

    log.info("DuckDuckGo response: %d chars, status %d", len(resp.text), resp.status_code)
    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[dict[str, str]] = []

    for result in soup.select("div.result"):
        if len(results) >= limit:
            break

        # Title + raw href
        anchor = result.select_one("a.result__a") or result.select_one(".result__title a")
        if not anchor:
            continue

        title = anchor.get_text(strip=True)
        raw_href = str(anchor.get("href") or "")

        # DDG wraps real URLs in a redirect like /l/?uddg=<encoded_url>
        parsed = urlparse(raw_href)
        qs = parse_qs(parsed.query)
        url = qs.get("uddg", [raw_href])[0]

        # Keep only OLX links
        if "olx.in" not in url and "olx.com" not in url:
            continue

        # Try to pull a price from the snippet text (e.g. "₹ 35,000" or "Rs. 500")
        price = ""
        snippet_el = result.select_one(".result__snippet")
        if snippet_el:
            snippet = snippet_el.get_text(strip=True)
            m = re.search(r"[₹Rr][Ss]?\.?\s*[\d,]+", snippet)
            if m:
                price = m.group(0)

        if title or url:
            results.append({"title": title, "price": price, "location": "", "url": url})
            log.debug("Listing: %r  price=%r  url=%s", title, price, url)

    log.info("Extracted %d OLX listing(s) from DuckDuckGo (limit=%d)", len(results), limit)
    return results


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/olx")
async def olx_websocket(websocket: WebSocket) -> None:
    client = websocket.client
    client_id = f"{client.host}:{client.port}" if client else "unknown"
    session_start = time.perf_counter()

    log.info("[%s] WebSocket connection opened", client_id)
    await websocket.accept()

    try:
        # Stage 1 - announce
        await _send(websocket, "init", "Connected to OLX search agent.")
        log.info("[%s] Stage 1 complete - announced", client_id)

        # Stage 2 - search query
        await _send(websocket, "query", "What would you like to search for on OLX?")
        query = await _recv(websocket)
        if not query:
            log.warning("[%s] Empty query - aborting", client_id)
            await _send(websocket, "error", "Search query cannot be empty.")
            return
        log.info("[%s] Query received: %r", client_id, query)

        # Stage 3 - result limit
        await _send(
            websocket,
            "limit_prompt",
            f"How many top results do you want? (default {DEFAULT_LIMIT}, max {MAX_LIMIT}):",
        )
        limit_str = await _recv(websocket)
        try:
            limit = max(1, min(MAX_LIMIT, int(limit_str)))
        except (ValueError, TypeError):
            limit = DEFAULT_LIMIT
        log.info("[%s] Limit set to %d", client_id, limit)

        # Stage 4 - fetch
        await _send(websocket, "searching", f"Searching OLX for '{query}', fetching top {limit} results...")
        log.info("[%s] Stage 4 - calling httpx fetch", client_id)

        t0 = time.perf_counter()
        try:
            results = await _fetch_olx_results(query, limit)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] Fetch error: %s", client_id, exc)
            await _send(websocket, "error", f"Failed to fetch OLX results: {exc}")
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info("[%s] Fetch done in %.0f ms - %d result(s)", client_id, elapsed_ms, len(results))

        # Stage 5 - results
        if results:
            await _send(
                websocket,
                "results",
                f"Found {len(results)} listing(s) for '{query}'.",
                results=results,
                count=len(results),
            )
        else:
            await _send(
                websocket,
                "no_results",
                f"No listings found on OLX for '{query}'.",
            )

        total_s = time.perf_counter() - session_start
        log.info("[%s] Session complete in %.1f s", client_id, total_s)

    except WebSocketDisconnect:
        log.info("[%s] Client disconnected", client_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("[%s] Unhandled error: %s", client_id, exc)
        try:
            await _send(websocket, "error", f"Unexpected error: {exc}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("[%s] WebSocket closed", client_id)
