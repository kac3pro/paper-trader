"""Forecast collector: archives each new GEFS 00Z run for EPWA, once.

    uv run python -m paper_trader.apps.collect_forecasts --data-dir data/raw

Every 15 min it asks dynamical.org which runs exist (cheap) and archives that answer:
the first time a run shows up is when it became available, which backtests need. When a
run we haven't stored yet appears, it fetches that run's EPWA point forecast (all 31
members, leads 0-72 h) and archives it.

This runs as its own process, separate from the market collector: the forecast API can
hang for minutes, and that must never delay order-book polling.
"""

import argparse
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

import httpx

from paper_trader.adapters.dynamical import (
    forecast_init_time,
    forecast_query,
    get_forecast_response,
    get_runs_response,
    latest_init_time,
)
from paper_trader.domain.time import next_tick
from paper_trader.store.raw import fetch_and_archive

log = logging.getLogger("paper_trader.collect_forecasts")

SOURCE_RUNS = "gefs_runs"
SOURCE_FORECAST = "gefs_epwa"


@dataclass(frozen=True)
class ForecastState:
    # Newest run whose forecast we archived successfully. After a restart this is None,
    # so the latest run is fetched once more; the duplicate is harmless.
    stored_init: datetime | None = None


def _json(response: httpx.Response | None) -> dict | None:
    if response is None or response.is_error:
        return None
    try:
        return response.json()
    except json.JSONDecodeError:
        return None


def run_cycle(client: httpx.Client, root: Path, state: ForecastState) -> ForecastState:
    """One check. Returns the new state; a failure leaves it unchanged, so the run is
    simply tried again at the next check."""
    runs = _json(fetch_and_archive(root, SOURCE_RUNS, partial(get_runs_response, client)))
    latest = latest_init_time(runs) if runs is not None else None
    if latest is None or latest == state.stored_init:
        return state

    log.info("new run %s, fetching forecast", latest.isoformat())
    request = partial(get_forecast_response, client, forecast_query(latest))
    forecast = _json(fetch_and_archive(root, SOURCE_FORECAST, request))
    if forecast is None or forecast_init_time(forecast) != latest:
        log.warning("forecast for run %s not usable yet, will retry", latest.isoformat())
        return state

    log.info("archived forecast for run %s", latest.isoformat())
    return ForecastState(stored_init=latest)


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive GEFS forecasts for EPWA.")
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--interval", type=int, default=900, help="seconds between checks")
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request at INFO
    interval = timedelta(seconds=args.interval)

    state = ForecastState()
    with httpx.Client() as client:  # timeouts are set per request in the adapter
        while True:
            try:
                state = run_cycle(client, args.data_dir, state)
            except Exception:  # never let one bad check kill the collector
                log.exception("check failed")
            if args.once:
                return
            now = datetime.now(UTC)
            time.sleep((next_tick(now, interval) - now).total_seconds())


if __name__ == "__main__":
    main()
