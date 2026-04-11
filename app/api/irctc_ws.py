from __future__ import annotations

import logging
import re
import time
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from services.irctc_browser_service import irctc_browser_service

router = APIRouter()

REQUEST_TIMEOUT = 30.0
DEFAULT_LIMIT = 5
MAX_LIMIT = 20
PNR_LENGTH = 10

log = logging.getLogger("irctc_ws")

TRAIN_KEYWORDS = {
    "train",
    "between",
    "availability",
    "seat",
    "fare",
    "route",
    "schedule",
}
PNR_KEYWORDS = {"pnr", "status"}


def _format_exception(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return text
    repr_text = repr(exc).strip()
    if repr_text:
        return f"{type(exc).__name__}: {repr_text}"
    return f"{type(exc).__name__}: unspecified error"


def _extract_limit(text: str) -> int:
    match = re.search(r"(?:top|limit)\s*(\d{1,2})", text, flags=re.IGNORECASE)
    if not match:
        return DEFAULT_LIMIT
    value = int(match.group(1))
    return max(1, min(MAX_LIMIT, value))


def _strip_limit_tokens(text: str) -> str:
    cleaned = re.sub(r"\b(?:top|limit)\s*\d{1,2}\b", "", text, flags=re.IGNORECASE)
    return " ".join(cleaned.split())


def _extract_pnr(text: str) -> str | None:
    match = re.search(r"\b(\d{10})\b", text)
    return match.group(1) if match else None


def _detect_intent(query: str) -> str:
    lowered = query.lower()
    tokens = set(re.findall(r"[a-z]+", lowered))

    if re.search(r"\bfrom\b.+\bto\b", lowered):
        return "train_search"
    if "pnr" in lowered or (PNR_KEYWORDS & tokens) == PNR_KEYWORDS:
        return "pnr_status"
    if TRAIN_KEYWORDS & tokens:
        return "train_search"
    return "info_search"


def _extract_journey_details(query: str) -> dict[str, str]:
    details: dict[str, str] = {}

    primary = re.search(
        r"from\s+(?P<from>[a-zA-Z\s]+?)\s+to\s+(?P<to>[a-zA-Z\s]+?)(?:\s+on\s+(?P<date>\d{4}-\d{2}-\d{2}))?\b",
        query,
        flags=re.IGNORECASE,
    )
    if primary:
        details["from"] = " ".join(primary.group("from").split())
        details["to"] = " ".join(primary.group("to").split())
        if primary.group("date"):
            details["date"] = primary.group("date")
        return details

    fallback = re.search(
        r"(?P<from>[a-zA-Z\s]+?)\s+to\s+(?P<to>[a-zA-Z\s]+?)(?:\s+on\s+(?P<date>\d{4}-\d{2}-\d{2}))?\b",
        query,
        flags=re.IGNORECASE,
    )
    if fallback:
        details["from"] = " ".join(fallback.group("from").split())
        details["to"] = " ".join(fallback.group("to").split())
        if fallback.group("date"):
            details["date"] = fallback.group("date")

    return details


def _extract_date(query: str) -> str | None:
    date_match = re.search(r"\b(\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4})\b", query)
    return date_match.group(1) if date_match else None


def _build_search_query(intent: str, query: str, details: dict[str, str], pnr: str | None) -> str:
    base = re.sub(r"\bsite\s*:\s*irctc\.co\.in\b", "", query, flags=re.IGNORECASE).strip()
    base = " ".join(base.split())

    if intent == "train_search" and details.get("from") and details.get("to"):
        date_part = f" on {details['date']}" if details.get("date") else ""
        return (
            "train between stations "
            f"{details['from']} to {details['to']}{date_part} site:irctc.co.in"
        )

    if intent == "pnr_status" and pnr:
        return f"pnr status {pnr} site:irctc.co.in"

    return f"{base or query} site:irctc.co.in"


def _score_url(url: str) -> int:
    lowered = url.lower()
    if "contents.irctc.co.in" in lowered:
        return 3
    if "www.irctc.co.in" in lowered:
        return 2
    if "irctc.co.in" in lowered:
        return 1
    return 0


def _fallback_irctc_results(query: str, intent: str, limit: int) -> list[dict[str, str]]:
    lowered = query.lower()
    catalog: list[dict[str, str]] = [
        {
            "title": "IRCTC Official Portal",
            "snippet": "Book train tickets, check PNR status, and access core passenger services.",
            "url": "https://www.irctc.co.in/",
        },
        {
            "title": "Tatkal Booking Guide",
            "snippet": "Official guide for Tatkal ticket booking process and timings.",
            "url": "http://contents.irctc.co.in/en/TatkalBooking.html",
        },
        {
            "title": "Tatkal FAQ",
            "snippet": "Frequently asked questions for Tatkal booking on IRCTC.",
            "url": "https://contents.irctc.co.in/en/TatkalFaq.html",
        },
        {
            "title": "Book E-Ticket",
            "snippet": "Official instructions and process for booking e-tickets.",
            "url": "https://contents.irctc.co.in/en/bookEticket.html",
        },
        {
            "title": "Cancellation and Refund Rules",
            "snippet": "Official cancellation and refund policy details.",
            "url": "https://contents.irctc.co.in/en/CancellationRulesforIRCTCTrain.pdf",
        },
        {
            "title": "TDR Filing Process",
            "snippet": "How to file TDR and track related claims.",
            "url": "https://contents.irctc.co.in/en/tdr.html",
        },
    ]

    if intent == "pnr_status" or "pnr" in lowered:
        catalog.insert(
            1,
            {
                "title": "IRCTC PNR Status",
                "snippet": "PNR status lookup and related journey updates from official IRCTC services.",
                "url": "https://www.irctc.co.in/nget/train-search",
            },
        )

    if intent == "train_search" or ("from" in lowered and "to" in lowered):
        catalog.insert(
            1,
            {
                "title": "Train Search (IRCTC)",
                "snippet": "Search trains between stations and check availability.",
                "url": "https://www.irctc.co.in/nget/train-search",
            },
        )

    if "tatkal" in lowered:
        catalog.insert(
            1,
            {
                "title": "User Guide: Tatkal Booking (PDF)",
                "snippet": "Detailed Tatkal booking workflow and quota opening timings.",
                "url": "http://contents.irctc.co.in/en/User%20Guide%20Tatkal%20Booking.pdf",
            },
        )

    deduped: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in catalog:
        key = item["url"].lower().rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped[:limit]


async def _send(ws: WebSocket, stage: str, message: str, **extra) -> None:
    payload: dict = {"stage": stage, "message": message}
    payload.update(extra)
    await ws.send_json(payload)


async def _recv(ws: WebSocket) -> str:
    data = await ws.receive_json()
    return str(data.get("data", "") or data.get("message", "")).strip()


async def _fetch_irctc_results(query: str, limit: int) -> list[dict[str, str]]:
    """Query DuckDuckGo HTML with site:irctc.co.in and return up to `limit` results."""
    search_url = (
        "https://html.duckduckgo.com/html/"
        f"?q=site%3Airctc.co.in+{quote_plus(query)}&kl=in-en"
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

    log.info("Querying DuckDuckGo: site:irctc.co.in %s", query)
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

    soup = BeautifulSoup(resp.text, "html.parser")
    results: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for result in soup.select("div.result"):
        if len(results) >= limit:
            break

        anchor = result.select_one("a.result__a") or result.select_one(".result__title a")
        if not anchor:
            continue

        title = anchor.get_text(strip=True)
        raw_href = str(anchor.get("href") or "")

        parsed = urlparse(raw_href)
        qs = parse_qs(parsed.query)
        url = qs.get("uddg", [raw_href])[0]

        if "irctc.co.in" not in url:
            continue

        normalized_url = url.lower().rstrip("/")
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)

        snippet = ""
        snippet_el = result.select_one(".result__snippet")
        if snippet_el:
            snippet = snippet_el.get_text(strip=True)

        if title or url:
            results.append({"title": title, "snippet": snippet, "url": url})

    ranked = sorted(results, key=lambda item: _score_url(item["url"]), reverse=True)
    trimmed = ranked[:limit]
    log.info("Extracted %d IRCTC result(s) from DuckDuckGo (limit=%d)", len(trimmed), limit)
    return trimmed


def _missing_detail_fields(details: dict[str, str]) -> list[str]:
    missing: list[str] = []
    if not details.get("from"):
        missing.append("from_station")
    if not details.get("to"):
        missing.append("to_station")
    if not details.get("date"):
        missing.append("date")
    return missing


@router.websocket("/ws/irctc")
async def irctc_websocket(websocket: WebSocket) -> None:
    client = websocket.client
    client_id = f"{client.host}:{client.port}" if client else "unknown"
    session_start = time.perf_counter()

    log.info("[%s] WebSocket connection opened", client_id)
    await websocket.accept()

    try:
        await _send(
            websocket,
            "init",
            "Connected to IRCTC assistant. Supports browser-live train fare and PNR lookup from IRCTC website.",
        )

        await _send(websocket, "query", "What would you like to search on IRCTC?")
        query_raw = await _recv(websocket)
        if not query_raw:
            await _send(websocket, "error", "Search query cannot be empty.")
            return

        limit = _extract_limit(query_raw)
        query_clean = _strip_limit_tokens(query_raw)
        intent = _detect_intent(query_clean)
        pnr = _extract_pnr(query_clean)
        details = _extract_journey_details(query_clean)
        if "date" not in details:
            extracted_date = _extract_date(query_clean)
            if extracted_date:
                details["date"] = extracted_date

        if intent == "train_search":
            missing = _missing_detail_fields(details)
            if missing:
                await _send(
                    websocket,
                    "needs_details",
                    "For fare/train search, provide from, to, and date. Example: from delhi to mumbai on 2026-05-01",
                    missing_fields=missing,
                )
                follow_up = await _recv(websocket)
                details_update = _extract_journey_details(follow_up)
                date_update = _extract_date(follow_up)
                details.update(details_update)
                if date_update:
                    details["date"] = date_update
                missing = _missing_detail_fields(details)
                if missing:
                    await _send(
                        websocket,
                        "error",
                        "Still missing required train details. Please include from, to, and date.",
                        missing_fields=missing,
                    )
                    return

        if intent == "pnr_status" and not pnr:
            await _send(
                websocket,
                "needs_details",
                "Please provide a 10-digit PNR number.",
                missing_fields=["pnr"],
            )
            pnr = _extract_pnr(await _recv(websocket))
            if not pnr:
                await _send(
                    websocket,
                    "error",
                    f"Invalid PNR. Please send exactly {PNR_LENGTH} digits.",
                    missing_fields=["pnr"],
                )
                return

        extra: dict[str, object] = {"intent": intent}
        if details:
            extra["journey_details"] = details
        if pnr:
            extra["pnr"] = pnr

        # Browser-live mode for operational intents (train search / pnr status)
        if intent in {"train_search", "pnr_status"}:
            try:
                if intent == "train_search":
                    await _send(
                        websocket,
                        "searching_live",
                        (
                            "Fetching live train and fare data from IRCTC website for "
                            f"{details['from']} to {details['to']} on {details['date']}..."
                        ),
                        **extra,
                    )

                    trains = await irctc_browser_service.search_trains_with_fare(
                        from_station=details["from"],
                        to_station=details["to"],
                        date_of_journey=details["date"],
                        travel_class=None,
                        limit=limit,
                    )

                    if trains:
                        await _send(
                            websocket,
                            "results",
                            (
                                f"Found {len(trains)} live train option(s) "
                                f"from {details['from']} to {details['to']} on {details['date']}."
                            ),
                            results=trains,
                            count=len(trains),
                            source="irctc_browser_live",
                            **extra,
                        )
                        total_s = time.perf_counter() - session_start
                        log.info("[%s] Session complete in %.1f s (browser train)", client_id, total_s)
                        return

                    await _send(
                        websocket,
                        "live_no_results",
                        "Could not parse live train cards from IRCTC page.",
                        **extra,
                    )
                    return

                if intent == "pnr_status":
                    await _send(
                        websocket,
                        "searching_live",
                        f"Fetching live PNR status from IRCTC website for {pnr}...",
                        **extra,
                    )
                    pnr_result = await irctc_browser_service.get_pnr_status(pnr or "")
                    await _send(
                        websocket,
                        "results",
                        f"Fetched live PNR status for {pnr}.",
                        result=pnr_result,
                        source="irctc_browser_live",
                        **extra,
                    )
                    total_s = time.perf_counter() - session_start
                    log.info("[%s] Session complete in %.1f s (browser pnr)", client_id, total_s)
                    return
            except Exception as exc:  # noqa: BLE001
                log.exception("[%s] Browser live fetch failed: %s", client_id, exc)
                error_text = _format_exception(exc)
                if intent == "pnr_status":
                    try:
                        await _send(
                            websocket,
                            "searching_public_web",
                            "Direct IRCTC fetch failed. Trying public railway web sources for PNR...",
                            **extra,
                        )
                        public_pnr = await irctc_browser_service.get_pnr_status_from_public_web(pnr or "")
                        await _send(
                            websocket,
                            "results",
                            f"Fetched PNR details from public railway web sources for {pnr}.",
                            result=public_pnr,
                            source="public_web_fallback",
                            **extra,
                        )
                        return
                    except Exception as public_exc:  # noqa: BLE001
                        error_text = f"{error_text}; public fallback failed: {_format_exception(public_exc)}"

                if intent == "train_search":
                    try:
                        await _send(
                            websocket,
                            "searching_public_web",
                            "Direct IRCTC fetch failed. Trying public railway web sources for fare/train data...",
                            **extra,
                        )
                        public_trains = await irctc_browser_service.search_trains_with_fare_public(
                            from_station=details.get("from", ""),
                            to_station=details.get("to", ""),
                            date_of_journey=details.get("date", ""),
                            limit=limit,
                        )
                        if public_trains:
                            await _send(
                                websocket,
                                "results",
                                "Fetched train/fare details from public railway web sources.",
                                results=public_trains,
                                count=len(public_trains),
                                source="public_web_fallback",
                                **extra,
                            )
                            return
                        error_text = f"{error_text}; public fallback returned no parsed train results"
                    except Exception as public_exc:  # noqa: BLE001
                        error_text = f"{error_text}; public fallback failed: {_format_exception(public_exc)}"

                await _send(
                    websocket,
                    "live_error",
                    "Could not fetch live IRCTC website data in headless mode.",
                    error=error_text,
                    **extra,
                )
                return

        search_query = _build_search_query(intent, query_clean, details, pnr)

        await _send(
            websocket,
            "searching",
            f"Searching IRCTC for '{search_query}', fetching top {limit} results...",
        )

        try:
            results = await _fetch_irctc_results(search_query, limit)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] Fetch error: %s", client_id, exc)
            await _send(websocket, "error", f"Failed to fetch IRCTC results: {exc}")
            return

        if not results:
            fallback = _fallback_irctc_results(search_query, intent, limit)
            if fallback:
                await _send(
                    websocket,
                    "results",
                    "Search provider returned no parseable results; returning official IRCTC fallback links.",
                    results=fallback,
                    count=len(fallback),
                    source="fallback_catalog",
                    **extra,
                )
                total_s = time.perf_counter() - session_start
                log.info("[%s] Session complete in %.1f s (fallback)", client_id, total_s)
                return

        if results:
            await _send(
                websocket,
                "results",
                f"Found {len(results)} IRCTC result(s) for '{search_query}'.",
                results=results,
                count=len(results),
                **extra,
            )
        else:
            await _send(
                websocket,
                "no_results",
                f"No IRCTC results found for '{search_query}'.",
                **extra,
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