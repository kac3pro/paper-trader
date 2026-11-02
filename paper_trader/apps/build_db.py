"""Rebuild the DuckDB warehouse from the raw archive.

uv run python -m paper_trader.apps.build_db --raw data/raw --db data/warehouse.duckdb
"""

import argparse
import logging
import time
from pathlib import Path

from paper_trader.store.db import build


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the DuckDB warehouse.")
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    parser.add_argument("--db", type=Path, default=Path("data/warehouse.duckdb"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = time.monotonic()
    counts = build(args.raw, args.db)
    for table, n in counts.items():
        print(f"{table:16} {n:>10,} rows")
    print(f"built {args.db} in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    main()
