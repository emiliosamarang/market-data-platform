from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from backtest import load_history, main
from ingestion.base import OHLCV_COLUMNS
from ingestion.raw_store import MissingDataError


def _raw_df(n=3, symbol="BTCUSDT", interval="1h", start="2024-01-01T00:00:00Z"):
    """A DataFrame in RawStore.read()'s output schema (lowercase OHLCV columns)."""
    ts = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [10.0] * n,
            "source": ["binance"] * n,
            "symbol": [symbol] * n,
            "interval": [interval] * n,
        }
    )[OHLCV_COLUMNS]


# ---------------------------------------------------------------------------
# load_history
# ---------------------------------------------------------------------------

class TestLoadHistory:
    def test_reshapes_to_bot_expected_schema(self):
        store = MagicMock()
        store.read.return_value = _raw_df()

        result = load_history(
            "BTCUSDT", "1h",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
            store, refresh=False,
        )

        assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert result.index.name == "timestamp"
        assert len(result) == 3

    def test_missing_data_without_refresh_propagates_and_does_not_write(self):
        store = MagicMock()
        store.read.side_effect = MissingDataError("missing stuff")

        with pytest.raises(MissingDataError):
            load_history(
                "BTCUSDT", "1h",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
                store, refresh=False,
            )
        store.write.assert_not_called()

    def test_missing_data_with_refresh_backfills_then_reads_again(self):
        store = MagicMock()
        store.read.side_effect = [MissingDataError("missing"), _raw_df()]
        source = MagicMock()
        source.fetch_ohlcv.return_value = _raw_df()

        result = load_history(
            "BTCUSDT", "1h",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
            store, refresh=True, source=source,
        )

        source.fetch_ohlcv.assert_called_once()
        store.write.assert_called_once()
        assert store.read.call_count == 2
        assert len(result) == 3

    def test_refresh_constructs_binance_source_when_not_provided(self, monkeypatch):
        store = MagicMock()
        store.read.side_effect = [MissingDataError("missing"), _raw_df()]
        fake_source_cls = MagicMock()
        fake_source_cls.return_value.fetch_ohlcv.return_value = _raw_df()
        monkeypatch.setattr("backtest.BinanceSource", fake_source_cls)

        load_history(
            "BTCUSDT", "1h",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
            store, refresh=True,
        )

        fake_source_cls.assert_called_once_with()

    def test_data_still_missing_after_refresh_propagates(self):
        store = MagicMock()
        store.read.side_effect = [MissingDataError("missing"), MissingDataError("still missing")]
        source = MagicMock()
        source.fetch_ohlcv.return_value = _raw_df()

        with pytest.raises(MissingDataError):
            load_history(
                "BTCUSDT", "1h",
                datetime(2024, 1, 1, tzinfo=timezone.utc),
                datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
                store, refresh=True, source=source,
            )


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_missing_data_symbol_is_skipped_others_continue(self, monkeypatch):
        processed = []

        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            if symbol == "BTCUSDT":
                raise MissingDataError("missing BTCUSDT")
            processed.append((symbol, interval))
            return "df"

        run_backtest_mock = MagicMock(return_value=[])

        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", run_backtest_mock)
        monkeypatch.setattr("backtest.log_report", MagicMock())
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        assert ("ETHUSDT", "4h") in processed
        assert ("ETHUSDT", "1h") in processed
        assert not any(symbol == "BTCUSDT" for symbol, _ in processed)
        run_backtest_mock.assert_called_once_with("ETHUSDT", "df", "df")

    def test_no_combined_report_for_single_symbol(self, monkeypatch):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock()
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        main(["BTCUSDT"], days=10, refresh=False)

        assert log_report_mock.call_count == 1

    def test_combined_report_for_multiple_symbols(self, monkeypatch):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock()
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        assert log_report_mock.call_count == 3  # BTCUSDT + ETHUSDT + combined
        assert log_report_mock.call_args_list[-1][0][0] == "ALL SYMBOLS COMBINED"

    def test_refresh_constructs_one_shared_binance_source(self, monkeypatch):
        source_calls = []

        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            source_calls.append(source)
            return "df"

        fake_source_cls = MagicMock()
        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", MagicMock())
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        monkeypatch.setattr("backtest.BinanceSource", fake_source_cls)

        main(["BTCUSDT"], days=10, refresh=True)

        fake_source_cls.assert_called_once_with()
        assert all(s is fake_source_cls.return_value for s in source_calls)

    def test_without_refresh_no_binance_source_constructed(self, monkeypatch):
        source_calls = []

        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            source_calls.append(source)
            return "df"

        fake_source_cls = MagicMock()
        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", MagicMock())
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        monkeypatch.setattr("backtest.BinanceSource", fake_source_cls)

        main(["BTCUSDT"], days=10, refresh=False)

        fake_source_cls.assert_not_called()
        assert all(s is None for s in source_calls)
