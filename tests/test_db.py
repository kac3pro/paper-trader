import gzip
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest

from paper_trader.domain.market import parse_event
from paper_trader.store.db import build
from paper_trader.store.raw import RawRecord, append_record, archive_path

T0 = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
TABLES = ["events", "brackets", "market_status", "book_snapshots", "metar_obs"]

METAR_A = "METAR EPWA 020730Z 03006KT 9999 BKN036 07/03 Q1029 NOSIG"
METAR_B = "METAR EPWA 020800Z 03006KT 9999 BKN036 08/03 Q1029 NOSIG"
METAR_B_COR = "METAR COR EPWA 020800Z 03006KT 9999 BKN036 09/03 Q1029 NOSIG"
SPECI = "SPECI EPWA 020812Z 03015G28KT 9999 BKN030 12/03 Q1029"


def _record(source: str, fetched_at: datetime, body: str, status: int = 200) -> RawRecord:
    return RawRecord(source, fetched_at, f"https://example.test/{source}", status, body)


def _book(token_id: str, bids: list, asks: list, last_trade: str = "0.5") -> dict:
    """A book in the CLOB's shape: levels worst price first."""
    return {
        "market": "0xcond",
        "asset_id": token_id,
        "timestamp": "1793606400000",  # 2026-11-02 08:00:00 UTC in ms
        "hash": "abc",
        "tick_size": "0.01",
        "min_order_size": "5",
        "last_trade_price": last_trade,
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
    }


@pytest.fixture
def raw_root(tmp_path, gamma_events_response):
    root = tmp_path / "raw"
    event = parse_event(gamma_events_response[0])
    by_label = {b.label: b for b in event.brackets}
    body = json.dumps(gamma_events_response)

    for record in [
        _record("gamma_events", T0, body),
        _record("gamma_events", T0 + timedelta(minutes=15), body),
        _record("gamma_events", T0, "[]"),  # an event that isn't listed yet
        _record("gamma_events", T0, "<html>502</html>", status=502),
        _record(
            "clob_books",
            T0,
            json.dumps(
                [
                    _book(
                        by_label["8°C"].yes_token_id,
                        bids=[("0.01", "100"), ("0.40", "10"), ("0.45", "5")],
                        asks=[("0.99", "100"), ("0.55", "10"), ("0.50", "5")],
                    ),
                    _book(
                        by_label["9°C"].yes_token_id,
                        bids=[("0.20", "10")],
                        asks=[("0.30", "10")],
                        last_trade="",  # never traded
                    ),
                ]
            ),
        ),
        _record("clob_books", T0, "<html>502</html>", status=502),
        _record("metar_epwa", T0 + timedelta(minutes=5), f"{METAR_B}\n{METAR_A}\n"),
        _record(
            "metar_epwa",
            T0 + timedelta(minutes=25),
            f"{SPECI}\n{METAR_B_COR}\n{METAR_B}\n{METAR_A}\n",
        ),
        _record("metar_epwa", T0 + timedelta(minutes=35), "", status=204),
    ]:
        append_record(root, record)
    return root


def _connect(db_path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db_path, read_only=True)
    con.execute("SET TimeZone = 'UTC'")
    return con


def test_build_counts(tmp_path, raw_root):
    counts = build(raw_root, tmp_path / "w.duckdb")
    assert counts == {
        "events": 1,
        "brackets": 11,
        "market_status": 22,  # 11 brackets x 2 successful fetches
        "book_snapshots": 2,
        "metar_obs": 4,  # A, B, B corrected, SPECI; repeats removed
    }


def test_events_keep_first_and_last_seen(tmp_path, raw_root):
    build(raw_root, tmp_path / "w.duckdb")
    with _connect(tmp_path / "w.duckdb") as con:
        target, first, last, source = con.execute(
            "SELECT target_date, first_seen_at, last_seen_at, resolution_source FROM events"
        ).fetchone()
    assert target == date(2026, 11, 2)
    assert (first, last) == (T0, T0 + timedelta(minutes=15))
    assert source.startswith("https://aviationweather.gov/api/data/metar?ids=EPWA")


def test_book_levels_are_sorted_best_first(tmp_path, raw_root):
    build(raw_root, tmp_path / "w.duckdb")
    with _connect(tmp_path / "w.duckdb") as con:
        bids, asks, best_bid, best_ask, mid, server_ts = con.execute(
            "SELECT bids, asks, best_bid, best_ask, mid, server_ts FROM book_top "
            "JOIN book_snapshots USING (fetched_at, best_bid, best_ask, mid) "
            "WHERE label = '8°C'"
        ).fetchone()
    assert [lvl["price"] for lvl in bids] == [Decimal("0.45"), Decimal("0.40"), Decimal("0.01")]
    assert [lvl["price"] for lvl in asks] == [Decimal("0.50"), Decimal("0.55"), Decimal("0.99")]
    assert (best_bid, best_ask, mid) == (Decimal("0.45"), Decimal("0.50"), Decimal("0.475"))
    assert server_ts == T0


