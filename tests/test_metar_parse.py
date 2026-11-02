from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from paper_trader.domain.metar import obs_time_from_day, parse_metar

SEEN = datetime(2026, 11, 2, 15, 7, tzinfo=UTC)


def test_parse_routine_report():
    r = parse_metar("METAR EPWA 021500Z 03006KT 360V060 9999 FEW024 BKN036 08/03 Q1029 NOSIG", SEEN)
    assert r.station == "EPWA"
    assert r.report_type == "METAR"
    assert r.obs_time == datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
    assert r.local_date == date(2026, 11, 2)
    assert r.temp_body_c == 8
    assert r.temp_tgroup_c is None
    assert r.is_correction is False


@pytest.mark.parametrize(
    ("line", "temp"),
    [
        ("METAR EPWA 020600Z 27004KT 9999 BKN010 M02/M05 Q1020", -2),
        ("METAR EPWA 020600Z 27004KT 9999 BKN010 M00/M01 Q1020", 0),
        ("METAR EPWA 020600Z 27004KT 9999 BKN010 12/// Q1020", 12),
        ("METAR EPWA 020600Z 27004KT 9999 BKN010 Q1020", None),
    ],
)
def test_body_temperature(line, temp):
    assert parse_metar(line, SEEN).temp_body_c == temp


def test_tgroup_remark():
    line = "METAR KJFK 021451Z 31010KT 10SM FEW250 14/M01 A3012 RMK AO2 SLP199 T01391011"
    r = parse_metar(line, SEEN)
    assert r.temp_body_c == 14
    assert r.temp_tgroup_c == Decimal("13.9")


def test_negative_tgroup_remark():
    line = "METAR KJFK 021451Z 31010KT 10SM FEW250 M02/M05 A3012 RMK AO2 T10171050"
    assert parse_metar(line, SEEN).temp_tgroup_c == Decimal("-1.7")


def test_speci_and_correction():
    r = parse_metar("SPECI COR EPWA 021512Z 03015G28KT 9999 BKN030 08/03 Q1029", SEEN)
    assert r.report_type == "SPECI"
    assert r.is_correction is True


def test_line_without_label_is_parsed():
    r = parse_metar("EPWA 021500Z 03006KT 9999 BKN036 08/03 Q1029", SEEN)
    assert r.report_type is None
    assert r.obs_time == datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


def test_local_date_differs_from_utc_date_late_in_the_evening():
    # 23:30 UTC on Nov 2 is 00:30 on Nov 3 in Warsaw (CET).
    seen = datetime(2026, 11, 2, 23, 34, tzinfo=UTC)
    r = parse_metar("METAR EPWA 022330Z 03006KT 9999 BKN036 05/03 Q1029", seen)
    assert r.obs_time.date() == date(2026, 11, 2)
    assert r.local_date == date(2026, 11, 3)


def test_unreadable_line_raises():
    with pytest.raises(ValueError):
        parse_metar("No data available", SEEN)


@pytest.mark.parametrize(
    ("day", "seen_at", "expected"),
    [
        (2, datetime(2026, 11, 2, 15, 7, tzinfo=UTC), datetime(2026, 11, 2, 15, 0, tzinfo=UTC)),
        # Seen just after midnight UTC on Dec 1: day 30 is still November.
        (30, datetime(2026, 12, 1, 0, 5, tzinfo=UTC), datetime(2026, 11, 30, 15, 0, tzinfo=UTC)),
        # Seen Jan 1: day 31 means Dec 31 of the previous year.
        (31, datetime(2027, 1, 1, 0, 5, tzinfo=UTC), datetime(2026, 12, 31, 15, 0, tzinfo=UTC)),
    ],
)
def test_obs_time_from_day(day, seen_at, expected):
    assert obs_time_from_day(day, 15, 0, seen_at) == expected
