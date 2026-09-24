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
from urfu_mcp.istudent.auth import (
    IStudentSessionProvider,
    UnconfiguredIStudentSessionProvider,
)
from urfu_mcp.istudent.brs import BRSPeriodReader
from urfu_mcp.istudent.brs_source import IStudentBRSPageSource
from urfu_mcp.istudent.mcp_tools import register_brs_tool
from urfu_mcp.istudent.session_auth import StoredIStudentSessionProvider
from urfu_mcp.istudent.session_store import create_istudent_session_store
from urfu_mcp.modeus.authorization import ResolvedPersonAuthorizer
from urfu_mcp.modeus.direct_client import ModeusDirectClient
from urfu_mcp.modeus.errors import NotAuthenticated
from urfu_mcp.modeus.mcp_tools import register_schedule_tools
from urfu_mcp.modeus.person_resolver import PersonResolver
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
    istudent_session_provider: StoredIStudentSessionProvider | None = None

    async def aclose(self) -> None:
        try:
            if self.istudent_session_provider is not None:
                await self.istudent_session_provider.aclose()
        finally:
            await self.client.aclose()


def build_server(
    config: AppConfig,
    *,
    token_store: TokenStore | None = None,
    client: httpx.AsyncClient | None = None,
    istudent_session_provider: IStudentSessionProvider | None = None,
    brs_reader: BRSPeriodReader | None = None,
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

    http_client = client or httpx.AsyncClient(timeout=config.modeus_http.timeout_seconds)
    gateway = ModeusDirectClient(
        client=http_client,
        timeout=config.modeus_http.timeout_seconds,
        max_response_bytes=config.modeus_http.max_response_bytes,
        event_page_size=config.modeus_http.event_page_size,
        timezone_name=config.schedule.timezone,
    )
    reader = ScheduleReader(
        gateway,
        max_days=config.schedule.max_days,
        max_subjects=config.schedule.max_subjects,
    )
    resolver = PersonResolver(
        gateway,
        page_size=config.modeus_http.person_page_size,
        max_pages=config.modeus_http.person_max_pages,
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
    # The default remains deliberately unavailable. An explicit session provider
    # opts into the verified, bounded read-only page source; tests and callers may
    # inject a period reader directly to control its transport.
    session_provider = istudent_session_provider or UnconfiguredIStudentSessionProvider()
    period_reader = brs_reader
    if period_reader is None and istudent_session_provider is not None:
        period_reader = BRSPeriodReader(IStudentBRSPageSource())
    register_brs_tool(
        server,
        identity_provider=_IdentityProvider(str(parsed_person_id)),
        session_provider=session_provider,
        reader=period_reader,
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
    http_client = client or httpx.AsyncClient(timeout=app_config.modeus_http.timeout_seconds)
    provider = None
    try:
        _validate_runtime_auth(app_config, token_store)
        provider = StoredIStudentSessionProvider(create_istudent_session_store())
        server = build_server(
            app_config,
            token_store=token_store,
            client=http_client,
            istudent_session_provider=provider,
        )
    except Exception:
        async def close_failed_setup() -> None:
            try:
                if provider is not None:
                    await provider.aclose()
            finally:
                await http_client.aclose()
        asyncio.run(close_failed_setup())
        raise
    return Runtime(server, http_client, provider)


def _validate_runtime_auth(config: AppConfig, token_store: TokenStore | None) -> None:
    """Validate Modeus token selection and identity before iStudent store access."""
    token_kind = getattr(config.auth, "token_kind", None)
    if token_kind not in {"id_token", "access_token"}:
        raise NotAuthenticated("No supported Modeus token kind is configured")
    try:
        tokens = (token_store or create_token_store()).load()
    except Exception:  # noqa: BLE001 - keyring errors may contain secret-bearing details
        raise NotAuthenticated("Could not load stored Modeus token") from None
    if tokens is None or tokens.expires_at is None or tokens.expires_at <= time.time():
        raise NotAuthenticated("No unexpired stored Modeus token is available")
    token = getattr(tokens, token_kind, None)
    if not isinstance(token, str) or not token:
        raise NotAuthenticated("The selected stored Modeus token is unavailable")
    try:
        person_id = UUID(tokens.person_id)
    except (ValueError, TypeError, AttributeError):
        raise NotAuthenticated("Stored authentication has no valid person identity") from None
    if person_id.int == 0:
        raise NotAuthenticated("Stored authentication has no valid person identity")


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

    async def run_and_close() -> None:
        try:
            await runtime.server.run_stdio_async()
        finally:
            await runtime.aclose()

    asyncio.run(run_and_close())
    return 0


__all__ = ["Runtime", "RuntimeConfig", "build_server", "create_runtime", "serve"]
