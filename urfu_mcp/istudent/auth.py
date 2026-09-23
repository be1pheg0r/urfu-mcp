"""Authentication ports kept separate from Modeus token handling."""

from __future__ import annotations

from typing import Protocol

from .errors import IntegrationUnavailable


class IStudentIdentityProvider(Protocol):
    """Return the trusted current identity without exposing any bearer token."""

    async def current_person(self) -> str | None: ...


class IStudentSessionProvider(Protocol):
    """Acquire an iStudent-specific session for one identity."""

    async def get_session(self, identity: str) -> object | None: ...


class UnconfiguredIStudentSessionProvider:
    """Fail closed until researcher evidence establishes an auth integration."""

    async def get_session(self, identity: str) -> object | None:
        # Do not mention identity or leak provider details in public errors.
        del identity
        raise IntegrationUnavailable(
            "iStudent BRS is unavailable until its authentication contract is verified"
        )
