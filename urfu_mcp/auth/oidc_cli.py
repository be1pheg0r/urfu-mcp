"""Generic, loopback-only interactive OIDC login orchestration."""

from __future__ import annotations

import asyncio
import ipaddress
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol
from urllib.parse import urlsplit

from urfu_mcp.auth.oidc import OidcClient, OidcConfig, OidcTokens
from urfu_mcp.auth.token_store import create_token_store


class CallbackReceiver(Protocol):
    async def start(self) -> None: ...
    async def wait_for_callback(self, timeout: float) -> str: ...
    async def close(self) -> None: ...


class TokenSaver(Protocol):
    def save(self, tokens: OidcTokens) -> None: ...


class LoopbackCallbackReceiver:
    """Receive exactly one HTTP callback on the configured loopback endpoint."""

    def __init__(self, redirect_uri: str) -> None:
        parsed = urlsplit(redirect_uri)
        try:
            loopback = parsed.hostname is not None and (
                parsed.hostname.lower() == "localhost"
                or ipaddress.ip_address(parsed.hostname).is_loopback
            )
        except ValueError:
            loopback = False
        hostname = parsed.hostname
        if (
            parsed.scheme != "http"
            or hostname is None
            or not loopback
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("interactive OIDC requires an explicit loopback HTTP redirect")
        if not parsed.path:
            raise ValueError("redirect URI must include a callback path")
        self._host = "127.0.0.1" if hostname.lower() == "localhost" else hostname
        self._port = parsed.port
        self._path = parsed.path
        self._redirect_uri = redirect_uri
        self._server: asyncio.Server | None = None
        self._event = asyncio.Event()
        self._callback: str | None = None

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("callback receiver already started")
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = request_line.decode("ascii", errors="replace").strip().split(" ")
            if len(parts) != 3 or parts[0] != "GET":
                await self._respond(writer, "400 Bad Request", "Invalid callback.")
                return
            while True:
                header = await asyncio.wait_for(reader.readline(), timeout=5)
                if header in (b"\r\n", b"\n", b""):
                    break
            target = urlsplit(parts[1])
            if target.path != self._path or not target.query:
                await self._respond(writer, "400 Bad Request", "Invalid callback.")
                return
            try:
                self._set_callback(f"{self._redirect_uri}?{target.query}")
            except RuntimeError:
                await self._respond(writer, "400 Bad Request", "Callback already received.")
                return
            await self._respond(writer, "200 OK", "Authorization received. You may close this window.")
        except (TimeoutError, UnicodeError):
            await self._respond(writer, "400 Bad Request", "Invalid callback.")
        finally:
            writer.close()
            await writer.wait_closed()

    async def _respond(self, writer: asyncio.StreamWriter, status: str, message: str) -> None:
        body = f"<!doctype html><title>urfu-mcp</title><p>{message}</p>".encode()
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
        )
        await writer.drain()

    def _set_callback(self, callback: str) -> None:
        if self._callback is not None:
            raise RuntimeError("callback already received")
        self._callback = callback
        self._event.set()

    async def wait_for_callback(self, timeout: float) -> str:
        await asyncio.wait_for(self._event.wait(), timeout=timeout)
        if self._callback is None:
            raise RuntimeError("callback receiver closed")
        return self._callback

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


async def login(
    config: OidcConfig,
    *,
    oidc_client: OidcClient | None = None,
    callback_receiver: CallbackReceiver | None = None,
    token_store: TokenSaver | None = None,
    webbrowser_open: Callable[[str], bool] = webbrowser.open,
    callback_timeout: float = 300,
) -> bool:
    """Run the browser flow; emit only fixed, non-secret status messages."""
    receiver = callback_receiver
    try:
        client = oidc_client or OidcClient(config)
        receiver = receiver or LoopbackCallbackReceiver(config.redirect_uri)
        store = token_store or create_token_store()
        await receiver.start()
        login_request = await client.begin_login()
        if not webbrowser_open(login_request.authorization_url):
            print("Could not open the system browser.")
            return False
        print("Complete sign-in in the browser; waiting for the local callback.")
        callback = await receiver.wait_for_callback(callback_timeout)
        tokens = await client.complete_login(callback, login_request.transaction)
        store.save(tokens)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("OIDC login cancelled.")
        return False
    except Exception:  # noqa: BLE001 - suppress provider/network/keyring details
        print("OIDC login failed; no tokens were saved.")
        return False
    finally:
        if receiver is not None:
            with suppress(Exception):  # cleanup details may contain provider data
                await receiver.close()
    print("OIDC login completed; tokens were stored securely.")
    return True


def run_login(config: OidcConfig) -> bool:
    """Synchronous CLI adapter."""
    try:
        return asyncio.run(login(config))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("OIDC login cancelled.")
        return False
