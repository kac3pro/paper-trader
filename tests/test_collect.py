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
    dates_to_refresh,
    is_due,
    merge_events,
    open_yes_token_ids,
    run_cycle,
    target_dates,
    tracked_window,
)
from paper_trader.domain.market import parse_event
from paper_trader.domain.time import next_tick
from paper_trader.store.raw import read_records

FIFTEEN_MIN = timedelta(minutes=15)
NOW = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
METAR_TEXT = (
    "METAR EPWA 020930Z 03005KT 350V060 9999 FEW020 BKN038 08/03 Q1030 NOSIG\n"
    "METAR EPWA 020900Z 03005KT 9999 FEW020 BKN038 07/03 Q1030 NOSIG\n"
)


@pytest.fixture
def event(gamma_events_response):
    return parse_event(gamma_events_response[0])  # target date 2026-11-02, still open


@pytest.fixture
def resolved(gamma_resolved_response):
    return parse_event(gamma_resolved_response[0])  # target date 2026-11-01, won by 9°C


def _on(event, day: int, *, resolved: bool):
    """A copy of `event` moved to Nov `day`, either open or resolved."""
    winner = event.brackets[0].market_id if resolved else None
    return replace(event, target_date=date(2026, 11, day), closed=resolved, winner_market_id=winner)


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


def test_open_yes_token_ids_skips_closed_events(event):
    closed_yesterday = _on(event, 1, resolved=True)
    still_trading_yesterday = _on(event, 1, resolved=False)
    assert open_yes_token_ids([closed_yesterday, event]) == [b.yes_token_id for b in event.brackets]
    assert len(open_yes_token_ids([still_trading_yesterday, event])) == 2 * len(event.brackets)


def test_tracked_window_is_seven_days_back_to_two_ahead():
    window = tracked_window(NOW)  # Warsaw date Nov 2
    assert (window[0], window[-1], len(window)) == (date(2026, 10, 26), date(2026, 11, 4), 10)


def test_dates_to_refresh_includes_yesterday_until_known_resolved(event):
    upcoming = (date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4))
    assert dates_to_refresh([], NOW) == (date(2026, 11, 1), *upcoming)  # e.g. after a restart
    assert dates_to_refresh([_on(event, 1, resolved=False)], NOW) == (date(2026, 11, 1), *upcoming)
    assert dates_to_refresh([_on(event, 1, resolved=True)], NOW) == upcoming


def test_dates_to_refresh_keeps_unresolved_past_events_for_seven_days(event):
    oct27 = replace(event, target_date=date(2026, 10, 27), closed=False, winner_market_id=None)
    oct25 = replace(event, target_date=date(2026, 10, 25), closed=False, winner_market_id=None)
    events = [oct25, oct27]
    refreshed = dates_to_refresh(events, NOW)
    assert date(2026, 10, 27) in refreshed  # 6 days old: still trying
    assert date(2026, 10, 25) not in refreshed  # 8 days old: given up


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


def _fake_network(*gamma_responses, seen: list[httpx.Request] | None = None) -> httpx.Client:
    """Fake Gamma (serving the given event responses by slug), CLOB and METAR APIs."""
    listed = {response[0]["slug"]: response for response in gamma_responses}

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.host == "gamma-api.polymarket.com":
            body = listed.get(request.url.params["slug"], [])
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

    with _fake_network(gamma_events_response, seen=seen) as client:
        state = run_cycle(client, tmp_path, CollectorState(), NOW, Schedule())

    assert state.events == (event,)  # only Nov 2 is "listed"
    assert state.events_refreshed_at == NOW
    assert state.metar_fetched_at == NOW
    # Events: yesterday (Nov 1, in case it's unresolved) plus Nov 2, 3 and 4.
    assert _sources_archived(tmp_path) == {SOURCE_EVENTS: 4, SOURCE_BOOKS: 1, SOURCE_METAR: 1}

    books_request = next(r for r in seen if r.url.host == "clob.polymarket.com")
    requested = [item["token_id"] for item in json.loads(books_request.content)]
    assert requested == [b.yes_token_id for b in event.brackets]

    metar_request = next(r for r in seen if r.url.host == "aviationweather.gov")
    assert metar_request.url.params["ids"] == "EPWA"


def test_next_cycle_fetches_only_books(tmp_path, gamma_events_response, event):
    state = CollectorState(events=(event,), events_refreshed_at=NOW, metar_fetched_at=NOW)

    with _fake_network(gamma_events_response) as client:
        new_state = run_cycle(client, tmp_path, state, NOW + timedelta(minutes=2), Schedule())

    assert new_state == state
    assert _sources_archived(tmp_path) == {SOURCE_EVENTS: 0, SOURCE_BOOKS: 1, SOURCE_METAR: 0}


def test_metar_and_events_follow_their_own_intervals(tmp_path, gamma_events_response, event):
    state = CollectorState(events=(event,), events_refreshed_at=NOW, metar_fetched_at=NOW)
    later = NOW + timedelta(minutes=10)  # METAR due (10 min), events not yet (15 min)

    with _fake_network(gamma_events_response) as client:
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


def test_past_event_is_followed_until_resolved(
    tmp_path, gamma_events_response, gamma_resolved_response, event, resolved
):
    # 23:30 UTC on Nov 1 is 00:30 on Nov 2 in Warsaw: the Nov 1 event is "yesterday" now,
    # still trading, and Polymarket is about to resolve it.
    after_midnight = datetime(2026, 11, 1, 23, 30, tzinfo=UTC)
    nov1_open = replace(resolved, closed=False, winner_market_id=None)
    state = CollectorState(events=(nov1_open, event))

    seen: list[httpx.Request] = []
    with _fake_network(gamma_resolved_response, gamma_events_response, seen=seen) as client:
        state = run_cycle(client, tmp_path, state, after_midnight, Schedule())

        assert state.events[0] == resolved  # official result picked up
        books_request = next(r for r in seen if r.url.host == "clob.polymarket.com")
        requested = {item["token_id"] for item in json.loads(books_request.content)}
        assert requested == {b.yes_token_id for b in event.brackets}  # closed Nov 1 dropped

        # 15 min later the resolved event is remembered but no longer fetched.
        seen.clear()
        state = run_cycle(client, tmp_path, state, after_midnight + FIFTEEN_MIN, Schedule())
        slugs = {r.url.params["slug"] for r in seen if r.url.host == "gamma-api.polymarket.com"}
        assert resolved.slug not in slugs
        assert state.events[0] == resolved
