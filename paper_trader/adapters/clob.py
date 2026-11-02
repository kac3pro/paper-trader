"""CLOB API adapter: order books.

This module only does HTTP. Responses are returned untouched so they can be archived.

Notes on the /books response (checked 2026-11-02):
- Levels are ordered worst price first: bids ascending, asks descending.
- The NO token's book mirrors the YES token's book (NO bid at p == YES ask at 1 - p),
  so fetching YES tokens is enough.
"""

from collections.abc import Sequence

import httpx

BASE_URL = "https://clob.polymarket.com"


def get_books_response(client: httpx.Client, token_ids: Sequence[str]) -> httpx.Response:
    """Request the order books for several tokens in one call."""
    return client.post(f"{BASE_URL}/books", json=[{"token_id": t} for t in token_ids])
