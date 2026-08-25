from datetime import datetime, timezone

import duckdb
import pandas as pd
import pytest

from bot import add_indicators
from transform.dims import populate_all_dims
from transform.fact_indicator import load_fact_indicator
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


class TestLoadFactIndicator:
    def test_loads_rows_once_warmup_has_passed(self):
        conn = _conn()
        n = 30  # > 20 (Volume_MA warmup) and > 14 (RSI/ATR warmup)
        timestamps = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        prices = [100.0 + i * 0.1 for i in range(n)]
        _insert_ohlcv(conn, "BTCUSDT", "1h", timestamps, prices)

        total = load_fact_indicator(
            conn, ["BTCUSDT"], ["1h"],
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, 6, tzinfo=timezone.utc),
        )

        count = conn.execute("SELECT COUNT(*) FROM fact_indicator").fetchone()[0]
        assert total == count
        assert 0 < count < n  # warmup rows dropped, but not everything

    def test_no_ohlcv_data_skips_without_error(self):
        conn = _conn()

        total = load_fact_indicator(
            conn, ["BTCUSDT"], ["1h"],
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc),
        )

        assert total == 0

    def test_rerun_does_not_duplicate(self):
        conn = _conn()
        n = 30
        timestamps = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        prices = [100.0 + i * 0.1 for i in range(n)]
        _insert_ohlcv(conn, "BTCUSDT", "1h", timestamps, prices)
        start, end = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, 6, tzinfo=timezone.utc)

        load_fact_indicator(conn, ["BTCUSDT"], ["1h"], start, end)
        count_after_first = conn.execute("SELECT COUNT(*) FROM fact_indicator").fetchone()[0]

        load_fact_indicator(conn, ["BTCUSDT"], ["1h"], start, end)
        count_after_second = conn.execute("SELECT COUNT(*) FROM fact_indicator").fetchone()[0]

        assert count_after_first == count_after_second
        assert count_after_first > 0

    def test_values_match_direct_add_indicators_call(self):
        conn = _conn()
        n = 30
        timestamps = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        prices = [100.0 + i * 0.1 for i in range(n)]
        _insert_ohlcv(conn, "BTCUSDT", "1h", timestamps, prices)
        start, end = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, 6, tzinfo=timezone.utc)

        load_fact_indicator(conn, ["BTCUSDT"], ["1h"], start, end)

        # Reconstruct the same OHLC frame directly and compute indicators
        # via bot.py — proves the loader isn't drifting from it.
        df = pd.DataFrame(
            {
                "Open": prices, "High": [p * 1.01 for p in prices], "Low": [p * 0.99 for p in prices],
                "Close": [p + 1 for p in prices], "Volume": [10.0] * n,
            },
            index=timestamps,
        )
        expected = add_indicators(df).iloc[-1]

        row = conn.execute(
            "SELECT ema_20, rsi FROM fact_indicator WHERE timestamp = ?", [timestamps[-1]]
        ).fetchone()

        assert row[0] == pytest.approx(expected["EMA_20"])
        assert row[1] == pytest.approx(expected["RSI"])
