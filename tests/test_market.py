import copy
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise

import pytest

from paper_trader.domain.market import parse_bracket, parse_event, resolved_winner


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("8°C", (8, 8)),
        ("1°C or below", (None, 1)),
        ("11°C or higher", (11, None)),
        ("-3°C", (-3, -3)),
        ("−3°C or below", (None, -3)),  # Unicode minus sign
        ("0°C or higher", (0, None)),
    ],
)
def test_parse_bracket(label, expected):
    assert parse_bracket(label) == expected


@pytest.mark.parametrize("label", ["8F", "8°C or more", "between 8 and 9°C", ""])
def test_parse_bracket_rejects_unknown_labels(label):
    with pytest.raises(ValueError):
        parse_bracket(label)


def test_parse_event(gamma_events_response):
    event = parse_event(gamma_events_response[0])

    assert event.slug == "highest-temperature-in-warsaw-on-november-2-2026"
    assert event.target_date == date(2026, 11, 2)
    assert event.neg_risk is True
    assert event.end_date == datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

    assert len(event.brackets) == 11
    assert (event.brackets[0].lo_c, event.brackets[0].hi_c) == (None, 1)
    assert (event.brackets[-1].lo_c, event.brackets[-1].hi_c) == (11, None)


def test_parse_event_brackets_are_contiguous(gamma_events_response):
    brackets = parse_event(gamma_events_response[0]).brackets
    for colder, warmer in pairwise(brackets):
        assert warmer.lo_c == colder.hi_c + 1


def test_parse_event_maps_yes_and_no_tokens(gamma_events_response):
    raw_market = gamma_events_response[0]["markets"][7]  # "8°C"
    bracket = next(b for b in parse_event(gamma_events_response[0]).brackets if b.label == "8°C")

    assert bracket.market_id == raw_market["id"]
    assert bracket.yes_token_id in raw_market["clobTokenIds"]
    assert bracket.no_token_id in raw_market["clobTokenIds"]
    assert bracket.yes_token_id != bracket.no_token_id


def test_open_event_is_not_resolved(gamma_events_response):
    event = parse_event(gamma_events_response[0])
    assert event.closed is False
    assert event.winner_market_id is None
    assert not event.resolved


def test_resolved_event_has_a_winner(gamma_resolved_response):
    event = parse_event(gamma_resolved_response[0])
    winner = next(b for b in event.brackets if b.market_id == event.winner_market_id)
    assert event.closed is True
    assert event.resolved
    assert winner.label == "9°C"


def _with_prices(raw: dict, yes_prices: dict[str, str]) -> dict:
    """Copy of a raw event with some brackets' YES/NO prices replaced, by label."""
    raw = copy.deepcopy(raw)
    for m in raw["markets"]:
        if m["groupItemTitle"] in yes_prices:
            yes = yes_prices[m["groupItemTitle"]]
            m["outcomePrices"] = json.dumps([yes, str(1 - Decimal(yes))])
    return raw


def test_closed_but_not_settled_has_no_winner(gamma_resolved_response):
    raw = _with_prices(gamma_resolved_response[0], {"9°C": "0.97", "10°C": "0.03"})
    assert resolved_winner(raw) is None


def test_two_winners_is_not_a_resolution(gamma_resolved_response):
    raw = _with_prices(gamma_resolved_response[0], {"10°C": "1"})
    assert resolved_winner(raw) is None


def test_open_bracket_means_not_resolved(gamma_resolved_response):
    raw = copy.deepcopy(gamma_resolved_response[0])
    raw["markets"][0]["closed"] = False
    assert resolved_winner(raw) is None
