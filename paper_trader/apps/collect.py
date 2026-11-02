"""Collector: archives Warsaw events, their order books and EPWA METARs, forever.

    uv run python -m paper_trader.apps.collect --data-dir data/raw

Every cycle (default 2 min) it fetches the order books of all tracked brackets.
Every 15 min it also re-fetches the events themselves (which archives market status:
prices, acceptingOrders, closed, ...). Tracked events are today, D+1 and D+2 in
Warsaw local time. Every 10 min it fetches the last 6 hours of EPWA METARs.

Every response, including errors, goes to the raw archive. A failed request is logged
and skipped; the loop never stops because of one.
"""

import argparse
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx

from paper_trader.adapters.clob import get_books_response
from paper_trader.adapters.gamma import event_from_response, get_event_response, warsaw_event_slug
from paper_trader.adapters.metar import get_metar_response
from paper_trader.domain.market import Event, parse_event
from paper_trader.domain.time import next_tick, warsaw_date
from paper_trader.store.raw import fetch_and_archive

log = logging.getLogger("paper_trader.collect")

SOURCE_EVENTS = "gamma_events"
SOURCE_BOOKS = "clob_books"
SOURCE_METAR = "metar_epwa"


@dataclass(frozen=True)
class Schedule:
    """How often the slower sources are fetched. Books are fetched every cycle."""

    events: timedelta = timedelta(minutes=15)
    metar: timedelta = timedelta(minutes=10)


@dataclass(frozen=True)
class CollectorState:
    events: tuple[Event, ...] = ()
    events_refreshed_at: datetime | None = None
    metar_fetched_at: datetime | None = None


# --- pure helpers -------------------------------------------------------------------------


def target_dates(now: datetime, days_ahead: int = 2) -> tuple[date, ...]:
    """Warsaw-local dates whose events we track: today through today + days_ahead."""
    today = warsaw_date(now)
    return tuple(today + timedelta(days=i) for i in range(days_ahead + 1))


def is_due(last: datetime | None, now: datetime, interval: timedelta) -> bool:
    """True if something last done at `last` (None = never) should be done again."""
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


# --- impure shell -------------------------------------------------------------------------


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


def collect_metar(client: httpx.Client, root: Path) -> None:
    response = fetch_and_archive(root, SOURCE_METAR, partial(get_metar_response, client))
    if response is not None and not response.is_error:
        log.info("archived %d METAR lines", len(response.text.splitlines()))


def run_cycle(
    client: httpx.Client,
    root: Path,
    state: CollectorState,
    now: datetime,
    schedule: Schedule,
) -> CollectorState:
    """One collection cycle. Returns the new state; never mutates the old one.

    A failed fetch still counts as done: it is retried at its next interval, not every cycle.
    """
    if is_due(state.events_refreshed_at, now, schedule.events):
        dates = target_dates(now)
        fresh = refresh_events(client, root, dates)
        state = replace(
            state, events=merge_events(state.events, fresh, dates), events_refreshed_at=now
        )
        log.info("tracking events: %s", [e.target_date.isoformat() for e in state.events])
    collect_books(client, root, yes_token_ids(state.events, warsaw_date(now)))
    if is_due(state.metar_fetched_at, now, schedule.metar):
        collect_metar(client, root)
        state = replace(state, metar_fetched_at=now)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive Warsaw events, books and METARs.")
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--book-interval", type=int, default=120, help="seconds")
    parser.add_argument("--event-interval", type=int, default=900, help="seconds")
    parser.add_argument("--metar-interval", type=int, default=600, help="seconds")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request at INFO
    book_interval = timedelta(seconds=args.book_interval)
    schedule = Schedule(
        events=timedelta(seconds=args.event_interval),
        metar=timedelta(seconds=args.metar_interval),
    )

    state = CollectorState()
    with httpx.Client(timeout=20) as client:
        while True:
            try:
                state = run_cycle(client, args.data_dir, state, datetime.now(UTC), schedule)
            except Exception:  # never let one bad cycle kill the collector
                log.exception("cycle failed")
            if args.once:
                return
            now = datetime.now(UTC)
            time.sleep((next_tick(now, book_interval) - now).total_seconds())


if __name__ == "__main__":
    main()
