"""Validated internal data models for normalized Modeus responses."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class NormalizedModel(BaseModel):
    """Base for immutable, extra-tolerant normalized upstream records."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class Room(NormalizedModel):
    """A room linked to an event; name is absent when upstream omits it."""

    id: str
    name: str | None = None


class Teacher(NormalizedModel):
    """A person whose teacher role has been verified by the upstream contract."""

    id: str
    full_name: str | None = None


class Organizer(NormalizedModel):
    """A person linked as an organizer; this role is not assumed to be teacher."""

    id: str
    full_name: str | None = None


class Event(NormalizedModel):
    """One normalized event; upstream's optional descriptive fields stay nullable."""

    id: str
    starts_at: datetime
    ends_at: datetime
    subject: str | None = None
    title: str | None = None
    type_id: str | None = None
    teachers: list[Teacher] = Field(default_factory=list)
    organizers: list[Organizer] = Field(default_factory=list)
    rooms: list[Room] = Field(default_factory=list)
    location_text: str | None = None
    meeting_url: str | None = None
    status: str | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone offset")
        return value

    @model_validator(mode="after")
    def require_positive_duration(self) -> Event:
        if self.ends_at <= self.starts_at:
            raise ValueError("event end must be after event start")
        return self


class ScheduleResult(NormalizedModel):
    """Complete schedule for one requested person and a half-open interval."""

    requested_person_id: str
    from_: datetime
    to: datetime
    timezone: str = "Asia/Yekaterinburg"
    events: list[Event]
    completeness: Literal["complete"] = "complete"
    source: Literal["modeus"] = "modeus"

    @field_validator("from_", "to")
    @classmethod
    def require_aware_interval(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("interval boundary must include a timezone offset")
        return value

    @model_validator(mode="after")
    def require_positive_interval(self) -> ScheduleResult:
        if self.to <= self.from_:
            raise ValueError("schedule end must be after schedule start")
        return self


class PersonCandidate(NormalizedModel):
    """A person search match; context is populated only when upstream supplies it."""

    person_id: str = Field(min_length=1)
    full_name: str = Field(min_length=1)
    context: dict[str, str] | None = None
    incomplete: bool = False

    @field_validator("person_id", "full_name")
    @classmethod
    def require_nonblank_identity_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("person ID and full name must not be blank")
        return value


class EventParticipants(NormalizedModel):
    """Attendees for one event, separate from ordinary schedule results."""

    event_id: str
    attendees: list[PersonCandidate]
    source: str
    completeness: Literal["complete", "incomplete"]
