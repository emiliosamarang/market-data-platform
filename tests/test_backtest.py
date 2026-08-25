import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from backtest import load_history, main
from config import ACCOUNT_SIZE
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

        result = main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        assert ("ETHUSDT", "4h") in processed
        assert ("ETHUSDT", "1h") in processed
        assert not any(symbol == "BTCUSDT" for symbol, _ in processed)
        run_backtest_mock.assert_called_once_with("ETHUSDT", "df", "df")
        assert result == 1

    def test_no_combined_report_for_single_symbol(self, monkeypatch):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock()
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        result = main(["BTCUSDT"], days=10, refresh=False)

        assert log_report_mock.call_count == 1
        assert result == 0

    def test_combined_report_for_multiple_symbols(self, monkeypatch):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock()
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        result = main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        assert log_report_mock.call_count == 3  # BTCUSDT + ETHUSDT + combined
        assert log_report_mock.call_args_list[-1][0][0] == "ALL SYMBOLS COMBINED"
        assert result == 0

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


# ---------------------------------------------------------------------------
# Incomplete-result reporting: skipped symbols must be impossible to miss,
# and the process must signal failure via its exit code.
# ---------------------------------------------------------------------------

class TestIncompleteReporting:
    def test_combined_label_marks_incomplete_when_a_symbol_is_skipped(self, monkeypatch):
        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            if symbol == "BTCUSDT":
                raise MissingDataError("missing BTCUSDT 4h candles")
            return "df"

        log_report_mock = MagicMock()
        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        combined_label = log_report_mock.call_args_list[-1][0][0]
        assert "INCOMPLETE" in combined_label
        assert "1/2" in combined_label

    def test_combined_label_unmarked_when_nothing_skipped(self, monkeypatch):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock()
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        combined_label = log_report_mock.call_args_list[-1][0][0]
        assert combined_label == "ALL SYMBOLS COMBINED"

    def test_exit_code_zero_when_nothing_skipped(self, monkeypatch):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", MagicMock())
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        assert main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False) == 0

    def test_exit_code_nonzero_when_a_symbol_is_skipped(self, monkeypatch):
        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            if symbol == "BTCUSDT":
                raise MissingDataError("missing BTCUSDT")
            return "df"

        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", MagicMock())
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        assert main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False) == 1

    def test_generic_load_failure_also_counts_as_skipped(self, monkeypatch):
        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            if symbol == "BTCUSDT":
                raise ValueError("boom")
            return "df"

        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock()
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        result = main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        assert result == 1
        assert "INCOMPLETE" in log_report_mock.call_args_list[-1][0][0]

    def test_single_skipped_symbol_still_reports_prominently(self, monkeypatch, caplog):
        # No "ALL SYMBOLS COMBINED" report exists for a single symbol — the
        # skip must still be impossible to miss even without one.
        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            raise MissingDataError("missing BTCUSDT 4h candles between X and Y")

        log_report_mock = MagicMock()
        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        with caplog.at_level(logging.ERROR, logger="backtest"):
            result = main(["BTCUSDT"], days=10, refresh=False)

        assert result == 1
        log_report_mock.assert_not_called()
        assert "INCOMPLETE RESULT" in caplog.text
        assert "1 of 1" in caplog.text
        assert "BTCUSDT" in caplog.text

    def test_skipped_summary_names_each_symbol_and_reason(self, monkeypatch, caplog):
        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            if symbol in ("BTCUSDT", "SOLUSDT"):
                raise MissingDataError(f"missing data for {symbol}")
            return "df"

        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", MagicMock())
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        with caplog.at_level(logging.ERROR, logger="backtest"):
            result = main(["BTCUSDT", "ETHUSDT", "SOLUSDT"], days=10, refresh=False)

        assert result == 1
        assert "INCOMPLETE RESULT — 2 of 3 symbol(s) skipped" in caplog.text
        assert "missing data for BTCUSDT" in caplog.text
        assert "missing data for SOLUSDT" in caplog.text
        assert "missing data for ETHUSDT" not in caplog.text

    def test_no_skipped_summary_logged_when_nothing_skipped(self, monkeypatch, caplog):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", MagicMock())
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        with caplog.at_level(logging.ERROR, logger="backtest"):
            main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        assert "INCOMPLETE RESULT" not in caplog.text


# ---------------------------------------------------------------------------
# Combined-report account size: each symbol trades its own ACCOUNT_SIZE-sized
# sleeve (see run_backtest/calculate_position_size) — there's no shared
# capital across symbols, so the combined report's denominator must scale
# with how many symbols actually contributed trades. A fixed ACCOUNT_SIZE
# here silently inflates "return on account" with every symbol added to the
# run (regression: reported +86.1% on 5 symbols was actually +17.2% on the
# real $5000 of capital in play — see NOTES.md).
# ---------------------------------------------------------------------------

class TestCombinedAccountSize:
    def test_scales_with_symbol_count_when_nothing_skipped(self, monkeypatch):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock()
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        main(["BTCUSDT", "ETHUSDT", "SOLUSDT"], days=10, refresh=False)

        combined_account = log_report_mock.call_args_list[-1][0][2]
        assert combined_account == 3 * ACCOUNT_SIZE

    def test_excludes_skipped_symbols_from_combined_account_size(self, monkeypatch):
        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            if symbol == "BTCUSDT":
                raise MissingDataError("missing BTCUSDT")
            return "df"

        log_report_mock = MagicMock()
        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        # Only ETHUSDT actually ran — its sleeve is the only capital in play.
        combined_account = log_report_mock.call_args_list[-1][0][2]
        assert combined_account == 1 * ACCOUNT_SIZE

    def test_per_symbol_reports_still_use_plain_account_size(self, monkeypatch):
        # The combined-report fix must not leak into individual per-symbol
        # reports — each of those really is one ACCOUNT_SIZE-sized sleeve.
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock()
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        btc_account = log_report_mock.call_args_list[0][0][2]
        eth_account = log_report_mock.call_args_list[1][0][2]
        assert btc_account == ACCOUNT_SIZE
        assert eth_account == ACCOUNT_SIZE
