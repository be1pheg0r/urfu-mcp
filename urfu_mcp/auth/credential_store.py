"""OS-keyring-backed storage for stashed URFU credentials."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol


class CredentialStoreError(Exception):
    """Base class for safe credential-store errors."""


class CredentialDataError(CredentialStoreError):
    """Stored credential data is malformed or inconsistent."""


class KeyringBackendUnavailable(CredentialStoreError):
    """No viable secure OS keyring backend is available."""


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    """A user's URFU email and password; independent of acquired tokens."""

    email: str
    password: str


class PasswordBackend(Protocol):
    """Minimal keyring-compatible password storage interface."""

    def set_password(self, service: str, account: str, value: str) -> None: ...

    def get_password(self, service: str, account: str) -> str | None: ...

    def delete_password(self, service: str, account: str) -> None: ...


class CredentialStore:
    """Persist credential records in an injected password backend."""

    def __init__(self, backend: PasswordBackend, service_name: str = "urfu-mcp"):
        self._backend = backend
        self._service_name = service_name

    def save(self, credentials: CredentialRecord) -> None:
        value = json.dumps(
            {"email": credentials.email, "password": credentials.password},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._backend.set_password(self._service_name, credentials.email, value)

    def load(self, email: str) -> CredentialRecord | None:
        value = self._backend.get_password(self._service_name, email)
        if value is None:
            return None
        try:
            data = json.loads(value)
            if (
                not isinstance(data, dict)
                or set(data) != {"email", "password"}
                or not isinstance(data["email"], str)
                or not isinstance(data["password"], str)
                or data["email"] != email
            ):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise CredentialDataError("Malformed stored credentials") from None
        return CredentialRecord(email=data["email"], password=data["password"])

    def delete(self, email: str) -> None:
        try:
            self._backend.delete_password(self._service_name, email)
        except Exception:  # noqa: BLE001 - suppress backend details that may leak secrets
            raise CredentialStoreError("Could not delete stored credentials") from None


def create_password_backend() -> PasswordBackend:
    """Return the selected system keyring backend only when it is viable."""
    try:
        import keyring

        backend = keyring.get_keyring()
        backend_type = type(backend)
        module = backend_type.__module__.lower()
        name = backend_type.__name__.lower()
        priority = backend.priority
        viable = (
            isinstance(priority, (int, float))
            and priority > 0
            and "plaintext" not in name
            and not module.startswith("keyrings.alt.file")
            and callable(getattr(backend, "set_password", None))
            and callable(getattr(backend, "get_password", None))
            and callable(getattr(backend, "delete_password", None))
        )
        if not viable:
            raise KeyringBackendUnavailable(
                "A secure OS keyring backend is required"
            )
        return backend
    except KeyringBackendUnavailable:
        raise
    except Exception:  # noqa: BLE001 - normalize keyring discovery failures safely
        raise KeyringBackendUnavailable(
            "A secure OS keyring backend is required"
        ) from None


def create_credential_store(service_name: str = "urfu-mcp") -> CredentialStore:
    """Create a credential store with the selected secure system keyring."""
    return CredentialStore(create_password_backend(), service_name=service_name)
