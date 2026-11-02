"""Build the DuckDB warehouse from the raw archive.

The warehouse is always rebuilt from scratch: the raw archive is the source of truth, so
a parser fix only needs a rebuild. The new database is written to a temporary file and
swapped in at the end, so a reader (e.g. an open notebook) never sees a half-built one.

Order books and market status are unpacked in SQL straight from the .jsonl.gz files,
which is fast. Events and METARs are first reduced in SQL to a few distinct rows, then
parsed in Python with the domain parsers.
"""

import json
import logging
from collections.abc import Sequence
from pathlib import Path

import duckdb

from paper_trader.store.normalize import event_rows, metar_row
from paper_trader.store.raw import read_records

log = logging.getLogger(__name__)

SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

RAW_COLUMNS = (
    "{source: 'VARCHAR', fetched_at: 'TIMESTAMPTZ', url: 'VARCHAR', "
    "status: 'INTEGER', body: 'VARCHAR'}"
)

# Each loader is an INSERT whose input relation is {src}: either read_json over many
# archive files, or a temp table holding the records of one damaged file.

# Doubled braces are literal braces once .format(src=...) has run.
BOOKS_SQL = """
INSERT INTO book_snapshots
WITH raw AS (
    SELECT fetched_at, unnest(from_json(body, '[{{
        "market": "VARCHAR", "asset_id": "VARCHAR", "timestamp": "VARCHAR",
        "hash": "VARCHAR", "tick_size": "VARCHAR", "min_order_size": "VARCHAR",
        "last_trade_price": "VARCHAR",
        "bids": [{{"price": "VARCHAR", "size": "VARCHAR"}}],
        "asks": [{{"price": "VARCHAR", "size": "VARCHAR"}}]
    }}]')) AS b
    FROM {src}
    WHERE status = 200 AND json_valid(body)
), sorted AS (
    -- The CLOB sends worst price first; sort so the best price is always element 1.
    SELECT
        fetched_at,
        b,
        list_sort(list_transform(b.bids, lambda x: {{
            'price': x.price::DECIMAL(6, 4), 'size': x.size::DECIMAL(18, 6)}}), 'DESC') AS bids,
        list_sort(list_transform(b.asks, lambda x: {{
            'price': x.price::DECIMAL(6, 4), 'size': x.size::DECIMAL(18, 6)}}), 'ASC') AS asks
    FROM raw
)
SELECT
    fetched_at,
    to_timestamp(b.timestamp::BIGINT / 1000),
    b.asset_id,
    b.market,
    b.hash,
    bids,
    asks,
    bids[1].price,
    asks[1].price,
    ((bids[1].price + asks[1].price) / 2)::DECIMAL(7, 5),
    nullif(b.tick_size, '')::DECIMAL(6, 4),
    nullif(b.min_order_size, '')::DECIMAL(18, 6),
    nullif(b.last_trade_price, '')::DECIMAL(6, 4)
FROM sorted
"""

MARKET_STATUS_SQL = """
INSERT INTO market_status
WITH m AS (
    SELECT fetched_at, unnest(json_extract(body, '$[*].markets[*]')) AS m
    FROM {src}
    WHERE status = 200 AND json_valid(body)
)
SELECT
    fetched_at,
    m->>'id',
    nullif(m->>'active', '')::BOOLEAN,
    nullif(m->>'closed', '')::BOOLEAN,
    nullif(m->>'acceptingOrders', '')::BOOLEAN,
    nullif(m->>'orderPriceMinTickSize', '')::DECIMAL(6, 4),
    nullif(m->>'orderMinSize', '')::DECIMAL(18, 6),
    nullif(m->>'bestBid', '')::DECIMAL(6, 4),
    nullif(m->>'bestAsk', '')::DECIMAL(6, 4),
    nullif(m->>'lastTradePrice', '')::DECIMAL(6, 4),
    m->>'outcomePrices',
    nullif(m->>'volume', '')::DECIMAL(18, 6),
    nullif(m->>'liquidity', '')::DECIMAL(18, 6),
    nullif(m->'feeSchedule'->>'rate', '')::DECIMAL(8, 6),
    nullif(m->'feeSchedule'->>'exponent', '')::DECIMAL(8, 4),
    nullif(m->'feeSchedule'->>'takerOnly', '')::BOOLEAN,
    nullif(m->'feeSchedule'->>'rebateRate', '')::DECIMAL(8, 6)
FROM m
"""

# Reduce Gamma responses to: per slug, first/last time seen and the latest body.
EVENTS_STAGING_SQL = """
INSERT INTO stg_events
SELECT body->>'$[0].slug', min(fetched_at), max(fetched_at), arg_max(body, fetched_at)
FROM {src}
WHERE status = 200 AND json_valid(body) AND json_array_length(body) > 0
GROUP BY 1
"""

