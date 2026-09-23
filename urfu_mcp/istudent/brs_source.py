"""Protected BRS source port; no HTTP route is guessed here."""

from __future__ import annotations

from typing import Protocol


class BRSPageSource(Protocol):
    """Fetch one identity's protected page through its iStudent session."""

    async def fetch(self, identity: str, session: object) -> str | bytes: ...
