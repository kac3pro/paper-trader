import json

import httpx

from paper_trader.adapters.clob import get_books_response


def test_get_books_response_posts_all_tokens_in_one_request():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content="[]")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = get_books_response(client, ["111", "222"])

    assert response.status_code == 200
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url == "https://clob.polymarket.com/books"
    assert json.loads(seen[0].content) == [{"token_id": "111"}, {"token_id": "222"}]
