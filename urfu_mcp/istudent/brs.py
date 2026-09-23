"""Date-aware orchestration and deterministic subject selection for BRS."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from .brs_parser import BRSPageParser
from .brs_source import BRSPageSource
from .errors import (
    AmbiguousSubject,
    InvalidUpstreamResponse,
    SubjectNotFound,
    UpstreamUnavailable,
)
from .public_models import BRSResult, BRSSubject


def normalize_subject_name(value: str) -> str:
    """Collapse whitespace and case-fold, preserving punctuation for identity."""
    return " ".join(value.split()).casefold()


def select_subjects(
    result: BRSResult, selector: str
) -> tuple[BRSSubject, ...]:
    """Resolve `all` or exactly one normalized name without choosing arbitrarily."""
    if not result.subjects:
        raise SubjectNotFound("No source-confirmed BRS subjects were returned")
    if normalize_subject_name(selector) == "all":
        return result.subjects

    wanted = normalize_subject_name(selector)
    matches: Sequence[BRSSubject] = tuple(
        subject
        for subject in result.subjects
        if normalize_subject_name(subject.subject_name) == wanted
    )
    if not matches:
        raise SubjectNotFound("No BRS subject matches the requested name")
    if len(matches) != 1:
        raise AmbiguousSubject("More than one BRS subject matches the requested name")
    return tuple(matches)


class BRSReader:
    """Call the injected per-identity source/parser without shared mutable state."""

    def __init__(self, source: BRSPageSource, parser: BRSPageParser) -> None:
        self._source = source
        self._parser = parser

    async def read(self, identity: str, session: object, *, as_of: date) -> BRSResult:
        """Fetch and normalize one user's BRS response; never cache personal rows."""
        try:
            payload = await self._source.fetch(identity, session)
        except Exception:  # noqa: BLE001 - sanitize source details before MCP boundary
            # Source exceptions may contain URLs, headers, cookies, or response text.
            raise UpstreamUnavailable("The iStudent BRS source is unavailable") from None
        try:
            result = self._parser.parse(payload, as_of=as_of)
        except Exception:  # noqa: BLE001 - sanitize parser details before MCP boundary
            # Parser errors and source fragments are deliberately hidden from callers.
            raise InvalidUpstreamResponse("The BRS response could not be parsed safely") from None
        if not isinstance(result, BRSResult):
            raise InvalidUpstreamResponse("The BRS parser returned an invalid normalized result")
        return result
