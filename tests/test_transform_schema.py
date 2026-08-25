import duckdb
import pytest

from transform.schema import create_schema


def _conn():
    conn = duckdb.connect(":memory:")
    conn.execute("SET TIMEZONE = 'UTC'")
    return conn


class TestCreateSchema:
    def test_creates_all_tables(self):
        conn = _conn()
        create_schema(conn)

        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}

        assert {"dim_symbol", "dim_interval", "dim_source", "dim_date", "fact_ohlcv"} <= tables

    def test_creates_canonical_view(self):
        conn = _conn()
        create_schema(conn)

        views = conn.execute(
            "SELECT view_name FROM duckdb_views() WHERE view_name = 'fact_ohlcv_canonical'"
        ).fetchall()

        assert len(views) == 1

    def test_idempotent_rerun_does_not_error(self):
        conn = _conn()
        create_schema(conn)
        create_schema(conn)  # must not raise

    def test_fact_ohlcv_primary_key_rejects_exact_duplicate(self):
        conn = _conn()
        create_schema(conn)
        conn.execute("INSERT INTO dim_symbol VALUES ('BTCUSDT')")
        conn.execute("INSERT INTO dim_interval VALUES ('1h', 60)")
        conn.execute("INSERT INTO dim_source VALUES ('binance', 0)")
        conn.execute("INSERT INTO dim_date VALUES ('2024-01-01', 2024, 1, 1, 0, 'Monday', false)")
        conn.execute(
            "INSERT INTO fact_ohlcv VALUES "
            "('BTCUSDT', '1h', 'binance', '2024-01-01 00:00:00+00', '2024-01-01', 100, 101, 99, 100.5, 10)"
        )

        with pytest.raises(duckdb.ConstraintException):
            conn.execute(
                "INSERT INTO fact_ohlcv VALUES "
                "('BTCUSDT', '1h', 'binance', '2024-01-01 00:00:00+00', '2024-01-01', 1, 1, 1, 1, 1)"
            )

    def test_fact_ohlcv_rejects_unknown_symbol(self):
        conn = _conn()
        create_schema(conn)
        conn.execute("INSERT INTO dim_interval VALUES ('1h', 60)")
        conn.execute("INSERT INTO dim_source VALUES ('binance', 0)")
        conn.execute("INSERT INTO dim_date VALUES ('2024-01-01', 2024, 1, 1, 0, 'Monday', false)")

        with pytest.raises(duckdb.ConstraintException):
            conn.execute(
                "INSERT INTO fact_ohlcv VALUES "
                "('NOPEUSDT', '1h', 'binance', '2024-01-01 00:00:00+00', '2024-01-01', 100, 101, 99, 100.5, 10)"
            )
