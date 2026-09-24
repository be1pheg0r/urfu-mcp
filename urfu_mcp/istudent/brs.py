"""Date-aware orchestration and deterministic subject selection for BRS."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol

from .brs_parser import BRSPageParser, IStudentBRSDetailParser, IStudentBRSHTMLParser
from .brs_source import BRSPageSource, BRSPeriodPages, BRSPeriodRequest
from .errors import (
    AmbiguousSubject,
    InvalidUpstreamResponse,
    SubjectNotFound,
    UpstreamUnavailable,
)
from .public_models import BRSResult, BRSSubject


class BRSPeriodSource(Protocol):
    async def fetch_period(
        self, identity: str, session: object, period: BRSPeriodRequest
    ) -> BRSPeriodPages: ...


class BRSPeriodReader:
    """Atomic full-period read: every overview row requires its own verified detail."""

    def __init__(self, source: BRSPeriodSource) -> None:
        self._source = source

    async def read(
        self, identity: str, session: object, *, as_of: date, period: BRSPeriodRequest
    ) -> BRSResult:
        try:
            pages = await self._source.fetch_period(identity, session, period)
        except Exception:  # noqa: BLE001 - never surface request URLs or session details
            raise UpstreamUnavailable("The iStudent BRS source is unavailable") from None
        try:
            overview = IStudentBRSHTMLParser().parse(
                pages.overview, as_of=as_of, period=period.label
            )
            if len(overview.subjects) != len(pages.details):
                raise InvalidUpstreamResponse("BRS details do not cover every subject")
            parser = IStudentBRSDetailParser()
            subjects = tuple(
                row.model_copy(update={"sections": parser.parse(fragment)})
                for row, fragment in zip(overview.subjects, pages.details, strict=True)
            )
            return overview.model_copy(update={"subjects": subjects})
        except Exception:  # noqa: BLE001 - never expose raw score fragments
            raise InvalidUpstreamResponse("The BRS response could not be parsed safely") from None


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
