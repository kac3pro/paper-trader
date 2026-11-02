import gzip
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from paper_trader.store.raw import (
    RawRecord,
    append_record,
    archive_path,
    from_json_line,
    read_records,
    record_from_response,
    to_json_line,
)

T0 = datetime(2026, 11, 2, 9, 30, 15, tzinfo=UTC)


def _record(fetched_at: datetime = T0, body: str = '[{"id": "1"}]') -> RawRecord:
    return RawRecord(
        source="gamma_events",
        fetched_at=fetched_at,
        url="https://gamma-api.polymarket.com/events?slug=x",
        status=200,
        body=body,
    )


def test_archive_path_partitions_by_utc_date_and_hour():
    path = archive_path(Path("/data/raw"), "gamma_events", T0)
    assert path == Path("/data/raw/gamma_events/2026-11-02/09.jsonl.gz")


def test_archive_path_converts_to_utc():
    # 00:30 in Warsaw (CET, UTC+1) is still 23:30 on the previous day in UTC.
    warsaw_winter = timezone(timedelta(hours=1))
    path = archive_path(Path("/r"), "metar", datetime(2026, 11, 3, 0, 30, tzinfo=warsaw_winter))
    assert path == Path("/r/metar/2026-11-02/23.jsonl.gz")


def test_archive_path_rejects_naive_datetime():
    with pytest.raises(ValueError):
        archive_path(Path("/r"), "metar", datetime(2026, 11, 2, 9, 0))


@pytest.mark.parametrize("source", ["", "../etc", "Gamma", "clob/books"])
def test_archive_path_rejects_bad_source_names(source):
    with pytest.raises(ValueError):
        archive_path(Path("/r"), source, T0)


def test_json_line_round_trip():
    record = _record(body='{"temp": "−2°C"}')  # non-ASCII must survive
    assert from_json_line(to_json_line(record)) == record


def test_append_and_read_back_in_order(tmp_path):
    first = _record(T0, body="first")
    second = _record(T0 + timedelta(minutes=2), body="second")

    path = append_record(tmp_path, first)
    assert append_record(tmp_path, second) == path  # same hour, same file

    assert list(read_records(path)) == [first, second]


def test_new_hour_goes_to_new_file(tmp_path):
    a = append_record(tmp_path, _record(T0))
    b = append_record(tmp_path, _record(T0 + timedelta(hours=1)))
    assert a != b


def test_truncated_last_record_is_skipped(tmp_path):
    good = _record(T0, body="good")
    path = append_record(tmp_path, good)
    broken = gzip.compress(b'{"source": "gamma_events", "fetched_at": "2026-11-02T09:31"}\n')
    with path.open("ab") as f:
        f.write(broken[: len(broken) // 2])  # simulate a crash halfway through a write

    assert list(read_records(path)) == [good]


def _respond(status: int, body: str) -> httpx.Response:
    transport = httpx.MockTransport(lambda _: httpx.Response(status, text=body))
    with httpx.Client(transport=transport) as client:
        return client.get("https://example.test/events", params={"slug": "abc"})


def test_record_from_response_keeps_query_and_body():
    record = record_from_response("gamma_events", _respond(200, '[{"id": "1"}]'), T0)
    assert record.url == "https://example.test/events?slug=abc"
    assert record.status == 200
    assert record.body == '[{"id": "1"}]'


@pytest.mark.parametrize(("status", "body"), [(500, "Internal Server Error"), (200, "")])
def test_record_from_response_captures_errors_and_empty_bodies(status, body):
    record = record_from_response("gamma_events", _respond(status, body), T0)
    assert (record.status, record.body) == (status, body)
