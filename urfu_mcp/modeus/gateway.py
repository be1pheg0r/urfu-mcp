from __future__ import annotations

import asyncio
import ipaddress
import json
from datetime import datetime
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from .errors import (Forbidden, InvalidUpstreamResponse, NotAuthenticated,
                     RateLimited, UpstreamUnavailable)


class SfeduGateway:
    """Async, per-request-token HTTP boundary to a local SfeduSchedule service."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        max_response_bytes: int = 2_000_000,
        size: int = 1000,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if size <= 0:
            raise ValueError("size must be positive")
        parsed_url = urlsplit(base_url)
        try:
            address = ipaddress.ip_address(parsed_url.hostname or "")
            is_loopback = address.is_loopback
        except ValueError:
            is_loopback = parsed_url.hostname == "localhost"
        if (
            parsed_url.scheme not in {"http", "https"}
            or not is_loopback
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("SfeduSchedule base_url must be a loopback HTTP(S) URL")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._size = size
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_events(
        self,
        person_id: str,
        time_min: datetime,
        time_max: datetime,
        *,
        token: str,
    ) -> dict[str, Any]:
        """Fetch a person's raw event search response; never shares token state."""
        if not person_id:
            raise ValueError("person_id must not be empty")
        if not token:
            raise ValueError("token must not be empty")
        if time_min.tzinfo is None or time_min.utcoffset() is None:
            raise ValueError("time_min must be timezone-aware")
        if time_max.tzinfo is None or time_max.utcoffset() is None:
            raise ValueError("time_max must be timezone-aware")
        if time_max <= time_min:
            raise ValueError("time_max must be later than time_min")

        payload = {
            "size": self._size,
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "attendeePersonId": [person_id],
        }
        try:
            async with asyncio.timeout(self._timeout):
                async with self._client.stream(
                    "POST",
                    f"{self._base_url}/api/proxy/events/search",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=self._timeout,
                ) as response:
                    self._raise_for_status(response)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > self._max_response_bytes:
                            raise InvalidUpstreamResponse(
                                "SfeduSchedule response exceeded size limit"
                            )
                        data.extend(chunk)
        except (httpx.TimeoutException, TimeoutError):
            raise UpstreamUnavailable("SfeduSchedule request timed out") from None
        except httpx.RequestError:
            raise UpstreamUnavailable("SfeduSchedule request failed") from None

        try:
            decoded = json.loads(data)
            if isinstance(decoded, str):
                decoded = json.loads(decoded)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise InvalidUpstreamResponse(
                "SfeduSchedule returned invalid JSON"
            ) from None
        if not isinstance(decoded, dict):
            raise InvalidUpstreamResponse("SfeduSchedule returned an invalid response")
        return decoded

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 401:
            raise NotAuthenticated("SfeduSchedule authentication failed")
        if response.status_code == 403:
            raise Forbidden("SfeduSchedule access was denied")
        if response.status_code == 429:
            raise RateLimited(response.headers.get("Retry-After"))
        if 500 <= response.status_code < 600:
            raise UpstreamUnavailable("SfeduSchedule is unavailable")
        if response.status_code >= 300:
            raise InvalidUpstreamResponse("SfeduSchedule returned an unexpected status")
