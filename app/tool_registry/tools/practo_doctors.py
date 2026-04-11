from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import requests

from tool_registry.registry import register_tool

DEFAULT_LIMIT = 5
MAX_LIMIT = 20
REQUEST_TIMEOUT_S = 25
WORD_PATTERN = re.compile(r"[a-z0-9]+")


def _format_error(exc: Exception) -> str:
    text = str(exc).strip()
    if text:
        return text
    return f"{type(exc).__name__}: unspecified error"


tool_definition = {
    "name": "practo_doctors",
    "description": "Find doctors on Practo by city, speciality, and locality without API keys",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "City to search in (for example: Bengaluru, Mumbai)",
            },
            "speciality": {
                "type": "string",
                "description": "Doctor speciality (for example: dentist, dermatologist)",
            },
            "locality": {
                "type": "string",
                "description": "Optional locality (for example: HSR Layout, Indiranagar)",
            },
            "query": {
                "type": "string",
                "description": "Optional free text query to rank doctor results",
            },
            "limit": {
                "type": "number",
                "description": "Maximum doctors to return",
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


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _city_slug(city: str) -> str:
    normalized = city.strip().lower()
    aliases = {
        "bengaluru": "bangalore",
        "bangalore": "bangalore",
        "new delhi": "delhi",
        "trivandrum": "thiruvananthapuram",
        "thiruvananthapuram": "thiruvananthapuram",
    }
    return aliases.get(normalized, _slugify(city))


def _query_terms(parts: list[str]) -> list[str]:
    terms: list[str] = []
    for part in parts:
        for token in WORD_PATTERN.findall(part.lower()):
            if len(token) < 3:
                continue
            terms.append(token)
    return list(dict.fromkeys(terms))


def _match_score(text: str, terms: list[str]) -> int:
    if not terms:
        return 1
    tokens = set(WORD_PATTERN.findall(text.lower()))
    return sum(1 for term in terms if term in tokens)


def _is_security_or_challenge(title: str, content: str) -> bool:
    title_lower = title.strip().lower()
    if any(marker in title_lower for marker in ("challenge", "access denied", "security", "forbidden")):
        return True

    head = content[:60000].lower()
    markers = (
        "you have been blocked",
        "captcha",
        "challenge validation",
        "attention required",
        "cf-error-code",
    )
    return any(marker in head for marker in markers)


def _resolve_practo_url(href: str) -> str:
    if not href:
        return ""
    return urljoin("https://www.practo.com", href)


def _canonical_profile_key(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.lower().rstrip("/")
    if path.endswith("/recommended"):
        path = path[: -len("/recommended")]
    return path


def _collect_practo_doctors(html: str, limit: int, terms: list[str]) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("div.listing-doctor-card[data-qa-id='doctor_card'], div.listing-doctor-card")
    seen: set[str] = set()
    ranked: list[tuple[int, dict[str, str]]] = []

    for card in cards:
        name_node = card.select_one("[data-qa-id='doctor_name']")
        if not name_node:
            continue

        name = " ".join(name_node.get_text(" ", strip=True).split())
        if not name:
            continue

        profile_url = ""
        for anchor in card.select("a[href*='/doctor/']"):
            href = str(anchor.get("href") or "")
            if not href or "/recommended" in href:
                continue
            profile_url = _resolve_practo_url(href)
            break

        if not profile_url:
            continue

        key = _canonical_profile_key(profile_url)
        if not key or key in seen:
            continue
        seen.add(key)

        speciality_node = card.select_one("div.u-grey_3-text div.u-d-flex span")
        experience_node = card.select_one("[data-qa-id='doctor_experience']")
        locality_node = card.select_one("[data-qa-id='practice_locality']")
        city_node = card.select_one("[data-qa-id='practice_city']")
        clinic_node = card.select_one("[data-qa-id='doctor_clinic_name']")
        fee_node = card.select_one("[data-qa-id='consultation_fee']")
        recommendation_node = card.select_one("[data-qa-id='doctor_recommendation']")
        stories_node = card.select_one("[data-qa-id='total_feedback']")

        speciality = " ".join((speciality_node.get_text(" ", strip=True) if speciality_node else "").split())
        experience = " ".join((experience_node.get_text(" ", strip=True) if experience_node else "").split())
        locality = " ".join((locality_node.get_text(" ", strip=True) if locality_node else "").split()).rstrip(",")
        city = " ".join((city_node.get_text(" ", strip=True) if city_node else "").split())
        clinic = " ".join((clinic_node.get_text(" ", strip=True) if clinic_node else "").split())
        fee = " ".join((fee_node.get_text(" ", strip=True) if fee_node else "").split())
        recommendation = " ".join(
            (recommendation_node.get_text(" ", strip=True) if recommendation_node else "").split()
        )
        patient_stories = " ".join((stories_node.get_text(" ", strip=True) if stories_node else "").split())

        if recommendation and not recommendation.endswith("%"):
            recommendation = f"{recommendation}%"

        location = ", ".join([part for part in [locality, city] if part])
        combined_text = " ".join([name, speciality, experience, location, clinic])
        score = _match_score(combined_text, terms)

        ranked.append(
            (
                score,
                {
                    "title": name,
                    "speciality": speciality,
                    "experience": experience,
                    "location": location,
                    "clinic": clinic,
                    "fee": fee,
                    "recommendation": recommendation,
                    "patient_stories": patient_stories,
                    "url": profile_url,
                    "snippet": f"{speciality} | {experience} | {fee}".strip(" |"),
                },
            )
        )

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in ranked[:limit]]


def _fallback_practo_links(city: str, speciality: str, locality: str, limit: int) -> list[dict[str, str]]:
    city_slug = _city_slug(city)
    speciality_slug = _slugify(speciality) if speciality else ""
    locality_slug = _slugify(locality) if locality else ""

    links: list[dict[str, str]] = [
        {
            "title": f"Doctors in {city} on Practo",
            "speciality": speciality,
            "experience": "",
            "location": city,
            "clinic": "",
            "fee": "",
            "recommendation": "",
            "patient_stories": "",
            "url": f"https://www.practo.com/{city_slug}/doctors",
            "snippet": f"Browse doctors in {city}",
        }
    ]

    if speciality_slug:
        links.append(
            {
                "title": f"{speciality.title()} in {city}",
                "speciality": speciality,
                "experience": "",
                "location": city,
                "clinic": "",
                "fee": "",
                "recommendation": "",
                "patient_stories": "",
                "url": f"https://www.practo.com/{city_slug}/{speciality_slug}",
                "snippet": f"Browse {speciality} doctors in {city}",
            }
        )

    if speciality_slug and locality_slug:
        links.append(
            {
                "title": f"{speciality.title()} in {locality}, {city}",
                "speciality": speciality,
                "experience": "",
                "location": f"{locality}, {city}",
                "clinic": "",
                "fee": "",
                "recommendation": "",
                "patient_stories": "",
                "url": f"https://www.practo.com/{city_slug}/{speciality_slug}/{locality_slug}",
                "snippet": f"Browse {speciality} near {locality}",
            }
        )

    return links[:limit]


async def _search_practo(city: str, speciality: str, locality: str, query: str, limit: int) -> list[dict[str, str]]:
    city_slug = _city_slug(city)
    speciality_slug = _slugify(speciality) if speciality else ""
    locality_slug = _slugify(locality) if locality else ""

    candidates = [f"https://www.practo.com/{city_slug}/doctors"]
    if speciality_slug:
        candidates.insert(0, f"https://www.practo.com/{city_slug}/{speciality_slug}")
    if speciality_slug and locality_slug:
        candidates.insert(0, f"https://www.practo.com/{city_slug}/{speciality_slug}/{locality_slug}")

    terms = _query_terms([speciality, locality, query])
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    for url in candidates:
        response = await asyncio.to_thread(
            requests.get,
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT_S,
            allow_redirects=True,
        )
        if response.status_code != 200:
            continue

        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if _is_security_or_challenge(title, html):
            continue

        results = _collect_practo_doctors(html, limit, terms)
        if results:
            return results

    return []


async def run(params: dict[str, Any]) -> dict[str, Any]:
    city = str(params.get("city", "")).strip()
    if not city:
        return {
            "success": False,
            "error": "'city' is required",
            "results": [],
        }

    speciality = str(params.get("speciality", "")).strip()
    locality = str(params.get("locality", "")).strip()
    query = str(params.get("query", "")).strip()
    limit = _normalize_limit(params.get("limit"))

    try:
        results = await _search_practo(city, speciality, locality, query, limit)
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "city": city,
            "speciality": speciality,
            "locality": locality,
            "query": query,
            "limit": limit,
            "results": [],
            "error": _format_error(exc),
        }

    if not results:
        fallback = _fallback_practo_links(city, speciality, locality, limit)
        return {
            "success": True,
            "city": city,
            "speciality": speciality,
            "locality": locality,
            "query": query,
            "limit": limit,
            "source": "practo_fallback_catalog",
            "count": len(fallback),
            "results": fallback,
            "note": "Direct Practo extraction failed in this session; returning Practo direct links.",
        }

    return {
        "success": True,
        "city": city,
        "speciality": speciality,
        "locality": locality,
        "query": query,
        "limit": limit,
        "source": "practo_direct",
        "count": len(results),
        "results": results,
    }


register_tool(tool_definition, run)
