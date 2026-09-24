"""Unauthenticated read-only transport for the verified public iStudent BRS tile.

Not a protected BRS source: session cookies and page schema have not been verified.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

BRS_TILE_URL = "https://istudent.urfu.ru/s/http-urfu-ru-ru-students-study-brs"


class HTMLTransportError(ValueError):
    """A fixed public failure without URLs, response content, or credentials."""


class IStudentHTMLTransport:
    """Fetch only the official BRS tile without cross-origin redirects or credentials."""

    def __init__(
        self,
        *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        timeout_seconds: float = 10.0,
        max_response_bytes: int = 2_000_000,
    ) -> None:
        if not 0 < timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be between 0 and 30")
        if not 1 <= max_response_bytes <= 10_000_000:
            raise ValueError("max_response_bytes must be between 1 and 10000000")
        self._transport_factory = transport_factory or httpx.AsyncHTTPTransport
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes

    async def fetch_tile(self) -> str:
        """Get bounded public HTML; a redirect is an auth gate, not score data."""
        try:
            async with httpx.AsyncClient(
                transport=self._transport_factory(),
                timeout=self._timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": "Mozilla/5.0 (compatible; urfu-mcp)", "Accept": "text/html"},
            ) as client, client.stream("GET", BRS_TILE_URL) as response:
                if response.is_redirect:
                    raise HTMLTransportError("iStudent authentication required")
                if response.status_code != 200:
                    raise HTMLTransportError("iStudent HTML request failed")
                media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
                if media_type not in {"text/html", "application/xhtml+xml"}:
                    raise HTMLTransportError("iStudent HTML response has an unsupported content type")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > self._max_response_bytes:
                        raise HTMLTransportError("iStudent HTML response exceeded the size limit")
                    body.extend(chunk)
                try:
                    return body.decode(response.encoding or "utf-8")
                except (LookupError, UnicodeDecodeError):
                    raise HTMLTransportError("iStudent HTML response encoding is unsupported") from None
        except httpx.HTTPError:
            raise HTMLTransportError("iStudent HTML request failed") from None


__all__ = ["BRS_TILE_URL", "HTMLTransportError", "IStudentHTMLTransport"]
