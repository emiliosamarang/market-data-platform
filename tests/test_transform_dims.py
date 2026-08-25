import datetime

import duckdb
import pandas as pd

from transform.dims import (
    DATE_DIM_END,
    DATE_DIM_START,
    populate_all_dims,
    populate_dim_date,
    populate_dim_interval,
    populate_dim_source,
    populate_dim_symbol,
)
from transform.schema import create_schema


def _conn():
    conn = duckdb.connect(":memory:")
    conn.execute("SET TIMEZONE = 'UTC'")
    create_schema(conn)
    return conn


class TestPopulateDimSymbol:
    def test_inserts_given_symbols(self):
        conn = _conn()
        populate_dim_symbol(conn, ["BTCUSDT", "ETHUSDT"])

        rows = conn.execute("SELECT symbol FROM dim_symbol ORDER BY symbol").fetchall()

        assert rows == [("BTCUSDT",), ("ETHUSDT",)]

    def test_rerun_does_not_duplicate(self):
        conn = _conn()
        populate_dim_symbol(conn, ["BTCUSDT"])
        populate_dim_symbol(conn, ["BTCUSDT"])

        count = conn.execute("SELECT COUNT(*) FROM dim_symbol").fetchone()[0]

        assert count == 1

    def test_rerun_with_new_symbols_adds_without_removing_existing(self):
        # Never delete: a symbol dropped from config must not orphan
        # historical fact_ohlcv rows that still reference it via FK.
        conn = _conn()
        populate_dim_symbol(conn, ["BTCUSDT", "ETHUSDT"])
        populate_dim_symbol(conn, ["SOLUSDT"])

        rows = conn.execute("SELECT symbol FROM dim_symbol ORDER BY symbol").fetchall()

        assert rows == [("BTCUSDT",), ("ETHUSDT",), ("SOLUSDT",)]


class TestPopulateDimInterval:
    def test_stores_interval_minutes(self):
        conn = _conn()
        populate_dim_interval(conn, ["1h", "4h"])

        rows = dict(conn.execute("SELECT interval, interval_minutes FROM dim_interval").fetchall())

        assert rows == {"1h": 60, "4h": 240}


class TestPopulateDimSource:
    def test_priority_follows_list_order(self):
        conn = _conn()
        populate_dim_source(conn, ["binance", "kraken"])

        rows = dict(conn.execute("SELECT source, priority FROM dim_source").fetchall())

        assert rows == {"binance": 0, "kraken": 1}


class TestPopulateDimDate:
    def test_covers_full_default_range(self):
        conn = _conn()
        populate_dim_date(conn)

        count = conn.execute("SELECT COUNT(*) FROM dim_date").fetchone()[0]
        expected_days = (pd.Timestamp(DATE_DIM_END) - pd.Timestamp(DATE_DIM_START)).days + 1

        assert count == expected_days

    def test_weekend_and_weekday_flagged_correctly(self):
        conn = _conn()
        # 2024-01-01 is a Monday
        populate_dim_date(conn, start="2024-01-01", end="2024-01-07")

        rows = dict(conn.execute("SELECT date, is_weekend FROM dim_date").fetchall())

        assert rows[datetime.date(2024, 1, 1)] is False  # Monday
        assert rows[datetime.date(2024, 1, 6)] is True   # Saturday
        assert rows[datetime.date(2024, 1, 7)] is True   # Sunday

    def test_rerun_does_not_duplicate(self):
        conn = _conn()
        populate_dim_date(conn, start="2024-01-01", end="2024-01-03")
        populate_dim_date(conn, start="2024-01-01", end="2024-01-03")

        count = conn.execute("SELECT COUNT(*) FROM dim_date").fetchone()[0]

        assert count == 3


class TestPopulateAllDims:
    def test_populates_all_four_dims(self):
        conn = _conn()
        populate_all_dims(conn)

        for table in ["dim_symbol", "dim_interval", "dim_source", "dim_date"]:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert count > 0
