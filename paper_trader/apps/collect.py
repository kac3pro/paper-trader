"""Collector: archives Warsaw events and their order books, forever.

    uv run python -m paper_trader.apps.collect --data-dir data/raw

Every cycle (default 2 min) it fetches the order books of all tracked brackets.
Every 15 min it also re-fetches the events themselves (which archives market status:
prices, acceptingOrders, closed, ...). Tracked events are today, D+1 and D+2 in
Warsaw local time.

Every response, including errors, goes to the raw archive. A failed request is logged
and skipped; the loop never stops because of one.
"""

import argparse
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from paper_trader.adapters.clob import get_books_response
from paper_trader.adapters.gamma import event_from_response, get_event_response, warsaw_event_slug
from paper_trader.domain.market import Event, parse_event
from paper_trader.store.raw import append_record, record_from_response

log = logging.getLogger("paper_trader.collect")

WARSAW = ZoneInfo("Europe/Warsaw")
SOURCE_EVENTS = "gamma_events"
SOURCE_BOOKS = "clob_books"


@dataclass(frozen=True)
class CollectorState:
    events: tuple[Event, ...] = ()
    events_refreshed_at: datetime | None = None


# --- pure helpers -------------------------------------------------------------------------


def warsaw_today(now: datetime) -> date:
    return now.astimezone(WARSAW).date()


def target_dates(now: datetime, days_ahead: int = 2) -> tuple[date, ...]:
    """Warsaw-local dates whose events we track: today through today + days_ahead."""
    today = warsaw_today(now)
    return tuple(today + timedelta(days=i) for i in range(days_ahead + 1))


def events_due(state: CollectorState, now: datetime, interval: timedelta) -> bool:
    last = state.events_refreshed_at
    return last is None or now - last >= interval


def merge_events(
    old: Iterable[Event], new: Iterable[Event], dates: Iterable[date]
) -> tuple[Event, ...]:
    """Fresh events replace old ones for the same date. If a refresh failed for a date, the
    old event is kept. Events outside `dates` are dropped. Result is sorted by date."""
    wanted = set(dates)
    by_date = {e.target_date: e for e in old} | {e.target_date: e for e in new}
    return tuple(by_date[d] for d in sorted(by_date) if d in wanted)


def yes_token_ids(events: Iterable[Event], today: date) -> list[str]:
    """YES tokens of every bracket of events from `today` onwards."""
    return [b.yes_token_id for e in events if e.target_date >= today for b in e.brackets]


def next_tick(now: datetime, interval: timedelta) -> datetime:
    """The next multiple of `interval` since the epoch, so polls land on round times."""
    step = interval.total_seconds()
    return datetime.fromtimestamp((now.timestamp() // step + 1) * step, tz=UTC)


# --- impure shell -------------------------------------------------------------------------


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


def refresh_events(client: httpx.Client, root: Path, dates: Iterable[date]) -> list[Event]:
    """Fetch, archive and parse the event for each date. Dates that fail are skipped."""
    events = []
    for d in dates:
        slug = warsaw_event_slug(d)
        response = fetch_and_archive(root, SOURCE_EVENTS, partial(get_event_response, client, slug))
        if response is None or response.is_error:
            continue
        try:
            raw = event_from_response(response)
            if raw is None:
                log.info("%s: not listed yet", slug)
                continue
            events.append(parse_event(raw))
        except (ValueError, KeyError) as exc:  # bad JSON or unexpected shape
            log.error("%s: could not parse event: %s", slug, exc)
    return events


def collect_books(client: httpx.Client, root: Path, token_ids: list[str]) -> None:
    if not token_ids:
        log.info("no tracked tokens, skipping books")
        return
    response = fetch_and_archive(root, SOURCE_BOOKS, partial(get_books_response, client, token_ids))
    if response is not None and not response.is_error:
        log.info("archived %d books (%d bytes)", len(token_ids), len(response.content))


def run_cycle(
    client: httpx.Client,
    root: Path,
    state: CollectorState,
    now: datetime,
    event_interval: timedelta,
) -> CollectorState:
    """One collection cycle. Returns the new state; never mutates the old one."""
    if events_due(state, now, event_interval):
        dates = target_dates(now)
        fresh = refresh_events(client, root, dates)
        state = CollectorState(
            events=merge_events(state.events, fresh, dates),
            events_refreshed_at=now,
        )
        log.info("tracking events: %s", [e.target_date.isoformat() for e in state.events])
    collect_books(client, root, yes_token_ids(state.events, warsaw_today(now)))
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive Warsaw events and order books.")
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--book-interval", type=int, default=120, help="seconds")
    parser.add_argument("--event-interval", type=int, default=900, help="seconds")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request at INFO
    book_interval = timedelta(seconds=args.book_interval)
    event_interval = timedelta(seconds=args.event_interval)

    state = CollectorState()
    with httpx.Client(timeout=20) as client:
        while True:
            try:
                state = run_cycle(client, args.data_dir, state, datetime.now(UTC), event_interval)
            except Exception:  # never let one bad cycle kill the collector
                log.exception("cycle failed")
            if args.once:
                return
            now = datetime.now(UTC)
            time.sleep((next_tick(now, book_interval) - now).total_seconds())


if __name__ == "__main__":
    main()