# Reduce METAR responses to: each distinct report line and when we first saw it.
METAR_STAGING_SQL = """
INSERT INTO stg_metar
SELECT line, min(fetched_at)
FROM (
    SELECT fetched_at, trim(unnest(string_split(body, chr(10)))) AS line
    FROM {src}
    WHERE status = 200
)
WHERE line <> ''
GROUP BY line
"""


def _sql_list(paths: Sequence[Path]) -> str:
    return "[" + ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths) + "]"


def _read_json(paths: Sequence[Path]) -> str:
    return f"read_json({_sql_list(paths)}, format = 'newline_delimited', columns = {RAW_COLUMNS})"


def _load_damaged_file(con: duckdb.DuckDBPyConnection, path: Path, insert_sql: str) -> None:
    """Load one file DuckDB can't read (e.g. truncated mid-write) via the tolerant reader."""
    con.execute(
        "CREATE OR REPLACE TEMP TABLE raw_fallback ("
        "source VARCHAR, fetched_at TIMESTAMPTZ, url VARCHAR, status INTEGER, body VARCHAR)"
    )
    rows = [(r.source, r.fetched_at, r.url, r.status, r.body) for r in read_records(path)]
    if rows:
        con.executemany("INSERT INTO raw_fallback VALUES (?, ?, ?, ?, ?)", rows)
    con.execute(insert_sql.format(src="raw_fallback"))


def load_source(con: duckdb.DuckDBPyConnection, files: Sequence[Path], insert_sql: str) -> None:
    """Run `insert_sql` over all `files`. If DuckDB chokes on a file, that file is loaded
    through the Python reader instead, and the rest is retried without it."""
    remaining = list(files)
    while remaining:
        try:
            con.execute(insert_sql.format(src=_read_json(remaining)))
            return
        except duckdb.Error as exc:
            bad = next((f for f in remaining if str(f) in str(exc)), None)
            if bad is None:
                raise
            log.warning("unreadable by DuckDB, using fallback reader: %s", bad)
            remaining.remove(bad)
            _load_damaged_file(con, bad, insert_sql)


def _archive_files(raw_root: Path, source: str) -> list[Path]:
    return sorted((raw_root / source).glob("*/*.jsonl.gz"))


def _insert_events(con: duckdb.DuckDBPyConnection) -> None:
    staged = con.execute(
        "SELECT slug, min(first_seen), max(last_seen), arg_max(body, last_seen) "
        "FROM stg_events GROUP BY slug"
    ).fetchall()
    for slug, first_seen, last_seen, body in staged:
        try:
            event_row, bracket_rows = event_rows(json.loads(body)[0], first_seen, last_seen)
        except (ValueError, KeyError) as exc:
            log.error("skipping event %s: %s", slug, exc)
            continue
        con.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", event_row)
        con.executemany("INSERT INTO brackets VALUES (?, ?, ?, ?, ?, ?, ?, ?)", bracket_rows)


def _insert_metars(con: duckdb.DuckDBPyConnection) -> None:
    staged = con.execute("SELECT line, min(first_seen) FROM stg_metar GROUP BY line").fetchall()
    rows = []
    for line, first_seen in staged:
        try:
            rows.append(metar_row(line, first_seen))
        except ValueError:
            log.debug("skipping non-METAR line: %r", line)
    if rows:
        con.executemany("INSERT INTO metar_obs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def build(raw_root: Path, db_path: Path) -> dict[str, int]:
    """Rebuild the warehouse at `db_path` from `raw_root`. Returns row counts per table."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".building")
    tmp_path.unlink(missing_ok=True)

    with duckdb.connect(tmp_path) as con:
        con.execute("SET TimeZone = 'UTC'")
        con.execute(SCHEMA)
        con.execute(
            "CREATE TEMP TABLE stg_events (slug VARCHAR, first_seen TIMESTAMPTZ, "
            "last_seen TIMESTAMPTZ, body VARCHAR)"
        )
        con.execute("CREATE TEMP TABLE stg_metar (line VARCHAR, first_seen TIMESTAMPTZ)")

        loads = [
            ("clob_books", BOOKS_SQL),
            ("gamma_events", MARKET_STATUS_SQL),
            ("gamma_events", EVENTS_STAGING_SQL),
            ("metar_epwa", METAR_STAGING_SQL),
        ]
        for source, sql in loads:
            load_source(con, _archive_files(raw_root, source), sql)
        _insert_events(con)
        _insert_metars(con)

        tables = ["events", "brackets", "market_status", "book_snapshots", "metar_obs"]
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}

    tmp_path.replace(db_path)
    return counts
