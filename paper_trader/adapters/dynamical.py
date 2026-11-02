"""dynamical.org point API adapter: NOAA GEFS ensemble forecasts for EPWA.

HTTP calls return responses untouched so they can be archived. The small pure helpers
build the query and read the init time back out of a response.

Notes on the API (checked 2026-11-02, docs at https://dynamical.org/api):
- No API key. Rate limit 30 requests / 10 s per IP. Beta: it sometimes hangs for minutes
  and answers 522 (Cloudflare timeout) with Retry-After, so timeouts here are long.
- GEFS product "noaa-gefs-forecast-35-day": 31 members, 00Z runs only, 3-hourly leads
  to 240 h. A forecast response returns every member.
- The location snaps to the nearest grid cell; the response reports which one.
"""

from datetime import datetime
from typing import Any

import httpx

BASE_URL = "https://api.dynamical.org"
GEFS = "noaa-gefs-forecast-35-day"

# Warsaw Chopin Airport (EPWA), the market's resolution station.
EPWA_LATITUDE = 52.1657
EPWA_LONGITUDE = 20.9671

VARIABLES = ("temperature_2m", "maximum_temperature_2m")
MAX_LEAD_HOURS = 72  # covers the Warsaw local days D+1 and D+2 of a 00Z run on day D

RUNS_TIMEOUT = 60.0  # seconds; normally ~1-4 s
FORECAST_TIMEOUT = 300.0  # seconds; normally ~50 s


def get_runs_response(client: httpx.Client, limit: int = 5) -> httpx.Response:
    """List the most recent GEFS runs available in the API."""
    return client.get(
        f"{BASE_URL}/v1/forecasts/runs",
        params={"dataProductId": GEFS, "limit": limit},
        timeout=RUNS_TIMEOUT,
    )


def latest_init_time(runs: dict[str, Any]) -> datetime | None:
    """Newest init time in a /v1/forecasts/runs response body, or None if it lists none."""
    times = [datetime.fromisoformat(r["initTime"]) for r in runs.get("runs", [])]
    return max(times, default=None)


def forecast_query(
    init_time: datetime,
    latitude: float = EPWA_LATITUDE,
    longitude: float = EPWA_LONGITUDE,
) -> dict[str, Any]:
    """Request body for one run's point forecast. The run is named explicitly (not
    "latest"), so we get exactly the run we saw listed even if a newer one appears."""
    return {
        "queries": [
            {
                "dataProductId": GEFS,
                "location": {"latitude": latitude, "longitude": longitude},
                "initTime": init_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "variables": list(VARIABLES),
                "maxLeadTimeHours": MAX_LEAD_HOURS,
            }
        ]
    }


def get_forecast_response(client: httpx.Client, query: dict[str, Any]) -> httpx.Response:
    return client.post(f"{BASE_URL}/v1/forecasts", json=query, timeout=FORECAST_TIMEOUT)


def forecast_init_time(forecast: dict[str, Any]) -> datetime | None:
    """Init time of the run in a /v1/forecasts response body, or None if it holds no
    forecast with ensemble members (i.e. it isn't a usable answer)."""
    try:
        run = forecast["results"][0]["forecasts"][0]
        if not run["ensembleMembers"]:
            return None
        return datetime.fromisoformat(run["initTime"])
    except (KeyError, IndexError, TypeError):
        return None
