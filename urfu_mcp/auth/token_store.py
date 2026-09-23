"""Secure OS-keyring persistence for acquired OAuth token sets."""

from __future__ import annotations

import json
import math
from typing import Any

from urfu_mcp.auth.credential_store import (PasswordBackend,
                                            create_password_backend)
from urfu_mcp.auth.oidc import OidcTokens


class TokenStoreError(ValueError):
    """A safe error while loading or storing OAuth tokens."""


class TokenStore:
    """Store OAuth tokens under the system keyring, never in a plaintext file."""

    def __init__(
        self,
        backend: PasswordBackend,
        *,
        service_name: str = "urfu-mcp.oauth",
        account: str = "modeus",
    ) -> None:
        self._backend = backend
        self._service_name = service_name
        self._account = account

    def save(self, tokens: OidcTokens) -> None:
        if (
            not tokens.access_token
            or not tokens.id_token
            or tokens.token_type.lower() != "bearer"
        ):
            raise TokenStoreError("Invalid OAuth token set")
        data = {
            "access_token": tokens.access_token,
            "id_token": tokens.id_token,
            "token_type": tokens.token_type,
            "expires_at": tokens.expires_at,
            "refresh_token": tokens.refresh_token,
        }
        try:
            encoded = json.dumps(data, separators=(",", ":"), allow_nan=False)
            self._backend.set_password(self._service_name, self._account, encoded)
        except (TypeError, ValueError):
            raise TokenStoreError("Could not serialize OAuth tokens") from None
        except Exception:  # noqa: BLE001 - hide backend errors that may contain secrets
            raise TokenStoreError("Could not save OAuth tokens") from None

    def load(self) -> OidcTokens | None:
        try:
            encoded = self._backend.get_password(self._service_name, self._account)
        except Exception:  # noqa: BLE001 - hide backend errors that may contain secrets
            raise TokenStoreError("Could not load OAuth tokens") from None
        if encoded is None:
            return None
        try:
            data: Any = json.loads(encoded)
            if (
                not isinstance(data, dict)
                or set(data)
                != {"access_token", "id_token", "token_type", "expires_at", "refresh_token"}
                or not isinstance(data["access_token"], str)
                or not data["access_token"]
                or not isinstance(data["id_token"], str)
                or not data["id_token"]
                or not isinstance(data["token_type"], str)
                or data["token_type"].lower() != "bearer"
                or data["expires_at"] is not None
                and (type(data["expires_at"]) not in (int, float))
                or data["refresh_token"] is not None
                and not isinstance(data["refresh_token"], str)
            ):
                raise ValueError
            expires_at = data["expires_at"]
            if expires_at is not None and not math.isfinite(expires_at):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise TokenStoreError("Malformed stored OAuth tokens") from None
        return OidcTokens(
            access_token=data["access_token"],
            id_token=data["id_token"],
            token_type=data["token_type"],
            expires_at=expires_at,
            refresh_token=data["refresh_token"],
        )

    def delete(self) -> None:
        try:
            self._backend.delete_password(self._service_name, self._account)
        except Exception:  # noqa: BLE001 - hide backend errors that may contain secrets
            raise TokenStoreError("Could not delete OAuth tokens") from None


def create_token_store() -> TokenStore:
    """Create a token store backed by the system's viable secure keyring."""
    return TokenStore(create_password_backend())
