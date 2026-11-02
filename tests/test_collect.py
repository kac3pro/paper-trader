import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from paper_trader.apps.collect import (
    SOURCE_BOOKS,
    SOURCE_EVENTS,
    SOURCE_METAR,
    CollectorState,
    Schedule,
    is_due,
    merge_events,
    next_tick,
    run_cycle,
    target_dates,
    yes_token_ids,
)
from paper_trader.domain.market import parse_event
from paper_trader.store.raw import read_records

FIFTEEN_MIN = timedelta(minutes=15)
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
METAR_TEXT = (
    "METAR EPWA 020930Z 03005KT 350V060 9999 FEW020 BKN038 08/03 Q1030 NOSIG\n"
    "METAR EPWA 020900Z 03005KT 9999 FEW020 BKN038 07/03 Q1030 NOSIG\n"
)


@pytest.fixture
def event(gamma_events_response):
    return parse_event(gamma_events_response[0])  # target date 2026-11-02


def test_target_dates_are_warsaw_today_plus_two():
    now = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    assert target_dates(now) == (date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4))


def test_target_dates_roll_over_at_warsaw_midnight_not_utc():
    # 23:30 UTC on Nov 2 is already 00:30 on Nov 3 in Warsaw (CET, UTC+1).
    now = datetime(2026, 11, 2, 23, 30, tzinfo=UTC)
    assert target_dates(now)[0] == date(2026, 11, 3)


def test_is_due():
    assert is_due(None, NOW, FIFTEEN_MIN)  # never done
    assert not is_due(NOW - timedelta(minutes=14), NOW, FIFTEEN_MIN)
    assert is_due(NOW - FIFTEEN_MIN, NOW, FIFTEEN_MIN)


def test_merge_events_prefers_fresh_keeps_old_and_drops_out_of_range(event):
    nov2_old, nov2_new = event, replace(event, title="refreshed")
    nov3_old = replace(event, target_date=date(2026, 11, 3))
    nov1_old = replace(event, target_date=date(2026, 11, 1))
    dates = (date(2026, 11, 2), date(2026, 11, 3))

    merged = merge_events([nov1_old, nov2_old, nov3_old], [nov2_new], dates)

    # Nov 2 refreshed, Nov 3 kept although its refresh failed, Nov 1 dropped.
    assert merged == (nov2_new, nov3_old)


def test_yes_token_ids_skips_past_events(event):
    yesterday = replace(event, target_date=date(2026, 11, 1))
    tokens = yes_token_ids([yesterday, event], today=date(2026, 11, 2))
    assert tokens == [b.yes_token_id for b in event.brackets]


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 2, 10, 0, 0, tzinfo=UTC), datetime(2026, 11, 2, 10, 2, tzinfo=UTC)),
        (datetime(2026, 11, 2, 10, 1, 59, tzinfo=UTC), datetime(2026, 11, 2, 10, 2, tzinfo=UTC)),
    ],
)
def test_next_tick_lands_on_round_times(now, expected):
    assert next_tick(now, timedelta(minutes=2)) == expected


# --- one full cycle against a fake network ------------------------------------------------


def _fake_network(gamma_events_response, seen: list[httpx.Request]) -> httpx.Client:
    listed_slug = gamma_events_response[0]["slug"]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "gamma-api.polymarket.com":
            slug = request.url.params["slug"]
            body = gamma_events_response if slug == listed_slug else []
            return httpx.Response(200, content=json.dumps(body))
        if request.url.host == "clob.polymarket.com" and request.url.path == "/books":
            return httpx.Response(200, content=json.dumps([]))
        if request.url.host == "aviationweather.gov":
            return httpx.Response(200, text=METAR_TEXT)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _sources_archived(root) -> dict[str, int]:
    return {
        src: sum(len(list(read_records(p))) for p in (root / src).glob("*/*.jsonl.gz"))
        for src in (SOURCE_EVENTS, SOURCE_BOOKS, SOURCE_METAR)
    }


def test_first_cycle_fetches_everything(tmp_path, gamma_events_response, event):
    seen: list[httpx.Request] = []

    with _fake_network(gamma_events_response, seen) as client:
        state = run_cycle(client, tmp_path, CollectorState(), NOW, Schedule())

    assert state.events == (event,)  # only Nov 2 is "listed"
    assert state.events_refreshed_at == NOW
    assert state.metar_fetched_at == NOW
    assert _sources_archived(tmp_path) == {SOURCE_EVENTS: 3, SOURCE_BOOKS: 1, SOURCE_METAR: 1}

    books_request = next(r for r in seen if r.url.host == "clob.polymarket.com")
    requested = [item["token_id"] for item in json.loads(books_request.content)]
    assert requested == [b.yes_token_id for b in event.brackets]

    metar_request = next(r for r in seen if r.url.host == "aviationweather.gov")
    assert metar_request.url.params["ids"] == "EPWA"


def test_next_cycle_fetches_only_books(tmp_path, gamma_events_response, event):
    state = CollectorState(events=(event,), events_refreshed_at=NOW, metar_fetched_at=NOW)

    with _fake_network(gamma_events_response, []) as client:
        new_state = run_cycle(client, tmp_path, state, NOW + timedelta(minutes=2), Schedule())

    assert new_state == state
    assert _sources_archived(tmp_path) == {SOURCE_EVENTS: 0, SOURCE_BOOKS: 1, SOURCE_METAR: 0}


def test_metar_and_events_follow_their_own_intervals(tmp_path, gamma_events_response, event):
    state = CollectorState(events=(event,), events_refreshed_at=NOW, metar_fetched_at=NOW)
    later = NOW + timedelta(minutes=10)  # METAR due (10 min), events not yet (15 min)

    with _fake_network(gamma_events_response, []) as client:
        new_state = run_cycle(client, tmp_path, state, later, Schedule())

    assert new_state == replace(state, metar_fetched_at=later)
    assert _sources_archived(tmp_path) == {SOURCE_EVENTS: 0, SOURCE_BOOKS: 1, SOURCE_METAR: 1}


def test_run_cycle_survives_network_errors(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = run_cycle(client, tmp_path, CollectorState(), NOW, Schedule())

    assert state.events == ()
    # Failed fetches count as done, so they are retried at their next interval, not every cycle.
    assert state.events_refreshed_at == NOW
    assert state.metar_fetched_at == NOW
