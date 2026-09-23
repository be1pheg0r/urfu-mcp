from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

YEKATERINBURG = ZoneInfo("Asia/Yekaterinburg")


@dataclass(frozen=True, slots=True)
class DateInterval:
    """A non-empty half-open interval with explicit timezone-aware endpoints."""

    start: datetime
    end: datetime
    timezone_name: str = "Asia/Yekaterinburg"

    def __post_init__(self) -> None:
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("interval endpoints must be timezone-aware")
        try:
            timezone = ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone_name must be a valid IANA timezone") from None
        object.__setattr__(self, "start", self.start.astimezone(timezone))
        object.__setattr__(self, "end", self.end.astimezone(timezone))
        if self.end <= self.start:
            raise ValueError("interval end must be after start")


def local_date_interval(
    start_date: date,
    end_date: date,
    timezone_name: str = "Asia/Yekaterinburg",
) -> DateInterval:
    """Convert inclusive local dates to a half-open interval in the given timezone."""
    if end_date < start_date:
        raise ValueError("end date must not precede start date")

    try:
        timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone_name must be a valid IANA timezone") from None
    start = datetime.combine(start_date, time.min, tzinfo=timezone)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone)
    return DateInterval(start=start, end=end, timezone_name=timezone_name)
