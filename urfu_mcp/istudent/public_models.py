"""Internal normalized BRS models and the stable public JSON projection."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import InvalidUpstreamResponse


class BRSPointsStatus(StrEnum):
    """Source-confirmed interpretation of earned points on one row."""

    AVAILABLE = "available"
    NOT_REPORTED = "not_reported"
    UNAVAILABLE = "unavailable"


class BRSCompleteness(StrEnum):
    """Parser assertion about trusted row coverage, never inferred by this module."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class BRSContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BRSPeriod(BRSContractModel):
    """Period metadata only when explicitly supported by the source."""

    label: str | None
    starts_on: date | None
    ends_on: date | None

    @model_validator(mode="after")
    def bounds_are_ordered(self) -> BRSPeriod:
        if self.starts_on and self.ends_on and self.ends_on < self.starts_on:
            raise ValueError("period end must not precede period start")
        return self


class BRSSubject(BRSContractModel):
    """One source-confirmed subject and its semantically mapped score values."""

    subject_name: str = Field(min_length=1)
    earned_points: Decimal | None
    maximum_points: Decimal | None
    points_status: BRSPointsStatus
    updated_at: datetime | None

    @model_validator(mode="after")
    def points_agree_with_status(self) -> BRSSubject:
        if self.points_status == BRSPointsStatus.AVAILABLE and self.earned_points is None:
            raise ValueError("available points require an earned-points value")
        if self.points_status != BRSPointsStatus.AVAILABLE and self.earned_points is not None:
            raise ValueError("non-available points must not contain earned points")
        if self.updated_at is not None and self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must include its source timezone offset")
        return self


class BRSResult(BRSContractModel):
    """Normalized internal source data; it is not itself the public JSON schema."""

    period: BRSPeriod
    subjects: tuple[BRSSubject, ...]
    completeness: BRSCompleteness


def _json_number(value: Decimal | None) -> int | float | None:
    """Encode representable Decimal values as JSON numbers without loss."""
    if value is None:
        return None
    if not value.is_finite():
        raise InvalidUpstreamResponse("BRS points contain an invalid numeric value")
    if value == value.to_integral_value():
        return int(value)
    converted = float(value)
    if not isfinite(converted):
        raise InvalidUpstreamResponse("BRS points exceed the supported numeric range")
    # Python's float repr is the shortest decimal that round-trips to that
    # binary float. Compare it numerically with the original Decimal so JSON
    # serialization cannot silently publish a rounded score.
    if Decimal(str(converted)) != value:
        raise InvalidUpstreamResponse("BRS points cannot be represented without loss")
    return converted


def public_brs_payload(
    result: BRSResult,
    *,
    selector: str,
    selected_subjects: tuple[BRSSubject, ...],
    as_of: date,
) -> dict[str, object]:
    """Project exact v0.8 public fields; omit all internal/source-only details."""
    return {
        "as_of": as_of.isoformat(),
        "timezone": "Asia/Yekaterinburg",
        "period": {
            "label": result.period.label,
            "starts_on": result.period.starts_on.isoformat() if result.period.starts_on else None,
            "ends_on": result.period.ends_on.isoformat() if result.period.ends_on else None,
        },
        "subject_name": selector,
        "subjects": [
            {
                "subject_name": subject.subject_name,
                "earned_points": _json_number(subject.earned_points),
                "maximum_points": _json_number(subject.maximum_points),
                "points_status": subject.points_status.value,
                "updated_at": subject.updated_at.isoformat() if subject.updated_at else None,
            }
            for subject in selected_subjects
        ],
        "completeness": result.completeness.value,
        "source": "istudent_brs",
    }
