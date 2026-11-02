import json
from datetime import UTC, datetime

import httpx

from paper_trader.adapters.dynamical import (
    FORECAST_TIMEOUT,
    GEFS,
    forecast_init_time,
    forecast_query,
    get_forecast_response,
    get_runs_response,
    latest_init_time,
)

RUN = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)


def forecast_body(init: str = "2026-11-02T00:00:00Z", members: int = 2) -> dict:
    """A /v1/forecasts response in the API's shape, trimmed to two leads."""
    return {
        "results": [
            {
                "forecasts": [
                    {
                        "initTime": init,
                        "leadTimeHours": [0.0, 3.0],
                        "validTimes": [init, "2026-11-02T03:00:00Z"],
                        "ensembleMembers": [
                            {
                                "member": m,
                                "data": {
                                    "temperature_2m": [6.9, 6.7],
                                    "maximum_temperature_2m": [None, 7.0],
                                },
                            }
                            for m in range(members)
                        ],
                    }
                ]
            }
        ]
    }


def test_latest_init_time_picks_newest_regardless_of_order():
    runs = {
        "runs": [
            {"initTime": "2026-11-01T00:00:00Z"},
            {"initTime": "2026-11-02T00:00:00Z"},
            {"initTime": "2026-10-31T00:00:00Z"},
        ]
    }
    assert latest_init_time(runs) == RUN


def test_latest_init_time_none_when_no_runs():
    assert latest_init_time({"runs": []}) is None
    assert latest_init_time({}) is None


def test_forecast_query_names_the_run_explicitly():
    query = forecast_query(RUN)["queries"][0]
    assert query["dataProductId"] == GEFS
    assert query["initTime"] == "2026-11-02T00:00:00Z"
    assert query["location"] == {"latitude": 52.1657, "longitude": 20.9671}
    assert query["variables"] == ["temperature_2m", "maximum_temperature_2m"]
    assert query["maxLeadTimeHours"] == 72


def test_forecast_init_time():
    assert forecast_init_time(forecast_body()) == RUN


def test_forecast_init_time_rejects_unusable_answers():
    assert forecast_init_time({}) is None
    assert forecast_init_time({"results": []}) is None
    assert forecast_init_time(forecast_body(members=0)) is None


def test_requests_hit_the_right_endpoints():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        get_runs_response(client)
        get_forecast_response(client, forecast_query(RUN))

    runs_request, forecast_request = seen
    assert runs_request.method == "GET"
    assert runs_request.url.path == "/v1/forecasts/runs"
    assert runs_request.url.params["dataProductId"] == GEFS
    assert forecast_request.method == "POST"
    assert forecast_request.url.path == "/v1/forecasts"
    assert json.loads(forecast_request.content) == forecast_query(RUN)
    assert forecast_request.extensions["timeout"]["read"] == FORECAST_TIMEOUT
