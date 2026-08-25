import logging
from datetime import datetime

import duckdb

from config import INGESTION_ASSET_CLASS
from ingestion.raw_store import RawStore

log = logging.getLogger("transform.fact_ohlcv")

_FACT_COLUMNS = ["symbol", "interval", "source", "timestamp", "date", "open", "high", "low", "close", "volume"]


def load_fact_ohlcv(
    conn: duckdb.DuckDBPyConnection,
    store: RawStore,
    symbols: list[str],
    intervals: list[str],
    sources: list[str],
    start: datetime,
    end: datetime,
) -> int:
    """Load OHLCV rows from the Raw Parquet layer into fact_ohlcv.

    Idempotent: INSERT OR REPLACE keyed on (symbol, interval, source,
    timestamp) — rerunning with an overlapping range never duplicates rows,
    matching RawStore's own idempotency. A (symbol, interval, source)
    combination with nothing available for the range (e.g. Kraken outside
    its rolling window, see NOTES.md) is skipped, not an error.

    Returns the total number of rows loaded.
    """
    total = 0
    for symbol in symbols:
        for interval in intervals:
            for source in sources:
                df = store.load_range(
                    INGESTION_ASSET_CLASS, source, symbol, interval, start, end, dedupe=True
                )
                if df.empty:
                    continue

                df = df[["timestamp", "open", "high", "low", "close", "volume", "symbol", "interval", "source"]].copy()
                df["date"] = df["timestamp"].dt.date
                df = df[_FACT_COLUMNS]

                conn.register("_fact_ohlcv_batch", df)
                conn.execute(
                    f"INSERT OR REPLACE INTO fact_ohlcv ({', '.join(_FACT_COLUMNS)}) "
                    f"SELECT {', '.join(_FACT_COLUMNS)} FROM _fact_ohlcv_batch"
                )
                conn.unregister("_fact_ohlcv_batch")

                log.info("%s %s %s | loaded %d rows into fact_ohlcv", symbol, interval, source, len(df))
                total += len(df)

    return total
