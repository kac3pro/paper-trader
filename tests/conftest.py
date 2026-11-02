import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def gamma_events_response() -> list[dict]:
    """A real Gamma /events?slug=... response (a list with one event), saved 2026-11-01."""
    return json.loads((FIXTURES / "gamma_event_warsaw_2026-11-02.json").read_text())
