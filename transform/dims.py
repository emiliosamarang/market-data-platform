import duckdb
import pandas as pd

from config import INGESTION_INTERVALS, INGESTION_SOURCES, INGESTION_SYMBOLS
from ingestion.time_utils import interval_to_ms

# Generous static calendar range, not tied to the raw layer's actual data
# span — standard practice for a date dimension, and avoids a chicken-and-egg
# dependency on fact_ohlcv (dim_date must exist and be populated before any
# fact_ohlcv row can be inserted, per its FK).
DATE_DIM_START = "2020-01-01"
DATE_DIM_END = "2030-12-31"


def populate_dim_symbol(conn: duckdb.DuckDBPyConnection, symbols: list[str] = INGESTION_SYMBOLS) -> None:
    """INSERT OR IGNORE, never DELETE — a dimension row must never disappear
    out from under historical fact_ohlcv rows still referencing it (via FK),
    and DuckDB rejects a DELETE against a still-referenced key regardless.
    A symbol dropped from config just stops getting new rows; existing
    history keeps its dimension row."""
    conn.executemany("INSERT OR IGNORE INTO dim_symbol (symbol) VALUES (?)", [(s,) for s in symbols])


def populate_dim_interval(conn: duckdb.DuckDBPyConnection, intervals: list[str] = INGESTION_INTERVALS) -> None:
    rows = [(i, interval_to_ms(i) // 60_000) for i in intervals]
    conn.executemany("INSERT OR IGNORE INTO dim_interval (interval, interval_minutes) VALUES (?, ?)", rows)


def populate_dim_source(conn: duckdb.DuckDBPyConnection, sources: list[str] = INGESTION_SOURCES) -> None:
    """Priority is the position in `sources` — see dim_source.priority in schema.py."""
    rows = [(source, priority) for priority, source in enumerate(sources)]
    conn.executemany("INSERT OR IGNORE INTO dim_source (source, priority) VALUES (?, ?)", rows)


def populate_dim_date(
    conn: duckdb.DuckDBPyConnection, start: str = DATE_DIM_START, end: str = DATE_DIM_END
) -> None:
    dates = pd.date_range(start, end, freq="D")
    df = pd.DataFrame({
        "date": dates.date,
        "year": dates.year,
        "month": dates.month,
        "day": dates.day,
        "day_of_week": dates.dayofweek,
        "day_name": dates.day_name(),
        "is_weekend": dates.dayofweek >= 5,
    })
    conn.register("_dim_date_batch", df)
    conn.execute("INSERT OR IGNORE INTO dim_date SELECT * FROM _dim_date_batch")
    conn.unregister("_dim_date_batch")


def populate_all_dims(conn: duckdb.DuckDBPyConnection) -> None:
    populate_dim_symbol(conn)
    populate_dim_interval(conn)
    populate_dim_source(conn)
    populate_dim_date(conn)
