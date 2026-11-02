"""Pure conversions from raw API payloads to warehouse rows (tuples in table column order).

High-volume, purely structural unpacking (order books, market status) is done in SQL in
store/db.py. Anything with real parsing rules lives here, reusing the domain parsers.
"""

import hashlib
from datetime import datetime
from typing import Any

from paper_trader.domain.market import parse_event
from paper_trader.domain.metar import parse_metar


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def event_rows(
    raw: dict[str, Any], first_seen_at: datetime, last_seen_at: datetime
) -> tuple[tuple, list[tuple]]:
    """Rows for `events` and `brackets` from one raw Gamma event object."""
    event = parse_event(raw)
    event_row = (
        event.event_id,
        event.slug,
        event.target_date,
        event.title,
        event.neg_risk,
        event.start_date,
        event.end_date,
        raw.get("resolutionSource"),
        _hash(raw.get("description", "")),
        first_seen_at,
        last_seen_at,
    )
    bracket_rows = [
        (
            b.market_id,
            b.condition_id,
            event.event_id,
            b.label,
            b.lo_c,
            b.hi_c,
            b.yes_token_id,
            b.no_token_id,
        )
        for b in event.brackets
    ]
    return event_row, bracket_rows


def metar_row(line: str, first_seen_at: datetime) -> tuple:
    """Row for `metar_obs` from one raw METAR line. Raises ValueError if unreadable."""
    r = parse_metar(line, first_seen_at)
    return (
        r.station,
        r.obs_time,
        r.local_date,
        r.report_type,
        r.is_correction,
        r.temp_body_c,
        r.temp_tgroup_c,
        r.raw,
        _hash(r.raw),
        first_seen_at,
    )
