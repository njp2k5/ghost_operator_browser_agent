from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

REQUEST_TIMEOUT = 20.0
DEFAULT_LIMIT = 10
MAX_LIMIT = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(name)s  -  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("hindu_ws")

# The Hindu RSS feeds keyed by section name (lowercase)
_SECTION_FEEDS: dict[str, str] = {
    "top":           "https://www.thehindu.com/feeder/default.rss",
    "news":          "https://www.thehindu.com/news/feeder/default.rss",
    "national":      "https://www.thehindu.com/news/national/feeder/default.rss",
    "international": "https://www.thehindu.com/news/international/feeder/default.rss",
    "states":        "https://www.thehindu.com/news/states/feeder/default.rss",
    "cities":        "https://www.thehindu.com/news/cities/feeder/default.rss",
    "opinion":       "https://www.thehindu.com/opinion/feeder/default.rss",
    "editorial":     "https://www.thehindu.com/opinion/editorial/feeder/default.rss",
    "business":      "https://www.thehindu.com/business/feeder/default.rss",
    "economy":       "https://www.thehindu.com/business/Economy/feeder/default.rss",
    "markets":       "https://www.thehindu.com/business/markets/feeder/default.rss",
    "sport":         "https://www.thehindu.com/sport/feeder/default.rss",
    "cricket":       "https://www.thehindu.com/sport/cricket/feeder/default.rss",
    "football":      "https://www.thehindu.com/sport/football/feeder/default.rss",
    "entertainment": "https://www.thehindu.com/entertainment/feeder/default.rss",
    "movies":        "https://www.thehindu.com/entertainment/movies/feeder/default.rss",
    "science":       "https://www.thehindu.com/sci-tech/science/feeder/default.rss",
    "technology":    "https://www.thehindu.com/sci-tech/technology/feeder/default.rss",
    "health":        "https://www.thehindu.com/sci-tech/health/feeder/default.rss",
    "lifestyle":     "https://www.thehindu.com/life-and-style/feeder/default.rss",
}
_SECTION_LIST = ", ".join(sorted(_SECTION_FEEDS.keys()))


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
# Fetch + parse The Hindu RSS
# ---------------------------------------------------------------------------

async def _fetch_hindu_news(section: str, limit: int) -> list[dict[str, str]]:
    feed_url = _SECTION_FEEDS.get(section, _SECTION_FEEDS["top"])
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }

    log.info("Fetching The Hindu RSS: %s (%s)", section, feed_url)
    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT,
        http2=False,
    ) as client:
        try:
            resp = await client.get(feed_url)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.error("RSS fetch failed (%s): %s", type(exc).__name__, exc)
            raise RuntimeError(f"Could not fetch RSS feed ({type(exc).__name__}): {exc}") from exc

    log.info("RSS response: %d chars, status %d", len(resp.text), resp.status_code)

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        raise RuntimeError(f"RSS XML parse error: {exc}") from exc

    items = root.findall(".//item")
    log.info("RSS items found: %d (returning up to %d)", len(items), limit)

    results: list[dict[str, str]] = []
    for item in items[:limit]:
        title     = (item.findtext("title") or "").strip()
        url       = (item.findtext("link") or item.findtext("guid") or "").strip()
        pub_date  = (item.findtext("pubDate") or "").strip()
        # description may contain HTML; strip tags with a simple regex alternative
        desc_raw  = item.findtext("description") or ""
        desc = re.sub(r"<[^>]+>", "", desc_raw).strip()

        if title or url:
            results.append({
                "title":       title,
                "url":         url,
                "published":   pub_date,
                "description": desc[:300],  # truncate long summaries
            })
            log.debug("Article: %r  (%s)", title, pub_date)

    log.info("Returning %d article(s) for section=%r", len(results), section)
    return results


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/ws/hindu/news")
async def hindu_news_websocket(websocket: WebSocket) -> None:
    client = websocket.client
    client_id = f"{client.host}:{client.port}" if client else "unknown"
    session_start = time.perf_counter()

    log.info("[%s] WebSocket connection opened", client_id)
    await websocket.accept()

    try:
        # Stage 1 — announce
        await _send(websocket, "init", "Connected to The Hindu News agent.")
        log.info("[%s] Stage 1 - announced", client_id)

        # Stage 2 — section
        await _send(
            websocket,
            "prompt_section",
            f"Which section would you like? ({_SECTION_LIST}) — or press Enter for top stories:",
        )
        section_raw = await _recv(websocket)
        section = section_raw.lower().strip() if section_raw else "top"
        if section not in _SECTION_FEEDS:
            log.warning("[%s] Unknown section %r — defaulting to 'top'", client_id, section)
            await _send(websocket, "info", f"Unknown section '{section}', defaulting to 'top stories'.")
            section = "top"
        log.info("[%s] Section: %r", client_id, section)

        # Stage 3 — result limit
        await _send(
            websocket,
            "prompt_limit",
            f"How many articles? (default {DEFAULT_LIMIT}, max {MAX_LIMIT}):",
        )
        limit_raw = await _recv(websocket)
        try:
            limit = max(1, min(MAX_LIMIT, int(limit_raw)))
        except (ValueError, TypeError):
            limit = DEFAULT_LIMIT
        log.info("[%s] Limit: %d", client_id, limit)

        # Stage 4 — fetch
        await _send(websocket, "fetching", f"Fetching top {limit} articles from The Hindu — {section}...")
        log.info("[%s] Stage 4 - fetching", client_id)

        t0 = time.perf_counter()
        try:
            articles = await _fetch_hindu_news(section, limit)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] Fetch error: %s", client_id, exc)
            await _send(websocket, "error", f"Failed to fetch news: {exc}")
            return
        elapsed_ms = (time.perf_counter() - t0) * 1000
        log.info("[%s] Fetch done in %.0f ms — %d article(s)", client_id, elapsed_ms, len(articles))

        # Stage 5 — results
        if articles:
            await _send(
                websocket,
                "results",
                f"Found {len(articles)} article(s) from The Hindu — {section}.",
                articles=articles,
                count=len(articles),
                section=section,
            )
        else:
            await _send(websocket, "no_results", f"No articles found for section '{section}'.")

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
