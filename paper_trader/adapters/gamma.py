"""Gamma API adapter: finds the daily Warsaw event.

This module only does HTTP. It returns raw JSON; turning it into domain
values is the job of paper_trader.domain.market.
"""

from datetime import date
from typing import Any

import httpx

BASE_URL = "https://gamma-api.polymarket.com"

_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)


def warsaw_event_slug(target_date: date) -> str:
    """Slug of the Warsaw event, e.g. highest-temperature-in-warsaw-on-november-2-2026."""
    # Month names are spelled out instead of using strftime("%B"), which depends on locale.
    month = _MONTHS[target_date.month - 1]
    return f"highest-temperature-in-warsaw-on-{month}-{target_date.day}-{target_date.year}"


def get_event_response(client: httpx.Client, slug: str) -> httpx.Response:
    """Request one event by slug. Returns the response untouched, so it can be archived."""
    return client.get(f"{BASE_URL}/events", params={"slug": slug})


def event_from_response(response: httpx.Response) -> dict[str, Any] | None:
    """Extract the raw event JSON from a /events response, or None if no event matched."""
    response.raise_for_status()
    events = response.json()
    return events[0] if events else None


def fetch_event(client: httpx.Client, slug: str) -> dict[str, Any] | None:
    """Fetch one event by slug without archiving. Returns the raw event JSON, or None."""
    return event_from_response(get_event_response(client, slug))
