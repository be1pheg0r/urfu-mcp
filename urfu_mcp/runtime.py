"""Single-user stdio runtime for Modeus tools and fail-closed iStudent BRS."""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
from mcp.server import MCPServer

from urfu_mcp.auth.token_store import TokenStore, create_token_store
from urfu_mcp.config import AppConfig, ConfigError, load_config
from urfu_mcp.istudent.auth import UnconfiguredIStudentSessionProvider
from urfu_mcp.istudent.mcp_tools import register_brs_tool
from urfu_mcp.modeus.authorization import ResolvedPersonAuthorizer
from urfu_mcp.modeus.errors import NotAuthenticated
from urfu_mcp.modeus.gateway import SfeduGateway
from urfu_mcp.modeus.mcp_tools import register_schedule_tools
from urfu_mcp.modeus.person_resolver import PersonResolver
from urfu_mcp.modeus.person_source import SfeduPersonPageSource
from urfu_mcp.modeus.reader import ScheduleReader

# Kept as an import alias for callers of the former environment config type.
RuntimeConfig = AppConfig


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
    config: AppConfig,
    *,
    token_store: TokenStore | None = None,
    client: httpx.AsyncClient | None = None,
) -> MCPServer:
    """Build the MCP server from YAML and authenticated identity in the keyring."""
    token_kind = getattr(config.auth, "token_kind", None)
    if token_kind not in {"id_token", "access_token"}:
        raise NotAuthenticated("No supported Modeus token kind is configured")
    try:
        tokens = (token_store or create_token_store()).load()
    except Exception:  # noqa: BLE001 - keyring errors can contain secret-bearing details
        raise NotAuthenticated("Could not load stored Modeus token") from None
    if tokens is None or tokens.expires_at is None or tokens.expires_at <= time.time():
        raise NotAuthenticated("No unexpired stored Modeus token is available")
    token = getattr(tokens, token_kind, None)
    if not isinstance(token, str) or not token:
        raise NotAuthenticated("The selected stored Modeus token is unavailable")
    person_id = tokens.person_id
    if not isinstance(person_id, str):
        raise NotAuthenticated("Stored authentication has no person identity; sign in again")
    try:
        parsed_person_id = UUID(person_id)
    except ValueError:
        raise NotAuthenticated("Stored authentication has no valid person identity") from None
    if parsed_person_id.int == 0:
        raise NotAuthenticated("Stored authentication has no valid person identity")

    http_client = client or httpx.AsyncClient(timeout=config.sidecar.timeout_seconds)
    gateway = SfeduGateway(
        config.sidecar.base_url,
        client=http_client,
        timeout=config.sidecar.timeout_seconds,
        max_response_bytes=config.sidecar.max_response_bytes,
        size=config.sidecar.event_page_size,
    )
    page_source = SfeduPersonPageSource(
        config.sidecar.base_url,
        api_key=config.sidecar.api_key.get_secret_value(),
        client=http_client,
        timeout=config.sidecar.timeout_seconds,
        max_response_bytes=config.sidecar.max_response_bytes,
    )
    reader = ScheduleReader(
        gateway,
        max_days=config.schedule.max_days,
        max_subjects=config.schedule.max_subjects,
    )
    resolver = PersonResolver(
        page_source,
        page_size=config.sidecar.person_page_size,
        max_pages=config.sidecar.person_max_pages,
    )
    server = MCPServer(config.server.name)
    register_schedule_tools(
        server,
        reader=reader,
        identity_provider=_IdentityProvider(str(parsed_person_id)),
        token_supplier=_TokenSupplier(token, tokens.expires_at),
        person_resolver=resolver,
        person_authorizer=ResolvedPersonAuthorizer(),
        timezone_name=config.schedule.timezone,
    )
    # The BRS contract is registered, but no protected iStudent auth/source
    # adapter is available until its real session and response schema are verified.
    register_brs_tool(
        server,
        identity_provider=_IdentityProvider(str(parsed_person_id)),
        session_provider=UnconfiguredIStudentSessionProvider(),
        reader=None,
    )
    return server


def create_runtime(
    config: AppConfig | None = None,
    *,
    config_path: str | Path = "config.yaml",
    token_store: TokenStore | None = None,
    client: httpx.AsyncClient | None = None,
) -> Runtime:
    """Load/generate YAML config and create the stdio server/client pair."""
    app_config = config or load_config(config_path)
    http_client = client or httpx.AsyncClient(timeout=app_config.sidecar.timeout_seconds)
    try:
        server = build_server(
            app_config,
            token_store=token_store,
            client=http_client,
        )
    except Exception:
        if client is None:
            asyncio.run(http_client.aclose())
        raise
    return Runtime(server, http_client)


def serve(config_path: str | Path = "config.yaml") -> int:
    """Run the configured MCP server over stdio, emitting no diagnostics to stdout."""
    try:
        runtime = create_runtime(config_path=config_path)
    except ConfigError:
        print("config.yaml is invalid; correct its settings and try again.", file=sys.stderr)
        return 1
    except (ValueError, NotAuthenticated):
        print("MCP is not authenticated. Run `urfu-mcp auth` to sign in, then retry.", file=sys.stderr)
        return 1
    try:
        runtime.server.run(transport="stdio")
    finally:
        asyncio.run(runtime.aclose())
    return 0


__all__ = ["Runtime", "RuntimeConfig", "build_server", "create_runtime", "serve"]
