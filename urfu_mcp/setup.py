"""First-run setup state machine and terminal presentation."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import UUID

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from urfu_mcp.auth.credential_store import CredentialStoreError
from urfu_mcp.auth.modeus_browser import run_unified_login
from urfu_mcp.auth.token_store import TokenStoreError, create_token_store
from urfu_mcp.config import AppConfig, ConfigError, initialize_config, load_config
from urfu_mcp.wizard_fire import play_flame_logo

T = TypeVar("T")


class SetupError(RuntimeError):
    """A safe error that can be shown by the setup UI."""


class SetupState(Enum):
    WELCOME = auto()
    PREFLIGHT = auto()
    READINESS = auto()
    CONFIG = auto()
    AUTH = auto()
    VERIFICATION = auto()
    READY = auto()
    FAILED = auto()
    CANCELLED = auto()


class SetupServices(Protocol):
    def preflight(self) -> None: ...
    def readiness(self) -> None: ...
    def config_is_ready(self) -> bool: ...
    def initialize_config(self) -> None: ...
    def has_valid_auth(self) -> bool: ...
    def authenticate(self) -> None: ...
    def verify(self) -> None: ...


class SetupUIProtocol(Protocol):
    def welcome(self) -> bool: ...
    def step(self, name: str, callback: Callable[[], T]) -> T: ...
    def ready(self) -> None: ...
    def failed(self, message: str) -> None: ...
    def cancelled(self) -> None: ...


@dataclass(slots=True)
class DefaultSetupServices:
    """Application operations used by the setup state machine."""

    def preflight(self) -> None:
        required = ("authlib", "httpx", "keyring", "mcp", "playwright", "pydantic", "yaml")
        if any(importlib.util.find_spec(module) is None for module in required):
            raise SetupError(
                "Application dependencies are missing. Install the project first with `uv sync`."
            )

    def readiness(self) -> None:
        if importlib.util.find_spec("urfu_mcp.auth.modeus_browser") is None:
            raise SetupError("The installed URFU-MCP application is incomplete.")

    def initialize_config(self) -> None:
        try:
            initialize_config()
        except ConfigError:
            raise SetupError("Could not create or validate config.yaml securely.") from None

    def config_is_ready(self) -> bool:
        if not Path("config.yaml").is_file():
            return False
        try:
            load_config()
        except ConfigError:
            return False
        return True

    def has_valid_auth(self) -> bool:
        try:
            config = load_config()
            tokens = create_token_store().load()
        except ConfigError:
            return False
        except (CredentialStoreError, TokenStoreError):
            raise SetupError(
                "Не удалось получить доступ к системному keyring. Настройте безопасное "
                "хранилище ключей в этой среде и повторите `urfu-mcp setup`."
            ) from None
        if not _tokens_usable(config, tokens):
            return False
        person_id = getattr(tokens, "person_id", None)
        return isinstance(person_id, str) and _istudent_session_ready(person_id)

    def authenticate(self) -> None:
        if not run_unified_login():
            raise SetupError("iStudent authentication is not verified; setup is incomplete.")

    def verify(self) -> None:
        try:
            config = load_config()
            tokens = create_token_store().load()
        except ConfigError:
            raise SetupError("Local configuration or secure authentication could not be verified.") from None
        except (CredentialStoreError, TokenStoreError):
            raise SetupError(
                "Не удалось получить доступ к системному keyring. Настройте безопасное "
                "хранилище ключей в этой среде и повторите `urfu-mcp setup`."
            ) from None
        if not _tokens_usable(config, tokens):
            raise SetupError("No valid Modeus sign-in was found. Rerun `urfu-mcp setup` to try again.")
        person_id = getattr(tokens, "person_id", None)
        if not isinstance(person_id, str) or not _istudent_session_ready(person_id):
            raise SetupError("iStudent authentication cannot be verified; setup is incomplete.")


def _istudent_session_ready(person_id: str) -> bool:
    """Check the signed, bound iStudent session and protected page, then close it."""
    from urfu_mcp.istudent.session_auth import StoredIStudentSessionProvider
    from urfu_mcp.istudent.session_store import create_istudent_session_store

    try:
        provider = StoredIStudentSessionProvider(create_istudent_session_store())
    except CredentialStoreError:
        raise SetupError("Не удалось получить доступ к системному keyring.") from None

    async def check() -> bool:
        try:
            await provider.get_session(person_id)
            return True
        except Exception:  # noqa: BLE001 - upstream/backend errors may contain secrets
            return False
        finally:
            await provider.aclose()

    try:
        return asyncio.run(check())
    except Exception:  # noqa: BLE001 - never expose backend errors in setup output
        return False


def _tokens_usable(config: AppConfig, tokens: object) -> bool:
    if tokens is None or config.auth.token_kind not in {"id_token", "access_token"}:
        return False
    person_id = getattr(tokens, "person_id", None)
    if not isinstance(person_id, str):
        return False
    try:
        parsed_person_id = UUID(person_id)
    except ValueError:
        return False
    if parsed_person_id.int == 0 or str(parsed_person_id) != person_id:
        return False
    expires_at = getattr(tokens, "expires_at", None)
    if not isinstance(expires_at, (int, float)) or expires_at <= datetime.now(UTC).timestamp():
        return False
    selected = getattr(tokens, config.auth.token_kind, None)
    return isinstance(selected, str) and bool(selected)


class SetupUI:
    """Rich interactive UI with plain, animation-free redirected output."""

    def __init__(self, *, no_color: bool = False, non_interactive: bool = False):
        self.interactive = bool(sys.stdin.isatty() and sys.stdout.isatty() and not non_interactive)
        color_disabled = no_color or "NO_COLOR" in os.environ
        self._animate_welcome = self.interactive and not color_disabled
        self.console = Console(
            highlight=False,
            color_system="auto" if self._animate_welcome else None,
            force_terminal=self._animate_welcome,
            no_color=not self._animate_welcome,
        )

    def welcome(self) -> bool:
        logo = _block_logo()
        if self._animate_welcome:
            play_flame_logo(self.console, logo)
        self.console.print(Panel(
            Text(f"{logo}\nURFU-MCP", style="bold blue", justify="center"),
            subtitle="Помощник по первичной настройке",
            border_style="blue",
            padding=(1, 2),
        ))
        self.console.print("Настроим локальный конфиг и проверим вход в Modeus.")
        if not self.interactive:
            return True
        try:
            return self.console.input("Продолжить? [Y/n] ").strip().lower() not in {"n", "no", "нет"}
        except (EOFError, KeyboardInterrupt):
            return False

    def step(self, name: str, callback: Callable[[], T]) -> T:
        if self.interactive:
            with self.console.status(f"[blue]{name}…[/blue]", spinner="dots"):
                result = callback()
        else:
            self.console.print(f"• {name}…")
            result = callback()
        self.console.print(f"✓ {name}")
        return result

    def ready(self) -> None:
        self.console.print(Panel(
            "Настройка завершена. Добавьте сервер `urfu-mcp` в конфигурацию вашего MCP-клиента "
            "и запускайте его командой `urfu-mcp serve`.",
            title="Готово",
            border_style="blue",
        ))

    def failed(self, message: str) -> None:
        self.console.print(f"✗ Настройка не завершена: {message}", style="red")

    def cancelled(self) -> None:
        self.console.print("Настройка отменена.")


_ALLOWED: dict[SetupState, set[SetupState]] = {
    SetupState.WELCOME: {SetupState.PREFLIGHT, SetupState.FAILED, SetupState.CANCELLED},
    SetupState.PREFLIGHT: {SetupState.READINESS, SetupState.FAILED, SetupState.CANCELLED},
    SetupState.READINESS: {SetupState.CONFIG, SetupState.FAILED, SetupState.CANCELLED},
    SetupState.CONFIG: {SetupState.AUTH, SetupState.FAILED, SetupState.CANCELLED},
    SetupState.AUTH: {SetupState.VERIFICATION, SetupState.FAILED, SetupState.CANCELLED},
    SetupState.VERIFICATION: {SetupState.READY, SetupState.FAILED, SetupState.CANCELLED},
    SetupState.READY: set(),
    SetupState.FAILED: set(),
    SetupState.CANCELLED: set(),
}


@dataclass(slots=True)
class SetupOrchestrator:
    services: SetupServices
    ui: SetupUIProtocol
    state: SetupState = SetupState.WELCOME
    history: list[SetupState] | None = None

    def transition(self, state: SetupState) -> None:
        if state not in _ALLOWED[self.state]:
            raise RuntimeError(f"Invalid setup transition: {self.state.name} -> {state.name}")
        self.state = state
        if self.history is not None:
            self.history.append(state)

    def run(self) -> int:
        try:
            if not self.ui.welcome():
                self.transition(SetupState.CANCELLED)
                self.ui.cancelled()
                return 1
            self.transition(SetupState.PREFLIGHT)
            self.ui.step("Проверка окружения", self.services.preflight)
            self.transition(SetupState.READINESS)
            self.ui.step("Проверка готовности приложения", self.services.readiness)
            self.transition(SetupState.CONFIG)
            if self.ui.step("Проверка конфигурации", self.services.config_is_ready):
                pass
            else:
                self.ui.step("Создание конфигурации", self.services.initialize_config)
            self.transition(SetupState.AUTH)
            authenticated = self.ui.step("Проверка входа в Modeus", self.services.has_valid_auth)
            if not authenticated:
                self.ui.step("Вход через браузер URFU SSO", self.services.authenticate)
            self.transition(SetupState.VERIFICATION)
            self.ui.step("Локальная проверка", self.services.verify)
            self.ui.ready()
            self.transition(SetupState.READY)
            return 0
        except KeyboardInterrupt:
            if self.state not in {SetupState.READY, SetupState.FAILED, SetupState.CANCELLED}:
                self.transition(SetupState.CANCELLED)
            self.ui.cancelled()
            return 1
        except Exception as error:  # noqa: BLE001 - providers may raise unsafe details
            self.transition(SetupState.FAILED)
            message = (
                str(error)
                if isinstance(error, SetupError)
                else f"Непредвиденная ошибка типа {type(error).__name__}. "
                "Перезапустите команду; если сбой повторится, сообщите этот тип ошибки."
            )
            self.ui.failed(message)
            return 1


def run_setup(*, no_color: bool = False, non_interactive: bool = False) -> int:
    """Run the interactive first-run path; never launch an MCP server."""
    return run_setup_with(
        steps=DefaultSetupServices(),
        ui=SetupUI(no_color=no_color, non_interactive=non_interactive),
    )


def run_setup_with(*, steps: SetupServices, ui: SetupUIProtocol) -> int:
    """Testable orchestrator entry with injected operations and presentation."""
    return SetupOrchestrator(services=steps, ui=ui, history=[]).run()


def _block_logo() -> str:
    glyphs = {
        "U": ("██╗  ██╗", "██║  ██║", "██║  ██║", "╚█████╔╝", " ╚═══╝ "),
        "R": ("██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██║  ██║"),
        "F": ("███████╗", "██╔════╝", "█████╗  ", "██╔══╝  ", "██║     "),
        "-": ("        ", "        ", "███████╗", "        ", "        "),
        "M": ("███╗   ███╗", "████╗ ████║", "██╔████╔██║", "██║╚██╔╝██║", "██║ ╚═╝ ██║"),
        "C": (" ██████╗ ", "██╔════╝ ", "██║      ", "██║      ", "╚██████╗ "),
        "P": ("██████╗ ", "██╔══██╗", "██████╔╝", "██╔═══╝ ", "██║     "),
    }
    return "\n".join("  ".join(glyphs[letter][row] for letter in "URFU-MCP") for row in range(5))
