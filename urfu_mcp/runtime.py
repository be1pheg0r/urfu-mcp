"""Single-user stdio runtime composition for the existing Modeus tools."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from uuid import UUID

import httpx
from mcp.server import MCPServer

from urfu_mcp.auth.token_store import TokenStore, create_token_store
from urfu_mcp.modeus.authorization import ResolvedPersonAuthorizer
from urfu_mcp.modeus.gateway import SfeduGateway
from urfu_mcp.modeus.mcp_tools import register_schedule_tools
from urfu_mcp.modeus.person_resolver import PersonResolver
from urfu_mcp.modeus.person_source import SfeduPersonPageSource
from urfu_mcp.modeus.reader import ScheduleReader


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    person_id: str
    token_kind: str
    sfedu_url: str
    api_key: str = field(repr=False)
    max_days: int
    max_subjects: int

    @classmethod
    def from_environment(cls) -> RuntimeConfig:
        person_id = _required("URFU_MCP_PERSON_ID")
        try:
            parsed_person = UUID(person_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("URFU_MCP_PERSON_ID must be a non-nil UUID") from None
        if parsed_person.int == 0:
            raise ValueError("URFU_MCP_PERSON_ID must be a non-nil UUID")

        token_kind = _required("URFU_MCP_MODEUS_TOKEN_KIND")
        if token_kind not in {"id_token", "access_token"}:
            raise ValueError(
                "URFU_MCP_MODEUS_TOKEN_KIND must be id_token or access_token"
            )

        sfedu_url = _required("URFU_MCP_SFEDU_URL")
        api_key = _required("URFU_MCP_API_KEY")
        max_days = _bounded_integer("URFU_MCP_MAX_DAYS", 1, 31)
        max_subjects = _bounded_integer("URFU_MCP_MAX_SUBJECTS", 1, 10)
        return cls(
            str(parsed_person), token_kind, sfedu_url, api_key, max_days, max_subjects
        )


def _required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _bounded_integer(name: str, minimum: int, maximum: int) -> int:
    value = _required(name)
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(
            f"{name} must be an integer between {minimum} and {maximum}"
        ) from None
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed


class _IdentityProvider:
    def __init__(self, person_id: str) -> None:
        self._person_id = person_id

    async def current_person(self) -> str:
        return self._person_id


class _TokenSupplier:
    def __init__(self, token: str, expires_at: float) -> None:
        self._token = token
        self._expires_at = expires_at

    async def get_token(self, identity: str) -> str:
        if time.time() >= self._expires_at:
            from urfu_mcp.modeus.errors import NotAuthenticated

            raise NotAuthenticated("Stored Modeus token has expired")
        return self._token


@dataclass(slots=True)
class Runtime:
    server: MCPServer
    client: httpx.AsyncClient

    async def aclose(self) -> None:
        await self.client.aclose()


def build_server(
    config: RuntimeConfig,
    *,
    token_store: TokenStore | None = None,
    client: httpx.AsyncClient | None = None,
) -> MCPServer:
    """Build the MCP server after loading and validating a stored selected token."""
    from urfu_mcp.modeus.errors import NotAuthenticated

    try:
        tokens = (token_store or create_token_store()).load()
    except Exception:  # noqa: BLE001 - keyring errors can contain secret-bearing details
        raise NotAuthenticated("Could not load stored Modeus token") from None
    if tokens is None or tokens.expires_at is None or tokens.expires_at <= time.time():
        raise NotAuthenticated("No unexpired stored Modeus token is available")
    token = getattr(tokens, config.token_kind, None)
    if not isinstance(token, str) or not token:
        raise NotAuthenticated("The selected stored Modeus token is unavailable")

    http_client = client or httpx.AsyncClient(timeout=10)
    gateway = SfeduGateway(config.sfedu_url, client=http_client)
    page_source = SfeduPersonPageSource(
        config.sfedu_url, api_key=config.api_key, client=http_client
    )
    reader = ScheduleReader(
        gateway, max_days=config.max_days, max_subjects=config.max_subjects
    )
    resolver = PersonResolver(page_source)
    server = MCPServer("urfu-mcp")
    register_schedule_tools(
        server,
        reader=reader,
        identity_provider=_IdentityProvider(config.person_id),
        token_supplier=_TokenSupplier(token, tokens.expires_at),
        person_resolver=resolver,
        person_authorizer=ResolvedPersonAuthorizer(),
    )
    return server


def create_runtime(
    config: RuntimeConfig | None = None,
    *,
    token_store: TokenStore | None = None,
    client: httpx.AsyncClient | None = None,
) -> Runtime:
    """Create a server and its shared HTTP client for the stdio process lifetime."""
    http_client = client or httpx.AsyncClient(timeout=10)
    try:
        server = build_server(
            config or RuntimeConfig.from_environment(),
            token_store=token_store,
            client=http_client,
        )
    except Exception:
        if client is None:
            asyncio.run(http_client.aclose())
        raise
    return Runtime(server, http_client)


def serve() -> int:
    """Run the configured MCP server over stdio, emitting no diagnostics to stdout."""
    from urfu_mcp.modeus.errors import NotAuthenticated

    try:
        runtime = create_runtime()
    except (ValueError, NotAuthenticated):
        print("MCP runtime configuration or stored token is invalid.", file=sys.stderr)
        return 1
    try:
        runtime.server.run(transport="stdio")
    finally:
        asyncio.run(runtime.aclose())
    return 0


__all__ = ["Runtime", "RuntimeConfig", "build_server", "create_runtime", "serve"]
