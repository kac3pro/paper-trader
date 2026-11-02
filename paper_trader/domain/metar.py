"""Parsing raw METAR lines into the fields the market's resolution rules care about.

Only what the rules need is extracted: report type (SPECI doesn't count), observation
time, correction flag, and temperature, from the hourly T-group remark if present,
otherwise from the body (whole degrees).
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from paper_trader.domain.time import warsaw_date

_HEADER_RE = re.compile(
    r"^(?:(?P<type>METAR|SPECI)\s+)?(?:COR\s+)?(?P<station>[A-Z]{4})\s+"
    r"(?P<day>\d{2})(?P<hour>\d{2})(?P<minute>\d{2})Z\b"
)
# Body temperature/dew point, e.g. "08/03", "M02/M05", "12/" or "12///".
_BODY_TEMP_RE = re.compile(r"(?<=\s)(M?\d{2})/(?:M?\d{2}|//)?(?=\s|$)")
# Hourly remark: T + sign digit + 3 digits (tenths °C) + same for dew point.
_TGROUP_RE = re.compile(r"(?<=\s)T([01])(\d{3})[01]\d{3}(?=\s|$)")


@dataclass(frozen=True)
class MetarReport:
    raw: str
    station: str
    report_type: str | None  # "METAR", "SPECI", or None if the line has no label
    obs_time: datetime  # UTC
    is_correction: bool
    temp_body_c: int | None  # whole degrees from the body group
    temp_tgroup_c: Decimal | None  # tenths from the T-group remark (EPWA doesn't send it)

    @property
    def local_date(self) -> date:
        return warsaw_date(self.obs_time)


def obs_time_from_day(day: int, hour: int, minute: int, seen_at: datetime) -> datetime:
    """Turn a METAR "DDHHMMZ" into a full UTC datetime.

    METARs only carry the day of the month, so month and year come from when we saw the
    report: the latest matching day that isn't after `seen_at` (one hour of slack for
    clock differences).
    """
    year, month = seen_at.year, seen_at.month
    for _ in range(3):  # this month, or one of the two before (e.g. day 31 in a 30-day month)
        try:
            candidate = datetime(year, month, day, hour, minute, tzinfo=UTC)
        except ValueError:  # day doesn't exist in this month
            candidate = None
        if candidate is not None and candidate <= seen_at + timedelta(hours=1):
            return candidate
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    raise ValueError(f"no valid date for day {day} before {seen_at}")


def _signed_temp(group: str) -> int:
    """'08' -> 8, 'M02' -> -2, 'M00' -> 0."""
    return -int(group[1:]) if group.startswith("M") else int(group)


def parse_metar(line: str, seen_at: datetime) -> MetarReport:
    """Parse one raw METAR line. Raises ValueError if the header is unreadable."""
    raw = line.strip()
    header = _HEADER_RE.match(raw)
    if header is None:
        raise ValueError(f"unrecognised METAR line: {raw!r}")

    body, _, remarks = raw.partition(" RMK ")
    body_temp = _BODY_TEMP_RE.search(body)
    tgroup = _TGROUP_RE.search(" " + remarks) if remarks else None

    return MetarReport(
        raw=raw,
        station=header["station"],
        report_type=header["type"],
        obs_time=obs_time_from_day(
            int(header["day"]), int(header["hour"]), int(header["minute"]), seen_at
        ),
        is_correction=" COR " in f" {body} ",
        temp_body_c=_signed_temp(body_temp[1]) if body_temp else None,
        temp_tgroup_c=(
            (-1 if tgroup[1] == "1" else 1) * Decimal(tgroup[2]) / 10 if tgroup else None
        ),
    )
