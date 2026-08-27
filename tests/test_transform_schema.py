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

    def test_fact_backtest_run_has_strategy_name_default(self):
        conn = _conn()
        create_schema(conn)

        default = conn.execute(
            "SELECT column_default FROM duckdb_columns() "
            "WHERE table_name = 'fact_backtest_run' AND column_name = 'strategy_name'"
        ).fetchone()[0]
        assert "ema_rsi_macd" in default


class TestStrategyNameMigration:
    def test_alter_table_adds_column_and_backfills_existing_rows_without_data_loss(self):
        """Simulates the real data/curated.duckdb, created before strategy_name
        existed: a bare-bones fact_backtest_run (as it would've looked pre-
        migration) with a row already in it, before create_schema() has ever
        run against this connection. create_schema() must add the column and
        backfill the pre-existing row via ALTER TABLE, not lose it — this is
        the exact scenario the real database's 22 historized rows went
        through, verified live against data/curated.duckdb during
        development of this migration."""
        conn = _conn()
        conn.execute("CREATE TABLE fact_backtest_run (run_id VARCHAR PRIMARY KEY, days INTEGER)")
        conn.execute("INSERT INTO fact_backtest_run VALUES ('abc123', 150)")

        create_schema(conn)  # the migration under test — must not error or drop rows

        row = conn.execute(
            "SELECT days, strategy_name FROM fact_backtest_run WHERE run_id = 'abc123'"
        ).fetchone()
        assert row == (150, "ema_rsi_macd")
        count = conn.execute("SELECT COUNT(*) FROM fact_backtest_run").fetchone()[0]
        assert count == 1

    def test_idempotent_when_column_already_present(self):
        conn = _conn()
        create_schema(conn)
        create_schema(conn)  # must not raise on a database that already has the column
        columns = {row[0] for row in conn.execute("DESCRIBE fact_backtest_run").fetchall()}
        assert "strategy_name" in columns
