"""Time zone helpers. Everything is stored in UTC; the market day is Warsaw local time."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


def warsaw_date(moment: datetime) -> date:
    """The Warsaw local date of a timezone-aware moment (handles CET/CEST)."""
    return moment.astimezone(WARSAW).date()
