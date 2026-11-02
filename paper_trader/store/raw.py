"""Raw archive: every API response we receive, stored as-is, append-only.

Layout (all times UTC):

    <root>/<source>/<YYYY-MM-DD>/<HH>.jsonl.gz

Each line is one RawRecord as JSON. The response body is kept as the exact text
the server sent (not parsed), so error pages and empty bodies are archived too,
and normalizers can be re-run later against the original data.

Each record is appended as its own gzip member. Concatenated gzip members form a
valid gzip file (Python's gzip module and DuckDB's read_json both read it), and a
crash mid-write can only damage the last record, never earlier ones.
"""

import gzip
import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_SOURCE_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class RawRecord:
    source: str  # e.g. "gamma_events", "clob_books"
    fetched_at: datetime  # when we received the response, timezone-aware
    url: str  # full URL including query parameters
    status: int  # HTTP status code
    body: str  # response body exactly as received


def record_from_response(source: str, response: httpx.Response, fetched_at: datetime) -> RawRecord:
    """Capture an HTTP response as a RawRecord. Error responses are captured too."""
    return RawRecord(
        source=source,
        fetched_at=fetched_at,
        url=str(response.request.url),
        status=response.status_code,
        body=response.text,
    )


def to_json_line(record: RawRecord) -> str:
    return json.dumps(
        {
            "source": record.source,
            "fetched_at": record.fetched_at.isoformat(),
            "url": record.url,
            "status": record.status,
            "body": record.body,
        },
        ensure_ascii=False,
    )


def from_json_line(line: str) -> RawRecord:
    data = json.loads(line)
    return RawRecord(
        source=data["source"],
        fetched_at=datetime.fromisoformat(data["fetched_at"]),
        url=data["url"],
        status=data["status"],
        body=data["body"],
    )


def archive_path(root: Path, source: str, fetched_at: datetime) -> Path:
    """Where a record fetched at `fetched_at` belongs. Partitioned by UTC date and hour."""
    if not _SOURCE_RE.match(source):
        raise ValueError(f"source must match [a-z0-9_]+, got {source!r}")
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    utc = fetched_at.astimezone(UTC)
    return root / source / utc.strftime("%Y-%m-%d") / f"{utc:%H}.jsonl.gz"


def append_record(root: Path, record: RawRecord) -> Path:
    """Append one record to the archive and return the file it went into."""
    path = archive_path(root, record.source, record.fetched_at)
    path.parent.mkdir(parents=True, exist_ok=True)
    member = gzip.compress((to_json_line(record) + "\n").encode())
    with path.open("ab") as f:
        f.write(member)  # one write call per record keeps a crash from splitting it
    return path


def read_records(path: Path) -> Iterator[RawRecord]:
    """Read records back in the order they were written.

    A truncated last record (from a crash mid-write) is skipped; everything before it is
    still returned.
    """
    with gzip.open(path, "rt", encoding="utf-8") as f:
        try:
            for line in f:
                yield from_json_line(line)
        except (EOFError, gzip.BadGzipFile, json.JSONDecodeError):
            return


def fetch_and_archive(
    root: Path, source: str, request: Callable[[], httpx.Response]
) -> httpx.Response | None:
    """Run one request and archive the response. Returns None if no response arrived."""
    try:
        response = request()
    except httpx.HTTPError as exc:
        log.warning("%s: request failed: %s", source, exc)
        return None
    append_record(root, record_from_response(source, response, datetime.now(UTC)))
    if response.is_error:
        log.warning("%s: HTTP %s", source, response.status_code)
    return response
