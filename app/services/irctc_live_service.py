from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from core.config import (
    IRCTC_API_BASE_URL,
    IRCTC_PNR_PATH,
    IRCTC_RAPIDAPI_HOST,
    IRCTC_RAPIDAPI_KEY,
    IRCTC_STATION_SEARCH_PATH,
    IRCTC_TRAIN_BETWEEN_PATH,
)

REQUEST_TIMEOUT = 25.0


class IRCTCLiveService:
    def __init__(self) -> None:
        self.enabled = bool(IRCTC_RAPIDAPI_KEY)

    def is_enabled(self) -> bool:
        return self.enabled

    def _headers(self) -> dict[str, str]:
        return {
            "x-rapidapi-key": IRCTC_RAPIDAPI_KEY or "",
            "x-rapidapi-host": IRCTC_RAPIDAPI_HOST,
        }

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("IRCTC live API is not configured")

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(
                f"{IRCTC_API_BASE_URL.rstrip('/')}/{path.lstrip('/')}",
                params=params,
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    @staticmethod
    def normalize_date(value: str) -> str:
        value = value.strip()
        if re.match(r"^\d{2}-\d{2}-\d{4}$", value):
            return value
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            dt = datetime.strptime(value, "%Y-%m-%d")
            return dt.strftime("%d-%m-%Y")
        raise ValueError("Date must be YYYY-MM-DD or DD-MM-YYYY")

    async def search_station(self, query: str) -> list[dict[str, str]]:
        payload = await self._get(IRCTC_STATION_SEARCH_PATH, {"query": query})
        data = payload.get("data") or payload.get("stations") or []

        stations: list[dict[str, str]] = []
        for item in data:
            code = (
                item.get("station_code")
                or item.get("stationCode")
                or item.get("code")
                or ""
            )
            name = item.get("station_name") or item.get("stationName") or item.get("name") or ""
            code = str(code).strip().upper()
            name = str(name).strip()
            if code:
                stations.append({"code": code, "name": name})
        return stations

    async def resolve_station_code(self, text: str) -> str:
        text = text.strip()
        if re.fullmatch(r"[A-Za-z]{2,5}", text):
            return text.upper()

        stations = await self.search_station(text)
        if not stations:
            raise RuntimeError(f"Could not resolve station code for '{text}'")
        return stations[0]["code"]

    async def train_between(self, from_input: str, to_input: str, date_value: str) -> dict[str, Any]:
        from_code = await self.resolve_station_code(from_input)
        to_code = await self.resolve_station_code(to_input)
        date_of_journey = self.normalize_date(date_value)

        payload = await self._get(
            IRCTC_TRAIN_BETWEEN_PATH,
            {
                "fromStationCode": from_code,
                "toStationCode": to_code,
                "dateOfJourney": date_of_journey,
            },
        )

        rows = payload.get("data") or payload.get("trains") or []
        mapped: list[dict[str, Any]] = []
        for row in rows:
            mapped.append(
                {
                    "train_number": row.get("train_number") or row.get("trainNumber") or "",
                    "train_name": row.get("train_name") or row.get("trainName") or "",
                    "from": row.get("from_station_name") or row.get("fromStationName") or from_code,
                    "to": row.get("to_station_name") or row.get("toStationName") or to_code,
                    "departure": row.get("from_std") or row.get("departure_time") or row.get("depTime") or "",
                    "arrival": row.get("to_sta") or row.get("arrival_time") or row.get("arrTime") or "",
                    "duration": row.get("duration") or "",
                }
            )

        return {
            "from_code": from_code,
            "to_code": to_code,
            "date": date_of_journey,
            "results": mapped,
            "raw": payload,
        }

    async def pnr_status(self, pnr: str) -> dict[str, Any]:
        payload = await self._get(IRCTC_PNR_PATH, {"pnrNumber": pnr})
        data = payload.get("data") or payload

        return {
            "pnr": pnr,
            "train_number": data.get("train_number") or data.get("trainNumber") or "",
            "train_name": data.get("train_name") or data.get("trainName") or "",
            "from": data.get("source_station_name") or data.get("sourceStationName") or "",
            "to": data.get("destination_station_name") or data.get("destinationStationName") or "",
            "journey_date": data.get("date_of_journey") or data.get("dateOfJourney") or "",
            "booking_status": data.get("booking_status") or data.get("bookingStatus") or "",
            "current_status": data.get("current_status") or data.get("currentStatus") or "",
            "passengers": data.get("passenger_status") or data.get("passengerStatus") or [],
            "raw": payload,
        }


irctc_live_service = IRCTCLiveService()
