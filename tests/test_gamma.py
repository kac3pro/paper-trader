import json
from datetime import date

import httpx

from paper_trader.adapters.gamma import fetch_event, warsaw_event_slug


def test_warsaw_event_slug():
    slug = warsaw_event_slug(date(2026, 11, 2))
    assert slug == "highest-temperature-in-warsaw-on-november-2-2026"


def test_warsaw_event_slug_has_no_zero_padding():
    assert warsaw_event_slug(date(2026, 3, 7)) == "highest-temperature-in-warsaw-on-march-7-2026"


def _client_returning(body: list, seen: list[httpx.Request]) -> httpx.Client:
    """An httpx client that never touches the network: it records requests and returns `body`."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=json.dumps(body))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_event_returns_raw_event(gamma_events_response):
    seen: list[httpx.Request] = []
    slug = "highest-temperature-in-warsaw-on-november-2-2026"

    with _client_returning(gamma_events_response, seen) as client:
        event = fetch_event(client, slug)

    assert event == gamma_events_response[0]
    assert seen[0].url.path == "/events"
    assert seen[0].url.params["slug"] == slug


def test_fetch_event_returns_none_when_missing():
    with _client_returning([], []) as client:
        assert fetch_event(client, "no-such-event") is None
