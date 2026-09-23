"""Normalize complete Modeus event-search payloads without network or auth.

Public API: ``ScheduleNormalizer.normalize(raw, requested_person_id=..., from_=..., to=...)``.
``raw`` may be a parsed mapping, a JSON object string, or the JSON string emitted
by ASP.NET ``Ok(string)`` (JSON containing another JSON-encoded string).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlparse

from pydantic import ValidationError

from urfu_mcp.modeus.models import Event, Organizer, Room, ScheduleResult

from .errors import IncompleteResult, InvalidUpstreamResponse


class ScheduleNormalizer:
    """Convert complete Modeus event-search responses to internal models."""

    @classmethod
    def normalize(
        cls,
        raw: Mapping[str, Any] | str | bytes,
        *,
        requested_person_id: str,
        from_: datetime,
        to: datetime,
    ) -> ScheduleResult:
        """Parse and validate a Modeus payload, refusing absent/truncated events.

        The requested person and interval are explicit so the result can never
        be attributed to a fabricated identity or host-local time range.
        """
        payload = cls._decode(raw)
        if not isinstance(payload, Mapping):
            raise InvalidUpstreamResponse("response must be a JSON object")
        events_data = payload.get("events")
        if not isinstance(events_data, list):
            raise InvalidUpstreamResponse("response must contain an events list")
        embedded = payload.get("_embedded", {})
        if embedded is None:
            embedded = {}
        if not isinstance(embedded, Mapping):
            raise InvalidUpstreamResponse("_embedded must be an object")
        page = payload.get("page")
        cls._validate_page(page, len(events_data))

        try:
            normalized_events = [cls._event(item, embedded) for item in events_data]
            return ScheduleResult(
                requested_person_id=requested_person_id,
                from_=from_,
                to=to,
                events=normalized_events,
            )
        except (ValidationError, TypeError, ValueError, KeyError) as exc:
            raise InvalidUpstreamResponse(f"invalid event response: {exc}") from exc

    @staticmethod
    def _decode(raw: Mapping[str, Any] | str | bytes) -> Any:
        value: Any = raw
        for _ in range(2):
            if isinstance(value, bytes):
                try:
                    value = value.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise InvalidUpstreamResponse("response is not UTF-8") from exc
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError as exc:
                    raise InvalidUpstreamResponse("response contains malformed JSON") from exc
                continue
            break
        if isinstance(value, str):
            raise InvalidUpstreamResponse("response contains too many JSON string layers")
        return value

    @staticmethod
    def _validate_page(page: Any, event_count: int) -> None:
        if not isinstance(page, Mapping):
            raise InvalidUpstreamResponse("response must contain page metadata")
        try:
            total_elements = page["totalElements"]
            total_pages = page["totalPages"]
            number = page["number"]
        except KeyError as exc:
            raise InvalidUpstreamResponse("page metadata is incomplete") from exc
        if any(type(value) is not int or value < 0 for value in (total_elements, total_pages, number)):
            raise InvalidUpstreamResponse("page metadata values must be non-negative integers")
        if total_elements > event_count or (total_pages > 0 and number + 1 < total_pages):
            raise IncompleteResult("Modeus response contains only part of the result")
        if (total_elements == 0 and event_count != 0) or (total_pages == 0 and event_count != 0):
            raise InvalidUpstreamResponse("page metadata conflicts with events")
        if total_pages > 0 and number >= total_pages:
            raise InvalidUpstreamResponse("page metadata conflicts with page count")

    @classmethod
    def _event(cls, item: Any, embedded: Mapping[str, Any]) -> Event:
        if not isinstance(item, Mapping):
            raise InvalidUpstreamResponse("each event must be an object")
        event_id = item.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise InvalidUpstreamResponse("event id is required")
        event_links = item.get("_links", {})
        if not isinstance(event_links, Mapping):
            event_links = {}
        realization_id = cls._link_id(event_links.get("course-unit-realization"))
        realizations = cls._array(embedded, "course-unit-realizations")
        subject = cls._by_id(realizations, realization_id).get("name") if realization_id else None

        locations = [row for row in cls._array(embedded, "event-locations") if row.get("eventId") == event_id]
        event_rooms = cls._array(embedded, "event-rooms")
        rooms_by_id = {row.get("id"): row for row in event_rooms}
        room_data = cls._array(embedded, "rooms")
        rooms: list[Room] = []
        seen_room_ids: set[str] = set()
        location_text: str | None = None
        for location in locations:
            if location_text is None:
                location_text = cls._string(location.get("customLocation"))
            location_links = location.get("_links", {})
            room_ids = cls._link_ids(location_links.get("event-rooms")) if isinstance(location_links, Mapping) else []
            for event_room_id in room_ids:
                event_room = rooms_by_id.get(event_room_id)
                if not event_room:
                    continue
                room_links = event_room.get("_links", {})
                room_link = room_links.get("room", {}) if isinstance(room_links, Mapping) else {}
                room_id = cls._link_id(room_link)
                room = cls._by_id(room_data, room_id)
                if room_id and room_id not in seen_room_ids:
                    rooms.append(Room(id=room_id, name=cls._first_string(room, "name", "nameShort")))
                    seen_room_ids.add(room_id)

        organizers = [row for row in cls._array(embedded, "event-organizers") if row.get("eventId") == event_id]
        attendees_rows = cls._array(embedded, "event-attendees")
        people = cls._array(embedded, "persons")
        attendee_by_id = {row.get("id"): row for row in attendees_rows}
        persons_by_id = {row.get("id"): row for row in people}
        normalized_organizers: list[Organizer] = []
        seen_organizer_ids: set[str] = set()
        for organizer in organizers:
            links = organizer.get("_links", {})
            attendee_ids = ScheduleNormalizer._link_ids(links.get("event-attendees")) if isinstance(links, Mapping) else []
            for attendee_id in attendee_ids:
                attendee = attendee_by_id.get(attendee_id)
                if not attendee:
                    continue
                person_id = cls._link_id(attendee.get("_links", {}).get("person", {}))
                person = persons_by_id.get(person_id)
                if person_id and person and person_id not in seen_organizer_ids:
                    normalized_organizers.append(
                        Organizer(
                            id=person_id,
                            full_name=cls._first_string(person, "fullName", "name"),
                        )
                    )
                    seen_organizer_ids.add(person_id)

        return Event(
            id=event_id,
            starts_at=cast(datetime, item.get("start")),
            ends_at=cast(datetime, item.get("end")),
            subject=subject if isinstance(subject, str) else None,
            title=cls._first_string(item, "title", "name"),
            type_id=cls._string(item.get("typeId")),
            organizers=normalized_organizers,
            rooms=rooms,
            location_text=location_text,
            meeting_url=None,
            status=cls._string(item.get("status")),
        )

    @staticmethod
    def _array(embedded: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
        value = embedded.get(key, [])
        if value is None:
            return []
        if not isinstance(value, list):
            raise InvalidUpstreamResponse(f"_embedded.{key} must be a list")
        if not all(isinstance(row, Mapping) for row in value):
            raise InvalidUpstreamResponse(f"_embedded.{key} entries must be objects")
        return value

    @staticmethod
    def _link_ids(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, Mapping):
            values: Sequence[Any] = [value]
        elif isinstance(value, list):
            values = value
        else:
            return []
        return [link_id for link in values if (link_id := ScheduleNormalizer._link_id(link))]

    @staticmethod
    def _link_id(link: Any) -> str | None:
        if not isinstance(link, Mapping):
            return None
        href = link.get("href")
        if not isinstance(href, str) or not href:
            return None
        path = urlparse(href).path.rstrip("/")
        return path.rsplit("/", 1)[-1] or None

    @staticmethod
    def _by_id(rows: list[Mapping[str, Any]], identifier: str | None) -> Mapping[str, Any]:
        if identifier is None:
            return {}
        return next((row for row in rows if row.get("id") == identifier), {})

    @staticmethod
    def _first_string(row: Mapping[str, Any], *keys: str) -> str | None:
        return next((row[key] for key in keys if isinstance(row.get(key), str)), None)

    @staticmethod
    def _string(value: Any) -> str | None:
        return value if isinstance(value, str) else None
