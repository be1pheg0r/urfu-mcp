"""Authenticated HTTP page source for the local SfeduSchedule person route."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from collections.abc import Mapping
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from .errors import (Forbidden, InvalidUpstreamResponse, NotAuthenticated,
                     RateLimited, UpstreamUnavailable)


class SfeduPersonPageSource:
    """Adapt the protected sidecar person-search route to ``PersonPageSource``."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if not api_key or any(character.isspace() for character in api_key):
            raise ValueError("a valid sidecar API key is required")
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
        self._api_key = api_key
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_page(
        self,
        query: str,
        page_number: int,
        page_size: int,
        *,
        identity: str,
        token: str,
    ) -> Mapping[str, Any]:
        """Fetch one bounded page with per-call user token and sidecar API key."""
        if not query.strip():
            raise ValueError("person query must not be empty")
        if not identity.strip():
            raise ValueError("authenticated identity is required")
        if not token or any(character.isspace() for character in token):
            raise ValueError("a valid per-call Modeus token is required")
        if type(page_number) is not int or page_number < 0:
            raise ValueError("page_number must be a non-negative integer")
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")

        payload = {"fullName": query.strip(), "page": page_number, "size": page_size}
        try:
            async with asyncio.timeout(self._timeout):
                async with self._client.stream(
                    "POST",
                    f"{self._base_url}/api/proxy/people/search",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "X-Api-Key": self._api_key,
                    },
                    timeout=self._timeout,
                    follow_redirects=False,
                ) as response:
                    self._raise_for_status(response)
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > self._max_response_bytes:
                            raise InvalidUpstreamResponse(
                                "SfeduSchedule person response exceeded size limit"
                            )
                        data.extend(chunk)
        except (httpx.TimeoutException, TimeoutError):
            raise UpstreamUnavailable("SfeduSchedule person search timed out") from None
        except httpx.RequestError:
            raise UpstreamUnavailable("SfeduSchedule person search request failed") from None

        try:
            decoded = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise InvalidUpstreamResponse(
                "SfeduSchedule returned invalid person-search JSON"
            ) from None
        if not isinstance(decoded, dict):
            raise InvalidUpstreamResponse(
                "SfeduSchedule returned an invalid person-search response"
            )
        items = decoded.get("items")
        page = decoded.get("page")
        if (
            not isinstance(items, list)
            or not isinstance(page, dict)
            or any(
                type(page.get(key)) is not int or page[key] < 0
                for key in ("number", "totalPages", "totalElements")
            )
        ):
            raise InvalidUpstreamResponse(
                "SfeduSchedule returned an invalid person-search page"
            )
        return decoded

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 401:
            raise NotAuthenticated("SfeduSchedule person search authentication failed")
        if response.status_code == 403:
            raise Forbidden("SfeduSchedule person search was denied")
        if response.status_code == 429:
            raise RateLimited(response.headers.get("Retry-After"))
        if 500 <= response.status_code < 600:
            raise UpstreamUnavailable("SfeduSchedule person search is unavailable")
        if response.status_code >= 300:
            raise InvalidUpstreamResponse(
                "SfeduSchedule person search returned an unexpected status"
            )


__all__ = ["SfeduPersonPageSource"]