def test_empty_last_trade_price_becomes_null(tmp_path, raw_root):
    build(raw_root, tmp_path / "w.duckdb")
    with _connect(tmp_path / "w.duckdb") as con:
        rows = con.execute(
            "SELECT label, last_trade_price FROM book_snapshots s "
            "JOIN brackets b ON b.yes_token_id = s.token_id ORDER BY label"
        ).fetchall()
    assert rows == [("8°C", Decimal("0.5")), ("9°C", None)]


def test_market_implied_sums_to_one(tmp_path, raw_root):
    build(raw_root, tmp_path / "w.duckdb")
    with _connect(tmp_path / "w.duckdb") as con:
        probs = dict(con.execute("SELECT label, implied_prob FROM market_implied").fetchall())
    # mids 0.475 and 0.25, normalized
    assert probs["8°C"] == pytest.approx(0.475 / 0.725)
    assert sum(probs.values()) == pytest.approx(1.0)


def test_metar_first_seen_and_corrections(tmp_path, raw_root):
    build(raw_root, tmp_path / "w.duckdb")
    with _connect(tmp_path / "w.duckdb") as con:
        rows = con.execute(
            "SELECT raw, report_type, is_correction, temp_body_c, first_seen_at "
            "FROM metar_obs ORDER BY first_seen_at, obs_time, raw"
        ).fetchall()
        daily = con.execute("SELECT local_date, max_temp_c, n_reports FROM metar_daily").fetchall()

    first_seen = {raw: seen for raw, *_, seen in rows}
    assert first_seen[METAR_A] == T0 + timedelta(minutes=5)  # not the later repeat
    assert first_seen[METAR_B_COR] == T0 + timedelta(minutes=25)
    assert {(r[1], r[2]) for r in rows if r[0] == SPECI} == {("SPECI", False)}

    # The SPECI (12°C) doesn't count; for 08:00 the corrected 9°C replaces the original 8°C.
    assert daily == [(date(2026, 11, 2), 9, 2)]


def test_damaged_file_is_loaded_with_fallback(tmp_path, raw_root, caplog):
    books_file = archive_path(raw_root, "clob_books", T0)
    half = gzip.compress(b'{"source": "clob_books", "fetched_at": "2026-11-02T08:02"}\n')
    with books_file.open("ab") as f:
        f.write(half[: len(half) // 2])  # a crash mid-write

    counts = build(raw_root, tmp_path / "w.duckdb")

    assert "fallback reader" in caplog.text and str(books_file) in caplog.text
    assert counts["book_snapshots"] == 2  # the intact record before the damage still loads


def test_empty_archive_builds_empty_tables(tmp_path):
    counts = build(tmp_path / "nothing-here", tmp_path / "w.duckdb")
    assert counts == dict.fromkeys(TABLES, 0)
    with _connect(tmp_path / "w.duckdb") as con:
        assert con.execute("SELECT count(*) FROM market_implied").fetchone() == (0,)


def test_rebuild_replaces_previous_database(tmp_path, raw_root):
    db = tmp_path / "w.duckdb"
    build(tmp_path / "nothing-here", db)
    counts = build(raw_root, db)
    assert counts["events"] == 1
    assert not (tmp_path / "w.duckdb.building").exists()


def test_official_results(tmp_path, raw_root, gamma_resolved_response):
    resolved_at = datetime(2026, 11, 1, 23, 25, tzinfo=UTC)
    append_record(
        raw_root, _record("gamma_events", resolved_at, json.dumps(gamma_resolved_response))
    )
    build(raw_root, tmp_path / "w.duckdb")

    with _connect(tmp_path / "w.duckdb") as con:
        results = con.execute(
            "SELECT target_date, winner_label, lo_c, hi_c, closed_time FROM official_results"
        ).fetchall()
        open_event = con.execute(
            "SELECT closed, winner_market_id FROM events WHERE target_date = '2026-11-02'"
        ).fetchone()

    # Nov 1 is resolved (9°C); Nov 2 is still open, so it has no official result yet.
    assert results == [
        (date(2026, 11, 1), "9°C", 9, 9, datetime(2026, 11, 1, 23, 19, 31, tzinfo=UTC))
    ]
    assert open_event == (False, None)
