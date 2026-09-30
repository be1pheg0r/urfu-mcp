"""Safe, bounded person search over an injected canonical page source.

The page source adapts the eventual approved sidecar route to this protocol. Its
canonical page shape is ``{"items": [...], "page": {"number",
"totalPages", "totalElements"}}``; this module does not assume an HTTP route.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import ceil
from typing import Any, Protocol, cast

from pydantic import ValidationError

from .errors import (
    AmbiguousPerson,
    IncompleteResult,
    InvalidUpstreamResponse,
    PersonNotFound,
)
from .models import NormalizedModel, PersonCandidate


class PersonPageSource(Protocol):
    """Fetch one canonical page using the current identity and call token."""

    async def fetch_page(
        self,
        query: str,
        page_number: int,
        page_size: int,
        *,
        identity: str,
        token: str,
    ) -> Mapping[str, Any]: ...


class PersonSearchResult(NormalizedModel):
    """Candidates found so far and whether every upstream page was visited."""

    candidates: list[PersonCandidate]
    complete: bool
    next_page: int | None = None


class PersonResolver:
    """Search people safely without choosing among ambiguous name matches."""

    def __init__(self, page_source: PersonPageSource, *, page_size: int = 50, max_pages: int = 10) -> None:
        if type(page_size) is not int or page_size < 1:
            raise ValueError("page_size must be a positive integer")
        if type(max_pages) is not int or max_pages < 1:
            raise ValueError("max_pages must be a positive integer")
        self._page_source = page_source
        self.page_size = page_size
        self.max_pages = max_pages

    async def search(self, query: str, *, identity: str, token: str) -> PersonSearchResult:
        """Fetch sequential pages, deduplicate IDs, and explicitly mark truncation."""
        if not query.strip():
            raise ValueError("person query must not be empty")
        if not identity or not token:
            raise ValueError("identity and per-call token are required")

        candidates: dict[str, PersonCandidate] = {}
        expected_pages: int | None = None
        expected_elements: int | None = None
        received_count = 0
        for page_number in range(self.max_pages):
            raw = await self._page_source.fetch_page(
                query, page_number, self.page_size, identity=identity, token=token
            )
            page_items, total_pages, total_elements = self._validate_page(raw, page_number)
            if expected_pages is None:
                expected_pages, expected_elements = total_pages, total_elements
            elif (total_pages, total_elements) != (expected_pages, expected_elements):
                raise InvalidUpstreamResponse("person page metadata changed during pagination")
            received_count += len(page_items)
            new_candidates = 0
            for raw_candidate in page_items:
                try:
                    parsed = PersonCandidate.model_validate(raw_candidate)
                except (ValidationError, TypeError, ValueError) as exc:
                    raise InvalidUpstreamResponse("person search candidate has an invalid schema") from exc
                previous = candidates.get(parsed.person_id)
                if previous is not None and previous != parsed:
                    raise InvalidUpstreamResponse("duplicate person ID has conflicting candidate data")
                if previous is None:
                    candidates[parsed.person_id] = parsed
                    new_candidates += 1
            # Observed live on 2026-09-30: Modeus returns the page-0 records for
            # a requested page 1, so a page that contributes nothing new means
            # upstream is not advancing. Silently deduplicating it would claim a
            # complete search that silently dropped records, so fail closed. The
            # final page is exempt: nothing beyond it is expected.
            if page_number and page_number + 1 < total_pages and not new_candidates:
                raise IncompleteResult("person search pagination did not advance")
            if page_number + 1 >= total_pages:
                if received_count != total_elements:
                    raise IncompleteResult("person search page metadata claims a different result count")
                return PersonSearchResult(candidates=list(candidates.values()), complete=True)

        assert expected_pages is not None
        return PersonSearchResult(
            candidates=[candidate.model_copy(update={"incomplete": True}) for candidate in candidates.values()],
            complete=False,
            next_page=self.max_pages if self.max_pages < expected_pages else None,
        )

    async def resolve(
        self,
        query: str,
        *,
        identity: str,
        token: str,
        selected_person_id: str | None = None,
    ) -> str:
        """Resolve a unique result or a caller-selected ID from a complete search."""
        result = await self.search(query, identity=identity, token=token)
        if not result.complete:
            raise IncompleteResult("person search exceeded its page limit; selection is unsafe")
        if not result.candidates:
            raise PersonNotFound("No person matched the complete search")
        if selected_person_id is not None:
            if selected_person_id not in {candidate.person_id for candidate in result.candidates}:
                raise AmbiguousPerson(result.candidates)
            return selected_person_id
        if len(result.candidates) != 1:
            raise AmbiguousPerson(result.candidates)
        return result.candidates[0].person_id

    def _validate_page(
        self, raw: Mapping[str, Any], expected_number: int
    ) -> tuple[list[Any], int, int]:
        if not isinstance(raw, Mapping):
            raise InvalidUpstreamResponse("person search page must be an object")
        items = raw.get("items")
        metadata = raw.get("page")
        if not isinstance(items, list) or not isinstance(metadata, Mapping):
            raise InvalidUpstreamResponse("person search page requires items and page metadata")
        number, total_pages, total_elements = (
            metadata.get("number"), metadata.get("totalPages"), metadata.get("totalElements")
        )
        if any(type(value) is not int or value < 0 for value in (number, total_pages, total_elements)):
            raise InvalidUpstreamResponse("person page metadata must contain non-negative integers")
        number = cast(int, number)
        total_pages = cast(int, total_pages)
        total_elements = cast(int, total_elements)
        if number != expected_number:
            raise InvalidUpstreamResponse("person page number is out of sequence")
        if len(items) > self.page_size:
            raise InvalidUpstreamResponse("person page exceeds the configured page size")
        if total_pages != (ceil(total_elements / self.page_size) if total_elements else 0):
            raise InvalidUpstreamResponse("person page totals conflict with configured page size")
        if (total_pages == 0) != (total_elements == 0) or number >= max(total_pages, 1):
            raise InvalidUpstreamResponse("person page totals are inconsistent")
        if total_pages and number < total_pages - 1 and len(items) != self.page_size:
            raise IncompleteResult("non-final person page is shorter than the configured page size")
        if not total_pages and items:
            raise InvalidUpstreamResponse("empty person search metadata conflicts with items")
        return items, total_pages, total_elements


__all__ = ["PersonPageSource", "PersonResolver", "PersonSearchResult"]
