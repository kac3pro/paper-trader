"""aviationweather.gov adapter: METAR reports, the market's resolution source.

This module only does HTTP. Responses are returned untouched so they can be archived.

Notes on /api/data/metar?format=raw (checked 2026-11-02):
- Plain text, one report per line, newest first, each prefixed "METAR" or "SPECI".
- No data for the window returns 204 with an empty body.
- The service occasionally answers 502 from its gateway; the next poll recovers.
"""

import httpx

BASE_URL = "https://aviationweather.gov/api/data"
EPWA = "EPWA"  # Warsaw Chopin Airport


def get_metar_response(client: httpx.Client, station: str = EPWA, hours: int = 6) -> httpx.Response:
    """Request all reports for `station` from the last `hours` hours.

    Polling a window much longer than the poll interval means a missed poll loses
    nothing, and later corrections (COR) of earlier reports are captured too.
    """
    return client.get(f"{BASE_URL}/metar", params={"ids": station, "format": "raw", "hours": hours})
