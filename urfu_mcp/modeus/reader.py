"""Orchestrate per-person Modeus queries and preserve schedule ownership."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from .intervals import DateInterval
from .models import Event, ScheduleResult
from .normalizer import ScheduleNormalizer


class EventGateway(Protocol):
    """Minimal transport contract needed by the schedule reader."""

    async def search_events(
        self,
        person_id: str,
        time_min: datetime,
        time_max: datetime,
        *,
        token: str,
    ) -> dict[str, Any]: ...


class ScheduleReader:
    """Fetch a complete schedule separately for every authorized person ID."""

    def __init__(
        self,
        gateway: EventGateway,
        *,
        max_days: int,
        max_subjects: int,
    ) -> None:
        if max_days <= 0:
            raise ValueError("max_days must be positive")
        if max_subjects <= 0:
            raise ValueError("max_subjects must be positive")
        self._gateway = gateway
        self._max_days = max_days
        self._max_subjects = max_subjects

    async def read(
        self,
        subjects: Sequence[str],
        interval: DateInterval,
        *,
        token: str,
    ) -> list[ScheduleResult]:
        """Read schedules without merging people or masking incomplete responses."""
        person_ids = self._validate_person_ids(subjects)
        if len(person_ids) > self._max_subjects:
            raise ValueError("requested people exceed configured limit")
        if interval.end - interval.start > timedelta(days=self._max_days):
            raise ValueError("requested period exceeds configured limit")
        if not token:
            raise ValueError("token must not be empty")

        results: list[ScheduleResult] = []
        for person_id in person_ids:
            raw = await self._gateway.search_events(
                person_id,
                interval.start,
                interval.end,
                token=token,
            )
            result = ScheduleNormalizer.normalize(
                raw,
                requested_person_id=person_id,
                from_=interval.start,
                to=interval.end,
            )
            events = self._filter_deduplicate_and_sort(
                result.events,
                start=interval.start,
                end=interval.end,
            )
            results.append(result.model_copy(update={"events": events}))
        return results

    @staticmethod
    def _validate_person_ids(subjects: Sequence[str]) -> list[str]:
        if not subjects:
            raise ValueError("at least one person ID is required")
        if len(set(subjects)) != len(subjects):
            raise ValueError("person IDs must be unique")

        normalized: list[str] = []
        for person_id in subjects:
            try:
                parsed = UUID(person_id)
            except (ValueError, AttributeError, TypeError) as exc:
                raise ValueError("person ID must be a UUID") from exc
            if parsed.int == 0:
                raise ValueError("person ID must not be the nil UUID")
            normalized.append(str(parsed))
        return normalized

    @staticmethod
    def _filter_deduplicate_and_sort(
        events: Sequence[Event],
        *,
        start: datetime,
        end: datetime,
    ) -> list[Event]:
        by_id = {
            event.id: event
            for event in events
            if event.starts_at < end and event.ends_at > start
        }
        return sorted(by_id.values(), key=lambda event: (event.starts_at, event.id))
