from datetime import datetime, timezone

import duckdb
import pandas as pd

from transform.dims import populate_all_dims
from transform.fact_signal import load_fact_signal
from transform.schema import create_schema


def _conn():
    conn = duckdb.connect(":memory:")
    conn.execute("SET TIMEZONE = 'UTC'")
    create_schema(conn)
    populate_all_dims(conn)
    return conn


def _insert_ohlcv(conn, symbol, interval, timestamps, prices, source="binance"):
    rows = pd.DataFrame({
        "symbol": symbol, "interval": interval, "source": source,
        "timestamp": pd.to_datetime(timestamps, utc=True),
        "date": pd.to_datetime(timestamps, utc=True).date,
        "open": prices, "high": [p * 1.01 for p in prices], "low": [p * 0.99 for p in prices],
        "close": [p + 1 for p in prices], "volume": [10.0] * len(prices),
    })
    conn.register("_seed", rows)
    conn.execute(
        "INSERT INTO fact_ohlcv SELECT symbol, interval, source, timestamp, date, open, high, low, close, volume "
        "FROM _seed"
    )
    conn.unregister("_seed")


def _seed_both_intervals(conn, symbol="BTCUSDT"):
    n_1h, n_4h = 120, 60
    ts_1h = pd.date_range("2024-01-01", periods=n_1h, freq="1h", tz="UTC")
    ts_4h = pd.date_range("2024-01-01", periods=n_4h, freq="4h", tz="UTC")
    prices_1h = [100.0 + i * 0.2 for i in range(n_1h)]
    prices_4h = [100.0 + i * 0.8 for i in range(n_4h)]
    _insert_ohlcv(conn, symbol, "1h", ts_1h, prices_1h)
    _insert_ohlcv(conn, symbol, "4h", ts_4h, prices_4h)
    return ts_1h[0].to_pydatetime(), ts_1h[-1].to_pydatetime()


class TestLoadFactSignal:
    def test_loads_rows_for_lower_interval_only(self):
        conn = _conn()
        start, end = _seed_both_intervals(conn)

        total = load_fact_signal(conn, ["BTCUSDT"], "1h", "4h", start, end)

        count = conn.execute("SELECT COUNT(*) FROM fact_signal").fetchone()[0]
        assert total == count
        assert count > 0
        intervals = {r[0] for r in conn.execute("SELECT DISTINCT interval FROM fact_signal").fetchall()}
        assert intervals == {"1h"}

    def test_no_data_skips_without_error(self):
        conn = _conn()

        total = load_fact_signal(
            conn, ["BTCUSDT"], "1h", "4h",
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc),
        )

        assert total == 0

    def test_higher_trend_and_signal_are_valid_categories(self):
        conn = _conn()
        start, end = _seed_both_intervals(conn)

        load_fact_signal(conn, ["BTCUSDT"], "1h", "4h", start, end)

        trends = {r[0] for r in conn.execute("SELECT DISTINCT higher_trend FROM fact_signal").fetchall()}
        signals = {r[0] for r in conn.execute("SELECT DISTINCT signal FROM fact_signal").fetchall()}
        assert trends <= {"BULLISH", "BEARISH", "NEUTRAL"}
        assert signals <= {"BUY", "SELL", "HOLD", "NOT ENOUGH DATA"}

    def test_rerun_does_not_duplicate(self):
        conn = _conn()
        start, end = _seed_both_intervals(conn)

        load_fact_signal(conn, ["BTCUSDT"], "1h", "4h", start, end)
        count_after_first = conn.execute("SELECT COUNT(*) FROM fact_signal").fetchone()[0]

        load_fact_signal(conn, ["BTCUSDT"], "1h", "4h", start, end)
        count_after_second = conn.execute("SELECT COUNT(*) FROM fact_signal").fetchone()[0]

        assert count_after_first == count_after_second
        assert count_after_first > 0

    def test_missing_higher_interval_data_skips_symbol(self):
        conn = _conn()
        n_1h = 120
        ts_1h = pd.date_range("2024-01-01", periods=n_1h, freq="1h", tz="UTC")
        prices_1h = [100.0 + i * 0.2 for i in range(n_1h)]
        _insert_ohlcv(conn, "BTCUSDT", "1h", ts_1h, prices_1h)
        # no 4h data seeded at all

        total = load_fact_signal(
            conn, ["BTCUSDT"], "1h", "4h", ts_1h[0].to_pydatetime(), ts_1h[-1].to_pydatetime()
        )

        assert total == 0
