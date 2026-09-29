"""Evidence-independent contracts for authenticated eLearn services."""

from __future__ import annotations

from typing import Protocol

from .errors import IntegrationUnavailable


class ELearnIdentityProvider(Protocol):
    """Return the trusted current identity without exposing credentials."""

    async def current_person(self) -> str | None: ...


class ELearnSessionProvider(Protocol):
    """Acquire an eLearn-specific session for one identity."""

    async def get_session(self, identity: str) -> object | None: ...


class UnconfiguredELearnSessionProvider:
    """Fail closed until eLearn authentication is verified."""

    async def get_session(self, identity: str) -> object | None:
        del identity
        raise IntegrationUnavailable("eLearn is unavailable until its authentication contract is verified")


__all__ = ["ELearnIdentityProvider", "ELearnSessionProvider", "UnconfiguredELearnSessionProvider"]
