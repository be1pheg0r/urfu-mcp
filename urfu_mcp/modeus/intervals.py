from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

YEKATERINBURG = ZoneInfo("Asia/Yekaterinburg")


@dataclass(frozen=True, slots=True)
class DateInterval:
    """A non-empty half-open interval with explicit timezone-aware endpoints."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("interval endpoints must be timezone-aware")
        object.__setattr__(self, "start", self.start.astimezone(YEKATERINBURG))
        object.__setattr__(self, "end", self.end.astimezone(YEKATERINBURG))
        if self.end <= self.start:
            raise ValueError("interval end must be after start")


def local_date_interval(start_date: date, end_date: date) -> DateInterval:
    """Convert inclusive local calendar dates to [start, day-after-end) in Yekaterinburg."""
    if end_date < start_date:
        raise ValueError("end date must not precede start date")

    start = datetime.combine(start_date, time.min, tzinfo=YEKATERINBURG)
    end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=YEKATERINBURG)
    return DateInterval(start=start, end=end)
