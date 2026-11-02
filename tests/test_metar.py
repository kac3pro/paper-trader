import httpx

from paper_trader.adapters.metar import get_metar_response


def test_get_metar_response_requests_raw_reports_for_epwa():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="METAR EPWA 021500Z ... 08/03 Q1029 NOSIG\n")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = get_metar_response(client)

    assert response.status_code == 200
    assert seen[0].url.host == "aviationweather.gov"
    assert seen[0].url.path == "/api/data/metar"
    assert dict(seen[0].url.params) == {"ids": "EPWA", "format": "raw", "hours": "6"}
