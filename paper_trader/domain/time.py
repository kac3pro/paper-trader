"""Time zone helpers. Everything is stored in UTC; the market day is Warsaw local time."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


def warsaw_date(moment: datetime) -> date:
    """The Warsaw local date of a timezone-aware moment (handles CET/CEST)."""
    return moment.astimezone(WARSAW).date()


def next_tick(now: datetime, interval: timedelta) -> datetime:
    """The next multiple of `interval` since the epoch, so polls land on round times."""
    step = interval.total_seconds()
    return datetime.fromtimestamp((now.timestamp() // step + 1) * step, tz=UTC)
