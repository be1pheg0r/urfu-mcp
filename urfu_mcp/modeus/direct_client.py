"""Bounded direct HTTP clients for the currently exposed Modeus schedule APIs."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime
from math import ceil
from typing import Any

import httpx

from .errors import (
    Forbidden,
    InvalidUpstreamResponse,
    NotAuthenticated,
    RateLimited,
    UpstreamUnavailable,
)


class ModeusDirectClient:
    """Call fixed Modeus routes with per-call Bearer tokens; never act as a proxy."""

    def __init__(self, base_url: str = "https://urfu.modeus.org", *, timezone_name: str = "Asia/Yekaterinburg",
                 client: httpx.AsyncClient | None = None, timeout: float = 10.0,
                 max_response_bytes: int = 2_000_000, event_page_size: int = 1000) -> None:
        if base_url.rstrip("/") != "https://urfu.modeus.org":
            raise ValueError("Modeus base_url must be https://urfu.modeus.org")
        if timeout <= 0 or max_response_bytes <= 0 or not 1 <= event_page_size <= 5000:
            raise ValueError("invalid Modeus HTTP limits")
        self._base_url = base_url.rstrip("/")
        self._timezone = timezone_name
        self._timeout = timeout
        self._limit = max_response_bytes
        self._event_size = event_page_size
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_events(self, person_id: str, time_min: datetime, time_max: datetime, *, token: str) -> dict[str, Any]:
        if not person_id or not token or time_min.tzinfo is None or time_max.tzinfo is None or time_max <= time_min:
            raise ValueError("valid person, token, and aware increasing interval are required")
        response = await self._post("/schedule-calendar-v2/api/calendar/events/search", {
            "size": self._event_size, "timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(),
            "attendeePersonId": [person_id],
        }, token=token, params={"tz": self._timezone})
        if not isinstance(response, dict):
            raise InvalidUpstreamResponse("Modeus returned an invalid event response")
        return response

    async def fetch_page(self, query: str, page_number: int, page_size: int, *, identity: str, token: str) -> Mapping[str, Any]:
        if not query.strip() or not identity.strip() or not token:
            raise ValueError("query, authenticated identity, and token are required")
        if type(page_number) is not int or page_number < 0 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("invalid person page bounds")
        response = await self._post("/schedule-calendar-v2/api/people/persons/search", {
            "fullName": query.strip(), "sort": "+fullName", "size": page_size, "page": page_number,
        }, token=token)
        embedded = response.get("_embedded") if isinstance(response, dict) else None
        people = embedded.get("persons") if isinstance(embedded, dict) else None
        page = response.get("page") if isinstance(response, dict) else None
        if not isinstance(people, list) or not isinstance(page, dict):
            raise InvalidUpstreamResponse("Modeus returned an invalid person-search response")
        number, total_pages, total_elements = (page.get(k) for k in ("number", "totalPages", "totalElements"))
        size = page.get("size")
        if any(type(x) is not int or x < 0 for x in (number, total_pages, total_elements)) or size != page_size or number != page_number:
            raise InvalidUpstreamResponse("Modeus returned invalid person page metadata")
        expected = ceil(total_elements / page_size) if total_elements else 0
        if total_pages != expected or len(people) > page_size or (total_pages == 0 and (number != 0 or people)) or (total_pages and number >= total_pages):
            raise InvalidUpstreamResponse("Modeus person page totals are inconsistent")
        items = []
        for person in people:
            if not isinstance(person, dict) or not isinstance(person.get("id"), str) or not person["id"] or not isinstance(person.get("fullName"), str) or not person["fullName"].strip():
                raise InvalidUpstreamResponse("Modeus returned an invalid person")
            items.append({"person_id": person["id"], "full_name": person["fullName"]})
        return {"items": items, "page": {"number": number, "totalPages": total_pages, "totalElements": total_elements}}

    async def _post(self, route: str, payload: dict[str, Any], *, token: str, params: dict[str, str] | None = None) -> Any:
        if not token or any(ch.isspace() for ch in token):
            raise ValueError("a valid per-call Modeus token is required")
        try:
            async with asyncio.timeout(self._timeout):
                async with self._client.stream("POST", self._base_url + route, json=payload,
                                               headers={"Authorization": f"Bearer {token}"}, params=params,
                                               timeout=self._timeout, follow_redirects=False) as response:
                    if response.status_code == 401:
                        raise NotAuthenticated("Modeus authentication failed")
                    if response.status_code == 403:
                        raise Forbidden("Modeus access was denied")
                    if response.status_code == 429:
                        raise RateLimited(response.headers.get("Retry-After"))
                    if response.status_code >= 500:
                        raise UpstreamUnavailable("Modeus is unavailable")
                    if response.status_code >= 300:
                        raise InvalidUpstreamResponse("Modeus returned an unexpected status")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > self._limit:
                            raise InvalidUpstreamResponse("Modeus response exceeded size limit")
                        data.extend(chunk)
        except (httpx.TimeoutException, TimeoutError):
            raise UpstreamUnavailable("Modeus request timed out") from None
        except httpx.RequestError:
            raise UpstreamUnavailable("Modeus request failed") from None
        try:
            decoded = json.loads(data)
            if isinstance(decoded, str):
                decoded = json.loads(decoded)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise InvalidUpstreamResponse("Modeus returned invalid JSON") from None
        return decoded


__all__ = ["ModeusDirectClient"]
