from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from ingestion.base import OHLCV_COLUMNS
from ingestion.raw_store import MissingDataError, RawStore


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


class TestWrite:
    def test_creates_expected_partition_path(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        df = _rows(["2024-01-01T00:00:00Z"], symbol="BTCUSDT", interval="1h")

        store.write(df, asset_class="crypto")

        expected = tmp_path / "crypto" / "binance" / "BTCUSDT" / "1h" / "2024-01-01.parquet"
        assert expected.exists()

    def test_written_rows_match_input(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        df = _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"])

        store.write(df, asset_class="crypto")

        path = tmp_path / "crypto" / "binance" / "BTCUSDT" / "1h" / "2024-01-01.parquet"
        result = pd.read_parquet(path)
        assert len(result) == 2
        assert list(result.columns) == OHLCV_COLUMNS

    def test_spans_multiple_days_into_separate_files(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        df = _rows(["2024-01-01T23:00:00Z", "2024-01-02T00:00:00Z"])

        store.write(df, asset_class="crypto")

        base = tmp_path / "crypto" / "binance" / "BTCUSDT" / "1h"
        assert (base / "2024-01-01.parquet").exists()
        assert (base / "2024-01-02.parquet").exists()

    def test_empty_dataframe_writes_nothing(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(_rows([]), asset_class="crypto")
        assert not (tmp_path / "crypto").exists()


class TestIdempotency:
    def test_writing_same_range_twice_produces_no_duplicates(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        df = _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z", "2024-01-01T02:00:00Z"])

        store.write(df, asset_class="crypto")
        store.write(df, asset_class="crypto")

        path = tmp_path / "crypto" / "binance" / "BTCUSDT" / "1h" / "2024-01-01.parquet"
        result = pd.read_parquet(path)
        assert len(result) == 3
        assert result["timestamp"].is_unique

    def test_writing_overlapping_ranges_merges_and_dedupes(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        first = _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"])
        second = _rows(["2024-01-01T01:00:00Z", "2024-01-01T02:00:00Z"])  # 01:00 overlaps

        store.write(first, asset_class="crypto")
        store.write(second, asset_class="crypto")

        path = tmp_path / "crypto" / "binance" / "BTCUSDT" / "1h" / "2024-01-01.parquet"
        result = pd.read_parquet(path)
        assert len(result) == 3
        assert result["timestamp"].is_unique
        assert result["timestamp"].is_monotonic_increasing

    def test_result_sorted_even_if_written_out_of_order(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        later = _rows(["2024-01-01T02:00:00Z"])
        earlier = _rows(["2024-01-01T00:00:00Z"])

        store.write(later, asset_class="crypto")
        store.write(earlier, asset_class="crypto")

        path = tmp_path / "crypto" / "binance" / "BTCUSDT" / "1h" / "2024-01-01.parquet"
        result = pd.read_parquet(path)
        assert result["timestamp"].is_monotonic_increasing

    def test_repeated_write_is_stable_across_many_calls(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        df = _rows(["2024-01-01T00:00:00Z"])

        for _ in range(5):
            store.write(df, asset_class="crypto")

        path = tmp_path / "crypto" / "binance" / "BTCUSDT" / "1h" / "2024-01-01.parquet"
        result = pd.read_parquet(path)
        assert len(result) == 1

    def test_different_symbols_do_not_collide(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        btc = _rows(["2024-01-01T00:00:00Z"], symbol="BTCUSDT")
        eth = _rows(["2024-01-01T00:00:00Z"], symbol="ETHUSDT")

        store.write(btc, asset_class="crypto")
        store.write(eth, asset_class="crypto")

        btc_path = tmp_path / "crypto" / "binance" / "BTCUSDT" / "1h" / "2024-01-01.parquet"
        eth_path = tmp_path / "crypto" / "binance" / "ETHUSDT" / "1h" / "2024-01-01.parquet"
        assert len(pd.read_parquet(btc_path)) == 1
        assert len(pd.read_parquet(eth_path)) == 1

    def test_newer_write_overwrites_value_for_same_timestamp(self, tmp_path):
        # keep="last": a re-fetched candle with a revised close should
        # replace, not duplicate, the previously stored row.
        store = RawStore(base_dir=tmp_path)
        stale = _rows(["2024-01-01T00:00:00Z"], price=100.0)
        fresh = _rows(["2024-01-01T00:00:00Z"], price=200.0)

        store.write(stale, asset_class="crypto")
        store.write(fresh, asset_class="crypto")

        path = tmp_path / "crypto" / "binance" / "BTCUSDT" / "1h" / "2024-01-01.parquet"
        result = pd.read_parquet(path)
        assert len(result) == 1
        assert result["open"].iloc[0] == 200.0


class TestRead:
    def test_reads_full_range_from_single_partition(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(
            _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z", "2024-01-01T02:00:00Z"]),
            asset_class="crypto",
        )

        result = store.read(
            "crypto", "binance", "BTCUSDT", "1h",
            datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
        )

        assert len(result) == 3
        assert list(result.columns) == OHLCV_COLUMNS
        assert result["timestamp"].is_monotonic_increasing

    def test_reads_across_multiple_day_partitions(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(_rows(["2024-01-01T23:00:00Z"]), asset_class="crypto")
        store.write(_rows(["2024-01-02T00:00:00Z", "2024-01-02T01:00:00Z"]), asset_class="crypto")

        result = store.read(
            "crypto", "binance", "BTCUSDT", "1h",
            datetime(2024, 1, 1, 23, tzinfo=timezone.utc),
            datetime(2024, 1, 2, 1, tzinfo=timezone.utc),
        )

        assert len(result) == 3
        assert result["timestamp"].is_monotonic_increasing

    def test_excludes_data_outside_requested_range(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(
            _rows([
                "2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z",
                "2024-01-01T02:00:00Z", "2024-01-01T03:00:00Z",
            ]),
            asset_class="crypto",
        )

        result = store.read(
            "crypto", "binance", "BTCUSDT", "1h",
            datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
        )

        assert len(result) == 2
        assert result["timestamp"].min() == pd.Timestamp("2024-01-01T01:00:00Z")
        assert result["timestamp"].max() == pd.Timestamp("2024-01-01T02:00:00Z")

    def test_missing_partition_raises(self, tmp_path):
        store = RawStore(base_dir=tmp_path)

        with pytest.raises(MissingDataError):
            store.read(
                "crypto", "binance", "BTCUSDT", "1h",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
            )

    def test_gap_within_existing_partition_raises(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        # hour 01:00 is missing between 00:00 and 02:00.
        store.write(
            _rows(["2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z"]),
            asset_class="crypto",
        )

        with pytest.raises(MissingDataError):
            store.read(
                "crypto", "binance", "BTCUSDT", "1h",
                datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
                datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
            )

    def test_error_message_names_missing_timestamp_and_hints_refresh(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(
            _rows(["2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z"]),
            asset_class="crypto",
        )

        with pytest.raises(MissingDataError) as excinfo:
            store.read(
                "crypto", "binance", "BTCUSDT", "1h",
                datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
                datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
            )

        message = str(excinfo.value)
        assert "BTCUSDT" in message
        assert "1h" in message
        assert "2024-01-01T01:00:00" in message
        assert "--refresh" in message
        assert "python -m ingestion.load" in message

    def test_no_data_at_all_lists_full_range_as_missing(self, tmp_path):
        store = RawStore(base_dir=tmp_path)

        with pytest.raises(MissingDataError) as excinfo:
            store.read(
                "crypto", "binance", "BTCUSDT", "1h",
                datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
                datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
            )

        assert "3 candle(s)" in str(excinfo.value)

    def test_different_source_is_isolated(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(
            _rows(["2024-01-01T00:00:00Z"], source="binance"),
            asset_class="crypto",
        )

        with pytest.raises(MissingDataError):
            store.read(
                "crypto", "kraken", "BTCUSDT", "1h",
                datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
                datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            )

    def test_boundary_not_aligned_to_grid_does_not_false_positive(self, tmp_path):
        # start/end don't fall exactly on the hourly grid — the expected
        # grid should be ceil(start)/floor(end), not start/end verbatim.
        store = RawStore(base_dir=tmp_path)
        store.write(
            _rows(["2024-01-01T01:00:00Z", "2024-01-01T02:00:00Z"]),
            asset_class="crypto",
        )

        result = store.read(
            "crypto", "binance", "BTCUSDT", "1h",
            datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, 30, tzinfo=timezone.utc),
        )

        assert len(result) == 2

    def test_start_after_end_raises_value_error(self, tmp_path):
        store = RawStore(base_dir=tmp_path)

        with pytest.raises(ValueError):
            store.read(
                "crypto", "binance", "BTCUSDT", "1h",
                datetime(2024, 1, 2, tzinfo=timezone.utc),
                datetime(2024, 1, 1, tzinfo=timezone.utc),
            )
