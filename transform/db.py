from pathlib import Path

import duckdb


def connect(db_path: str | Path) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the Curated Layer's DuckDB file.

    Forces the session timezone to UTC — DuckDB otherwise displays
    TIMESTAMPTZ values in the local system timezone, inconsistent with the
    rest of this codebase, which works in UTC throughout (RawStore,
    quality checks, backtest).
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute("SET TIMEZONE = 'UTC'")
    return conn
