"""CLI entry point for building/refreshing the Curated Layer (DuckDB).

Usage:
    python -m transform.build
    python -m transform.build --start 2024-01-01 --end 2024-06-01

Build order (see MODEL.md): schema first, then the small dimensions
(dim_symbol, dim_interval, dim_source, dim_date), then fact_ohlcv loaded
from the Raw Parquet layer. fact_indicator/fact_signal and
fact_backtest_run/fact_backtest_trade come later, once this foundation
holds plausible row counts.
"""
import argparse
import logging
from datetime import datetime, timedelta, timezone

import duckdb

from config import (
    CURATED_DB_PATH, INGESTION_INTERVALS, INGESTION_SOURCES, INGESTION_SYMBOLS,
    RAW_DATA_DIR, setup_logging,
)
from ingestion.raw_store import RawStore
from transform.db import connect
from transform.dims import populate_all_dims
from transform.fact_ohlcv import load_fact_ohlcv
from transform.schema import create_schema

log = logging.getLogger("transform.build")


def _default_start() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=730)


def build(
    conn: duckdb.DuckDBPyConnection,
    store: RawStore,
    symbols: list[str],
    intervals: list[str],
    sources: list[str],
    start: datetime,
    end: datetime,
) -> int:
    create_schema(conn)
    populate_all_dims(conn)
    row_count = load_fact_ohlcv(conn, store, symbols, intervals, sources, start, end)
    log.info("fact_ohlcv: %d rows loaded", row_count)
    return row_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m transform.build",
        description="Build/refresh the Curated Layer (DuckDB) from the raw Parquet layer.",
    )
    parser.add_argument(
        "--start", type=_parse_date, default=None,
        help="Start date/time (UTC, ISO format). Default: 730 days ago.",
    )
    parser.add_argument(
        "--end", type=_parse_date, default=None,
        help="End date/time (UTC, ISO format). Default: now.",
    )
    return parser


def _parse_date(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> None:
    setup_logging()
    args = build_parser().parse_args()
    start = args.start or _default_start()
    end = args.end or datetime.now(timezone.utc)

    conn = connect(CURATED_DB_PATH)
    store = RawStore(base_dir=RAW_DATA_DIR)
    try:
        build(conn, store, INGESTION_SYMBOLS, INGESTION_INTERVALS, INGESTION_SOURCES, start, end)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
