"""Pure parser port for a future evidence-backed iStudent response mapping."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from .public_models import BRSResult


class BRSPageParser(Protocol):
    """Normalize a verified source payload; implementations own schema evidence."""

    def parse(self, payload: str | bytes, *, as_of: date) -> BRSResult: ...
