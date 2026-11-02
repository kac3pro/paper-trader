"""Domain types for a daily Warsaw max-temperature event and its brackets.

Everything here is pure: raw Gamma JSON goes in, immutable values come out.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Bracket:
    """One YES/NO market inside an event, covering whole degrees lo_c..hi_c inclusive.

    None means the bracket is open-ended on that side ("or below" / "or higher").
    """

    market_id: str
    condition_id: str
    label: str
    lo_c: int | None
    hi_c: int | None
    yes_token_id: str
    no_token_id: str


@dataclass(frozen=True)
class Event:
    """One daily event: "Highest temperature in Warsaw on <target_date>?"."""

    event_id: str
    slug: str
    title: str
    target_date: date  # Europe/Warsaw local date
    neg_risk: bool
    start_date: datetime
    end_date: datetime
    brackets: tuple[Bracket, ...]  # sorted from coldest to warmest
    closed: bool = False  # no longer trading
    winner_market_id: str | None = None  # set once Polymarket has resolved the event

    @property
    def resolved(self) -> bool:
        return self.winner_market_id is not None


_LABEL_RE = re.compile(r"^\s*(-?\d+)\s*°C(?:\s+or\s+(below|higher))?\s*$")


def parse_bracket(label: str) -> tuple[int | None, int | None]:
    """Turn a bracket label into an inclusive (lo_c, hi_c) range.

    "8°C" -> (8, 8), "1°C or below" -> (None, 1), "11°C or higher" -> (11, None).
    Raises ValueError on any label we don't recognise, so format changes fail loudly.
    """
    match = _LABEL_RE.match(label.replace("−", "-"))  # accept a Unicode minus sign
    if match is None:
        raise ValueError(f"unrecognised bracket label: {label!r}")
    temp = int(match.group(1))
    match match.group(2):
        case "below":
            return (None, temp)
        case "higher":
            return (temp, None)
        case _:
            return (temp, temp)


def parse_market(raw: dict[str, Any]) -> Bracket:
    """Build a Bracket from one entry of a Gamma event's "markets" list."""
    # Gamma encodes these lists as JSON strings inside the JSON.
    outcomes = json.loads(raw["outcomes"])
    token_ids = json.loads(raw["clobTokenIds"])
    token_by_outcome = dict(zip(outcomes, token_ids, strict=True))
    lo_c, hi_c = parse_bracket(raw["groupItemTitle"])
    return Bracket(
        market_id=raw["id"],
        condition_id=raw["conditionId"],
        label=raw["groupItemTitle"],
        lo_c=lo_c,
        hi_c=hi_c,
        yes_token_id=token_by_outcome["Yes"],
        no_token_id=token_by_outcome["No"],
    )


def resolved_winner(raw: dict[str, Any]) -> str | None:
    """Market id of the winning bracket, or None if the event isn't fully resolved yet.

    Resolved means: the event and every bracket are closed, every bracket's YES price is
    exactly 0 or 1, and exactly one bracket's YES price is 1.
    """
    markets = raw.get("markets", [])
    if not raw.get("closed") or not markets or not all(m.get("closed") for m in markets):
        return None
    yes_prices = {}
    for m in markets:
        prices = dict(zip(json.loads(m["outcomes"]), json.loads(m["outcomePrices"]), strict=True))
        yes_prices[m["id"]] = Decimal(prices["Yes"])
    if not all(p in (0, 1) for p in yes_prices.values()):
        return None
    winners = [market_id for market_id, p in yes_prices.items() if p == 1]
    return winners[0] if len(winners) == 1 else None


def _coldest_first(bracket: Bracket) -> float:
    return float("-inf") if bracket.lo_c is None else bracket.lo_c


def parse_event(raw: dict[str, Any]) -> Event:
    """Build an Event from one Gamma event object."""
    brackets = sorted((parse_market(m) for m in raw["markets"]), key=_coldest_first)
    return Event(
        event_id=raw["id"],
        slug=raw["slug"],
        title=raw["title"],
        target_date=date.fromisoformat(raw["eventDate"]),
        neg_risk=raw["negRisk"],
        start_date=datetime.fromisoformat(raw["startDate"]),
        end_date=datetime.fromisoformat(raw["endDate"]),
        brackets=tuple(brackets),
        closed=bool(raw.get("closed", False)),
        winner_market_id=resolved_winner(raw),
    )
