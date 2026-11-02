from datetime import UTC, datetime

import httpx

from paper_trader.apps.collect_forecasts import (
    SOURCE_FORECAST,
    SOURCE_RUNS,
    ForecastState,
    run_cycle,
)
from paper_trader.store.raw import read_records
from tests.test_dynamical import forecast_body

NOV1 = datetime(2026, 11, 1, tzinfo=UTC)
NOV2 = datetime(2026, 11, 2, tzinfo=UTC)
RUNS = {"runs": [{"initTime": "2026-11-02T00:00:00Z"}, {"initTime": "2026-11-01T00:00:00Z"}]}


def _fake_api(runs=None, forecast=None, forecast_status=200, seen=None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        if request.url.path == "/v1/forecasts/runs":
            return httpx.Response(200, json=RUNS if runs is None else runs)
        if request.url.path == "/v1/forecasts":
            if forecast_status != 200:
                return httpx.Response(forecast_status, text="<html>522</html>")
            return httpx.Response(200, json=forecast_body() if forecast is None else forecast)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _archived(root) -> dict[str, int]:
    return {
        src: sum(len(list(read_records(p))) for p in (root / src).glob("*/*.jsonl.gz"))
        for src in (SOURCE_RUNS, SOURCE_FORECAST)
    }


def test_new_run_is_fetched_and_remembered(tmp_path):
    with _fake_api() as client:
        state = run_cycle(client, tmp_path, ForecastState(stored_init=NOV1))

    assert state == ForecastState(stored_init=NOV2)
    assert _archived(tmp_path) == {SOURCE_RUNS: 1, SOURCE_FORECAST: 1}


def test_known_run_only_checks_the_list(tmp_path):
    seen: list[str] = []
    with _fake_api(seen=seen) as client:
        state = run_cycle(client, tmp_path, ForecastState(stored_init=NOV2))

    assert state == ForecastState(stored_init=NOV2)
    assert seen == ["/v1/forecasts/runs"]
    assert _archived(tmp_path) == {SOURCE_RUNS: 1, SOURCE_FORECAST: 0}


def test_failed_forecast_is_retried_next_time(tmp_path):
    with _fake_api(forecast_status=522) as client:
        state = run_cycle(client, tmp_path, ForecastState(stored_init=NOV1))

    assert state == ForecastState(stored_init=NOV1)  # unchanged, so it's tried again
    assert _archived(tmp_path) == {SOURCE_RUNS: 1, SOURCE_FORECAST: 1}  # the 522 is archived


def test_forecast_for_a_different_run_is_not_accepted(tmp_path):
    stale = forecast_body(init="2026-11-01T00:00:00Z")
    with _fake_api(forecast=stale) as client:
        state = run_cycle(client, tmp_path, ForecastState(stored_init=None))

    assert state == ForecastState(stored_init=None)


def test_empty_run_list_does_nothing(tmp_path):
    seen: list[str] = []
    with _fake_api(runs={"runs": []}, seen=seen) as client:
        state = run_cycle(client, tmp_path, ForecastState())

    assert state == ForecastState()
    assert seen == ["/v1/forecasts/runs"]


def test_hanging_api_leaves_state_unchanged(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        state = run_cycle(client, tmp_path, ForecastState(stored_init=NOV1))

    assert state == ForecastState(stored_init=NOV1)
    assert _archived(tmp_path) == {SOURCE_RUNS: 0, SOURCE_FORECAST: 0}
