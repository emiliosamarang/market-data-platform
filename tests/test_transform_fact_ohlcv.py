from datetime import datetime, timezone

import duckdb
import pandas as pd

from ingestion.base import OHLCV_COLUMNS
from ingestion.raw_store import RawStore
from transform.dims import populate_all_dims
from transform.fact_ohlcv import load_fact_ohlcv
from transform.schema import create_schema


def _rows(timestamps, symbol="BTCUSDT", interval="1h", source="binance", price=100.0):
    n = len(timestamps)
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "open": [price] * n,
            "high": [price * 1.01] * n,
            "low": [price * 0.99] * n,
            "close": [price + 1] * n,
            "volume": [10.0] * n,
            "source": [source] * n,
            "symbol": [symbol] * n,
            "interval": [interval] * n,
        }
    )[OHLCV_COLUMNS]


def _conn():
    conn = duckdb.connect(":memory:")
    conn.execute("SET TIMEZONE = 'UTC'")
    create_schema(conn)
    populate_all_dims(conn)
    return conn


class TestLoadFactOhlcv:
    def test_loads_rows_for_available_combination(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(_rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"]), asset_class="crypto")
        conn = _conn()

        total = load_fact_ohlcv(
            conn, store, ["BTCUSDT"], ["1h"], ["binance"],
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
        )

        assert total == 2
        count = conn.execute("SELECT COUNT(*) FROM fact_ohlcv").fetchone()[0]
        assert count == 2

    def test_skips_empty_combination_without_error(self, tmp_path):
        store = RawStore(base_dir=tmp_path)  # nothing written
        conn = _conn()

        total = load_fact_ohlcv(
            conn, store, ["BTCUSDT"], ["1h"], ["binance"],
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
        )

        assert total == 0

    def test_rerun_with_overlapping_range_does_not_duplicate(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(_rows(["2024-01-01T00:00:00Z"]), asset_class="crypto")
        conn = _conn()
        start = end = datetime(2024, 1, 1, tzinfo=timezone.utc)

        load_fact_ohlcv(conn, store, ["BTCUSDT"], ["1h"], ["binance"], start, end)
        load_fact_ohlcv(conn, store, ["BTCUSDT"], ["1h"], ["binance"], start, end)

        count = conn.execute("SELECT COUNT(*) FROM fact_ohlcv").fetchone()[0]
        assert count == 1

    def test_rerun_reflects_updated_values(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(_rows(["2024-01-01T00:00:00Z"], price=100.0), asset_class="crypto")
        conn = _conn()
        start = end = datetime(2024, 1, 1, tzinfo=timezone.utc)
        load_fact_ohlcv(conn, store, ["BTCUSDT"], ["1h"], ["binance"], start, end)

        store.write(_rows(["2024-01-01T00:00:00Z"], price=200.0), asset_class="crypto")  # revised value
        load_fact_ohlcv(conn, store, ["BTCUSDT"], ["1h"], ["binance"], start, end)

        close = conn.execute("SELECT close FROM fact_ohlcv").fetchone()[0]
        assert close == 201.0

    def test_multiple_sources_both_loaded(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(_rows(["2024-01-01T00:00:00Z"], source="binance"), asset_class="crypto")
        store.write(_rows(["2024-01-01T00:00:00Z"], source="kraken", price=99.0), asset_class="crypto")
        conn = _conn()

        total = load_fact_ohlcv(
            conn, store, ["BTCUSDT"], ["1h"], ["binance", "kraken"],
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, tzinfo=timezone.utc),
        )

        assert total == 2
        sources = {row[0] for row in conn.execute("SELECT source FROM fact_ohlcv").fetchall()}
        assert sources == {"binance", "kraken"}

    def test_canonical_view_prefers_binance(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(_rows(["2024-01-01T00:00:00Z"], source="binance", price=100.0), asset_class="crypto")
        store.write(_rows(["2024-01-01T00:00:00Z"], source="kraken", price=999.0), asset_class="crypto")
        conn = _conn()
        load_fact_ohlcv(
            conn, store, ["BTCUSDT"], ["1h"], ["binance", "kraken"],
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, tzinfo=timezone.utc),
        )

        row = conn.execute("SELECT source, close FROM fact_ohlcv_canonical").fetchone()

        assert row[0] == "binance"
        assert row[1] == 101.0

    def test_canonical_view_falls_back_when_binance_missing(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(_rows(["2024-01-01T00:00:00Z"], source="kraken", price=99.0), asset_class="crypto")
        conn = _conn()
        load_fact_ohlcv(
            conn, store, ["BTCUSDT"], ["1h"], ["kraken"],
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 1, tzinfo=timezone.utc),
        )

        row = conn.execute("SELECT source FROM fact_ohlcv_canonical").fetchone()

        assert row[0] == "kraken"
