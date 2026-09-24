"""Verified iStudent JWT claims from the fixed official Keycloak realm."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from joserfc import jwk, jwt
from joserfc.errors import JoseError

from .brs_parser import _HTMLTree

ISSUER = "https://keys.urfu.ru/auth/realms/urfu-lk"
JWKS_URL = f"{ISSUER}/protocol/openid-connect/certs"
MAX_JWKS_BYTES = 1_000_000
ISTUDENT_HOST = "istudent.urfu.ru"
ISTUDENT_PATH = "/s/http-urfu-ru-ru-students-study-brs"


def _looks_like_protected_brs(html: str) -> bool:
    """Check real HTML elements, not class attributes with exact whitespace."""
    tree = _HTMLTree()
    try:
        tree.feed(html)
    except ValueError:
        return False
    nodes = tree.root.descendants()
    return (
        any(node.tag == "html" for node in nodes)
        and any(node.tag == "select" and node.attrs.get("id") == "year-select" for node in nodes)
        and any(node.tag == "select" and node.attrs.get("id") == "semester-select" for node in nodes)
        and any("disciplines-list-header" in node.classes() for node in nodes)
        and any(
            "discipline-outer-container" in descendant.classes()
            for article in nodes if article.tag == "article"
            for descendant in article.descendants()
        )
    )


def _valid_session_url(url: httpx.URL) -> bool:
    """Allow requests only to the exact secure iStudent origin and BRS path."""
    return (
        url.scheme == "https"
        and url.host == ISTUDENT_HOST
        and url.port in (None, 443)
        and url.path == ISTUDENT_PATH
        and not url.username
        and not url.password
    )


class _RestrictedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport | None) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not _valid_session_url(request.url):
            raise ValueError("iStudent session requests must use the protected HTTPS origin")
        if self._transport is None:
            raise httpx.ConnectError("No iStudent transport configured", request=request)
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        if self._transport is not None:
            await self._transport.aclose()


class IStudentTokenError(ValueError):
    """Safe failure validating the dedicated iStudent token."""


def validate_istudent_token(token: str, *, now: float, transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Check RS256 signature, exact issuer, iStudent authorized party and expiry."""
    if not isinstance(token, str) or len(token) > 65_536 or token.count(".") != 2:
        raise IStudentTokenError("iStudent token is malformed")
    try:
        with httpx.Client(transport=transport, timeout=15, follow_redirects=False) as client:
            response = client.get(JWKS_URL)
            response.raise_for_status()
            if len(response.content) > MAX_JWKS_BYTES:
                raise ValueError
            document = response.json()
        keys = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(keys, list) or not keys or any(not isinstance(key, dict) for key in keys):
            raise ValueError
        if any({"d", "p", "q", "dp", "dq", "qi", "oth"}.intersection(key) for key in keys):
            raise ValueError
        key_set = jwk.KeySet.import_key_set(cast(Any, {"keys": keys}))
        decoded = jwt.decode(token, key_set, algorithms=["RS256"])
        claims = decoded.claims
        if not isinstance(claims, dict):
            raise TypeError
        if claims.get("iss") != ISSUER or claims.get("azp") != "istudent":
            raise ValueError
        exp, issued = claims.get("exp"), claims.get("iat")
        if type(exp) not in (int, float) or type(issued) not in (int, float):
            raise ValueError
        if cast(float, exp) <= now or cast(float, issued) > now + 60:
            raise ValueError
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise ValueError
        return dict(claims)
    except (httpx.HTTPError, JoseError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise IStudentTokenError("iStudent token signature or required claims are invalid") from None


def is_exact_istudent_url(value: str) -> bool:
    """Accept only the fixed iStudent HTTPS host and the BRS overview path."""
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https" and parsed.hostname == "istudent.urfu.ru"
            and parsed.port in (None, 443) and parsed.username is None
            and parsed.password is None and parsed.path == "/s/http-urfu-ru-ru-students-study-brs"
            and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False


class StoredIStudentSessionProvider:
    """Create identity-bound, cookie-only clients from secure stored sessions.

    Clients are owned by this provider; close them with ``aclose`` after use.
    """

    def __init__(
        self,
        store: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        jwks_transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._transport = transport
        self._jwks_transport = jwks_transport
        self._now = now
        self._clients: list[httpx.AsyncClient] = []

    async def get_session(self, identity: str) -> httpx.AsyncClient:
        from .session_store import IStudentSessionStoreError

        try:
            record = self._store.load(identity)
            if record is None:
                raise IStudentSessionStoreError("Stored iStudent session is unavailable")
            claims = validate_istudent_token(
                record.access_token, now=self._now(), transport=self._jwks_transport,
            )
            if claims.get("sub") != record.istudent_subject:
                raise IStudentTokenError("iStudent token subject does not match its session")
            cookies = httpx.Cookies()
            cookies.set("PHPSESSID", record.php_session_id, domain=ISTUDENT_HOST, path="/")
            cookies.set("keycloakAccessToken", record.access_token, domain=ISTUDENT_HOST, path="/")
            client = httpx.AsyncClient(
                base_url="https://istudent.urfu.ru",
                cookies=cookies,
                follow_redirects=False,
                timeout=15,
                transport=_RestrictedTransport(self._transport),
            )
            try:
                response = await client.get(ISTUDENT_PATH)
            except BaseException:
                await client.aclose()
                raise
            body = response.content
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if (
                response.status_code != 200 or content_type != "text/html" or len(body) > 2_000_000
                or not _looks_like_protected_brs(response.text)
            ):
                await client.aclose()
                raise IStudentSessionStoreError("iStudent protected page validation failed")
            self._clients.append(client)
            return client
        except (IStudentSessionStoreError, IStudentTokenError):
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            raise IStudentSessionStoreError("Could not validate stored iStudent session") from None

    async def aclose(self) -> None:
        """Close every dedicated client created by this provider."""
        clients, self._clients = self._clients, []
        for client in clients:
            await client.aclose()
