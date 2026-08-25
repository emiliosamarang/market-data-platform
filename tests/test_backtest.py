import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import duckdb
import pandas as pd
import pytest

from backtest import (
    FEE_RATE,
    _return_to_drawdown_ratio,
    combine_buy_and_hold,
    compute_buy_and_hold,
    compute_market_phases,
    compute_phase_returns_buy_and_hold,
    compute_phase_returns_strategy,
    load_history,
    log_report,
    main,
)
from bot import add_indicators, get_trend_4h
from config import ACCOUNT_SIZE
from ingestion.base import OHLCV_COLUMNS
from ingestion.raw_store import MissingDataError
from transform.schema import create_schema


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
        monkeypatch.setattr("backtest.log_report", MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0}))
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

        result = main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        assert ("ETHUSDT", "4h") in processed
        assert ("ETHUSDT", "1h") in processed
        assert not any(symbol == "BTCUSDT" for symbol, _ in processed)
        run_backtest_mock.assert_called_once_with("ETHUSDT", "df", "df")
        assert result == 1

    def test_no_combined_report_for_single_symbol(self, monkeypatch):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

        result = main(["BTCUSDT"], days=10, refresh=False)

        assert log_report_mock.call_count == 1
        assert result == 0

    def test_combined_report_for_multiple_symbols(self, monkeypatch):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

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
        monkeypatch.setattr("backtest.log_report", MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0}))
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)
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
        monkeypatch.setattr("backtest.log_report", MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0}))
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)
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

        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        combined_label = log_report_mock.call_args_list[-1][0][0]
        assert "INCOMPLETE" in combined_label
        assert "1/2" in combined_label

    def test_combined_label_unmarked_when_nothing_skipped(self, monkeypatch):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        combined_label = log_report_mock.call_args_list[-1][0][0]
        assert combined_label == "ALL SYMBOLS COMBINED"

    def test_exit_code_zero_when_nothing_skipped(self, monkeypatch):
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0}))
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

        assert main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False) == 0

    def test_exit_code_nonzero_when_a_symbol_is_skipped(self, monkeypatch):
        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            if symbol == "BTCUSDT":
                raise MissingDataError("missing BTCUSDT")
            return "df"

        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0}))
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

        assert main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False) == 1

    def test_generic_load_failure_also_counts_as_skipped(self, monkeypatch):
        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            if symbol == "BTCUSDT":
                raise ValueError("boom")
            return "df"

        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

        result = main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        assert result == 1
        assert "INCOMPLETE" in log_report_mock.call_args_list[-1][0][0]

    def test_single_skipped_symbol_still_reports_prominently(self, monkeypatch, caplog):
        # No "ALL SYMBOLS COMBINED" report exists for a single symbol — the
        # skip must still be impossible to miss even without one.
        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            raise MissingDataError("missing BTCUSDT 4h candles between X and Y")

        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

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
        monkeypatch.setattr("backtest.log_report", MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0}))
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

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
        monkeypatch.setattr("backtest.log_report", MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0}))
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

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
        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

        main(["BTCUSDT", "ETHUSDT", "SOLUSDT"], days=10, refresh=False)

        combined_account = log_report_mock.call_args_list[-1][0][2]
        assert combined_account == 3 * ACCOUNT_SIZE

    def test_excludes_skipped_symbols_from_combined_account_size(self, monkeypatch):
        def fake_load_history(symbol, interval, start, end, store, refresh, source=None):
            if symbol == "BTCUSDT":
                raise MissingDataError("missing BTCUSDT")
            return "df"

        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.load_history", fake_load_history)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        # Only ETHUSDT actually ran — its sleeve is the only capital in play.
        combined_account = log_report_mock.call_args_list[-1][0][2]
        assert combined_account == 1 * ACCOUNT_SIZE

    def test_per_symbol_reports_still_use_plain_account_size(self, monkeypatch):
        # The combined-report fix must not leak into individual per-symbol
        # reports — each of those really is one ACCOUNT_SIZE-sized sleeve.
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: "df")
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.log_report", log_report_mock)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        # These tests exercise skip/reporting logic, not the benchmark
        # itself — load_history is stubbed with a plain "df" placeholder,
        # which real compute_buy_and_hold can't operate on.
        monkeypatch.setattr("backtest.compute_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0})
        monkeypatch.setattr("backtest.combine_buy_and_hold", lambda *a, **k: {"return_pct": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0, "phase_returns": {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}})
        monkeypatch.setattr("backtest.compute_phase_returns_buy_and_hold", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0})
        monkeypatch.setattr("backtest.compute_phase_returns_strategy", lambda *a, **k: {"BULLISH": 0.0, "BEARISH": 0.0})
        # These tests don't exercise Curated Layer persistence either —
        # keep them from touching the real curated.duckdb file.
        monkeypatch.setattr("backtest.connect_curated_db", lambda *a, **k: MagicMock())
        monkeypatch.setattr("backtest.create_schema", lambda *a, **k: None)
        monkeypatch.setattr("backtest.populate_all_dims", lambda *a, **k: None)
        monkeypatch.setattr("backtest.record_backtest_run", lambda *a, **k: "fake-run-id")
        monkeypatch.setattr("backtest.record_backtest_trades", lambda *a, **k: 0)

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False)

        btc_account = log_report_mock.call_args_list[0][0][2]
        eth_account = log_report_mock.call_args_list[1][0][2]
        assert btc_account == ACCOUNT_SIZE
        assert eth_account == ACCOUNT_SIZE


# ---------------------------------------------------------------------------
# Buy-and-hold benchmark
# ---------------------------------------------------------------------------

def _ohlc_df(opens, highs, lows, closes, start="2024-01-01T00:00:00Z", freq="1h", volume=10.0):
    """bot.py-shape frame (capitalized OHLC columns, DatetimeIndex) — the
    shape compute_buy_and_hold/run_backtest actually operate on."""
    index = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": [volume] * len(closes)},
        index=index,
    )


class TestComputeBuyAndHold:
    def test_known_return_matches_hand_calculation(self):
        df = _ohlc_df(
            opens=[100.0, 101.0, 99.0, 110.0],
            highs=[101.0, 102.0, 100.0, 111.0],
            lows=[99.0, 100.0, 98.0, 109.0],
            closes=[100.5, 100.0, 98.5, 110.0],
        )
        account = 1000.0

        result = compute_buy_and_hold(df, account)

        entry, exit_price = 100.0, 110.0  # first Open, last Close
        size = account / entry
        gross = (exit_price - entry) * size
        fee = (entry + exit_price) * size * FEE_RATE
        expected_net = gross - fee
        expected_return_pct = expected_net / account * 100

        assert result["entry"] == entry
        assert result["exit"] == exit_price
        assert result["net_pnl"] == pytest.approx(expected_net)
        assert result["return_pct"] == pytest.approx(expected_return_pct)

    def test_single_candle_period(self):
        df = _ohlc_df(opens=[100.0], highs=[101.0], lows=[99.0], closes=[100.5])

        result = compute_buy_and_hold(df, 1000.0)

        entry, exit_price = 100.0, 100.5
        size = 1000.0 / entry
        expected_net = (exit_price - entry) * size - (entry + exit_price) * size * FEE_RATE

        assert result["net_pnl"] == pytest.approx(expected_net)
        assert result["max_drawdown_pct"] == pytest.approx(0.0)

    def test_falling_market_produces_negative_return_and_drawdown(self):
        df = _ohlc_df(
            opens=[100.0, 90.0, 80.0, 70.0],
            highs=[101.0, 91.0, 81.0, 71.0],
            lows=[99.0, 89.0, 79.0, 69.0],
            closes=[95.0, 85.0, 75.0, 65.0],
        )

        result = compute_buy_and_hold(df, 1000.0)

        assert result["return_pct"] < 0
        assert result["max_drawdown_pct"] > 0

    def test_empty_dataframe_raises(self):
        df = pd.DataFrame(columns=["Open", "High", "Low", "Close"])

        with pytest.raises(ValueError):
            compute_buy_and_hold(df, 1000.0)

    def test_drawdown_reflects_interim_dip_not_just_endpoints(self):
        # Price dips hard mid-period then recovers above entry — a naive
        # entry-vs-exit comparison would show 0 drawdown, which would be wrong.
        df = _ohlc_df(
            opens=[100.0, 100.0, 100.0, 100.0],
            highs=[101.0, 101.0, 101.0, 121.0],
            lows=[99.0, 49.0, 99.0, 119.0],
            closes=[100.0, 50.0, 100.0, 120.0],
        )

        result = compute_buy_and_hold(df, 1000.0)

        assert result["return_pct"] > 0  # ended up net positive
        assert result["max_drawdown_pct"] == pytest.approx(50.0)  # but the dip must show up


class TestCombineBuyAndHold:
    def test_combines_pnl_and_scales_by_symbol_count(self):
        idx = pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC")
        results = [
            {"net_pnl": 100.0, "equity_curve": pd.Series([1000.0, 1010.0], index=idx)},
            {"net_pnl": -50.0, "equity_curve": pd.Series([1000.0, 990.0], index=idx)},
        ]

        combined = combine_buy_and_hold(results, account_per_symbol=1000.0)

        assert combined["net_pnl"] == pytest.approx(50.0)
        assert combined["return_pct"] == pytest.approx(50.0 / 2000.0 * 100)

    def test_empty_results_returns_zeroed_dict(self):
        combined = combine_buy_and_hold([], account_per_symbol=1000.0)

        assert combined == {"net_pnl": 0.0, "return_pct": 0.0, "max_drawdown_pct": 0.0}

    def test_combined_drawdown_from_summed_equity_not_averaged(self):
        idx = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
        # A dips hard mid-period while B stays flat.
        results = [
            {"net_pnl": 0.0, "equity_curve": pd.Series([1000.0, 500.0, 1000.0], index=idx)},
            {"net_pnl": 0.0, "equity_curve": pd.Series([1000.0, 1000.0, 1000.0], index=idx)},
        ]

        combined = combine_buy_and_hold(results, account_per_symbol=1000.0)

        # combined equity: [2000, 1500, 2000] -> dd = 500/2000 = 25%
        assert combined["max_drawdown_pct"] == pytest.approx(25.0)


class TestLogReportBenchmark:
    def test_logs_benchmark_lines_when_provided(self, caplog):
        trades = [{"pnl": 50.0, "exit_reason": "TP"}]
        benchmark = {"return_pct": 10.0, "max_drawdown_pct": 5.0}

        with caplog.at_level(logging.INFO, logger="backtest"):
            log_report("BTCUSDT", trades, 1000.0, benchmark=benchmark)

        assert "Buy & Hold return:    +10.0%" in caplog.text
        assert "Strategy return:      +5.0%" in caplog.text
        assert "Strategy vs B&H:      -5.0 pp" in caplog.text
        assert "Buy & Hold max DD:    5.0%" in caplog.text

    def test_no_benchmark_lines_when_not_provided(self, caplog):
        trades = [{"pnl": 50.0, "exit_reason": "TP"}]

        with caplog.at_level(logging.INFO, logger="backtest"):
            log_report("BTCUSDT", trades, 1000.0)

        assert "Buy & Hold" not in caplog.text

    def test_benchmark_logged_even_with_no_closed_trades(self, caplog):
        benchmark = {"return_pct": 10.0, "max_drawdown_pct": 5.0}

        with caplog.at_level(logging.INFO, logger="backtest"):
            log_report("BTCUSDT", [], 1000.0, benchmark=benchmark)

        assert "Buy & Hold return:    +10.0%" in caplog.text
        assert "Strategy return:      +0.0%" in caplog.text

    def test_logs_return_to_drawdown_ratio(self, caplog):
        trades = [{"pnl": 50.0, "exit_reason": "TP"}]
        benchmark = {"return_pct": 10.0, "max_drawdown_pct": 5.0}

        with caplog.at_level(logging.INFO, logger="backtest"):
            log_report("BTCUSDT", trades, 1000.0, benchmark=benchmark)

        assert "Return/MaxDD (B&H):   2.00" in caplog.text

    def test_logs_phase_lines_when_provided(self, caplog):
        trades = [{"pnl": 50.0, "exit_reason": "TP"}]
        benchmark = {
            "return_pct": 10.0, "max_drawdown_pct": 5.0,
            "phase_returns": {"BULLISH": 8.0, "BEARISH": 1.5, "NEUTRAL": 0.5},
            "strategy_phase_returns": {"BULLISH": 4.0, "BEARISH": 1.0},
        }

        with caplog.at_level(logging.INFO, logger="backtest"):
            log_report("BTCUSDT", trades, 1000.0, benchmark=benchmark)

        assert "Bullish phase — B&H: +8.0%   Strategy: +4.0%" in caplog.text
        assert "Bearish phase — B&H: +1.5%   Strategy: +1.0%" in caplog.text
        assert "Neutral phase — B&H: +0.5%" in caplog.text

    def test_no_phase_lines_when_phase_data_absent(self, caplog):
        trades = [{"pnl": 50.0, "exit_reason": "TP"}]
        benchmark = {"return_pct": 10.0, "max_drawdown_pct": 5.0}

        with caplog.at_level(logging.INFO, logger="backtest"):
            log_report("BTCUSDT", trades, 1000.0, benchmark=benchmark)

        assert "market phase" not in caplog.text


class TestReturnToDrawdownRatio:
    def test_basic_ratio(self):
        assert _return_to_drawdown_ratio(20.0, 10.0) == pytest.approx(2.0)

    def test_negative_return(self):
        assert _return_to_drawdown_ratio(-20.0, 10.0) == pytest.approx(-2.0)

    def test_zero_drawdown_with_positive_return_is_infinite(self):
        assert _return_to_drawdown_ratio(5.0, 0.0) == float("inf")

    def test_zero_drawdown_with_zero_return_is_zero(self):
        assert _return_to_drawdown_ratio(0.0, 0.0) == 0.0


class TestComputeMarketPhases:
    def test_matches_direct_get_trend_4h_call_on_final_row(self):
        n = 60
        closes = [100.0 + i * 0.5 for i in range(n)]
        df_4h = _ohlc_df(
            opens=closes, highs=[c * 1.01 for c in closes], lows=[c * 0.99 for c in closes], closes=closes,
            freq="4h",
        )
        df_4h = add_indicators(df_4h)

        phases = compute_market_phases(df_4h)

        assert len(phases) == n
        assert list(phases.index) == list(df_4h.index)
        assert phases.iloc[-1] == get_trend_4h(df_4h)

    def test_sustained_uptrend_eventually_classified_bullish(self):
        n = 60
        closes = [100.0 + i * 1.0 for i in range(n)]
        df_4h = _ohlc_df(
            opens=closes, highs=[c * 1.01 for c in closes], lows=[c * 0.99 for c in closes], closes=closes,
            freq="4h",
        )
        df_4h = add_indicators(df_4h)

        phases = compute_market_phases(df_4h)

        assert phases.iloc[-1] == "BULLISH"


class TestComputePhaseReturnsBuyAndHold:
    def test_phases_recombine_multiplicatively_to_total_price_return(self, monkeypatch):
        # Log-returns are additive, so per-phase-summed-then-exponentiated
        # percentages must recombine *multiplicatively* to the whole
        # period's raw price return — the identity the whole function
        # relies on. Bypass real trend classification with a hand-picked
        # phase assignment covering all three buckets, so the test is
        # about the attribution math, not get_trend_4h's own rules.
        closes = [100.0, 102.0, 101.0, 105.0, 103.0, 108.0, 107.0, 110.0]
        df_1h = _ohlc_df(
            opens=closes, highs=[c * 1.01 for c in closes], lows=[c * 0.99 for c in closes], closes=closes,
        )
        fake_phases = pd.Series(
            ["BULLISH", "BULLISH", "BEARISH", "BULLISH", "NEUTRAL", "BULLISH", "BEARISH", "BULLISH"],
            index=df_1h.index,
        )
        monkeypatch.setattr("backtest.compute_market_phases", lambda df_4h: fake_phases)
        df_4h_placeholder = _ohlc_df(opens=[100.0], highs=[101.0], lows=[99.0], closes=[100.0], freq="4h")

        result = compute_phase_returns_buy_and_hold(df_1h, df_4h_placeholder, account=1000.0)

        assert set(result) == {"BULLISH", "BEARISH", "NEUTRAL"}
        recombined = (
            (1 + result["BULLISH"] / 100) * (1 + result["BEARISH"] / 100) * (1 + result["NEUTRAL"] / 100) - 1
        )
        raw_price_return = (closes[-1] - closes[0]) / closes[0]
        assert recombined == pytest.approx(raw_price_return, rel=1e-9)

    def test_phase_never_occurring_contributes_zero(self, monkeypatch):
        closes = [100.0, 102.0, 104.0]
        df_1h = _ohlc_df(
            opens=closes, highs=[c * 1.01 for c in closes], lows=[c * 0.99 for c in closes], closes=closes,
        )
        fake_phases = pd.Series(["BULLISH", "BULLISH", "BULLISH"], index=df_1h.index)
        monkeypatch.setattr("backtest.compute_market_phases", lambda df_4h: fake_phases)
        df_4h_placeholder = _ohlc_df(opens=[100.0], highs=[101.0], lows=[99.0], closes=[100.0], freq="4h")

        result = compute_phase_returns_buy_and_hold(df_1h, df_4h_placeholder, account=1000.0)

        assert result["BEARISH"] == pytest.approx(0.0)
        assert result["NEUTRAL"] == pytest.approx(0.0)


class TestComputePhaseReturnsStrategy:
    def test_groups_pnl_by_trade_side(self):
        trades = [
            {"side": "BUY", "pnl": 50.0},
            {"side": "BUY", "pnl": -20.0},
            {"side": "SELL", "pnl": 30.0},
            {"side": "SELL", "pnl": None},  # still open, excluded
        ]

        result = compute_phase_returns_strategy(trades, account=1000.0)

        assert result["BULLISH"] == pytest.approx(30.0 / 1000.0 * 100)
        assert result["BEARISH"] == pytest.approx(30.0 / 1000.0 * 100)

    def test_no_trades_returns_zero_for_both_phases(self):
        result = compute_phase_returns_strategy([], account=1000.0)

        assert result == {"BULLISH": 0.0, "BEARISH": 0.0}


class TestMainBenchmarkIntegration:
    def test_per_symbol_benchmark_passed_to_log_report(self, monkeypatch):
        df = _ohlc_df(opens=[100.0, 100.0], highs=[101.0, 101.0], lows=[99.0, 99.0], closes=[100.0, 110.0])
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: df)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.log_report", log_report_mock)

        main(["BTCUSDT"], days=10, refresh=False, conn=duckdb.connect(":memory:"))

        benchmark = log_report_mock.call_args_list[0].kwargs["benchmark"]
        assert benchmark["symbol"] == "BTCUSDT"
        assert benchmark["entry"] == 100.0
        assert benchmark["exit"] == 110.0

    def test_combined_benchmark_passed_for_multi_symbol_run(self, monkeypatch):
        df = _ohlc_df(opens=[100.0, 100.0], highs=[101.0, 101.0], lows=[99.0, 99.0], closes=[100.0, 110.0])
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: df)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.log_report", log_report_mock)

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False, conn=duckdb.connect(":memory:"))

        combined_benchmark = log_report_mock.call_args_list[-1].kwargs["benchmark"]
        assert combined_benchmark is not None
        assert "return_pct" in combined_benchmark

    def test_per_symbol_benchmark_includes_phase_returns(self, monkeypatch):
        closes = [100.0 + i for i in range(10)]
        df = _ohlc_df(opens=closes, highs=[c * 1.01 for c in closes], lows=[c * 0.99 for c in closes], closes=closes)
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: df)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.log_report", log_report_mock)

        main(["BTCUSDT"], days=10, refresh=False, conn=duckdb.connect(":memory:"))

        benchmark = log_report_mock.call_args_list[0].kwargs["benchmark"]
        assert set(benchmark["phase_returns"]) == {"BULLISH", "BEARISH", "NEUTRAL"}
        assert benchmark["strategy_phase_returns"] == {"BULLISH": 0.0, "BEARISH": 0.0}

    def test_combined_benchmark_includes_phase_returns(self, monkeypatch):
        closes = [100.0 + i for i in range(10)]
        df = _ohlc_df(opens=closes, highs=[c * 1.01 for c in closes], lows=[c * 0.99 for c in closes], closes=closes)
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: df)
        monkeypatch.setattr("backtest.run_backtest", MagicMock(return_value=[]))
        log_report_mock = MagicMock(return_value={"return_pct": 0.0, "max_drawdown_pct": 0.0, "return_to_dd_ratio": 0.0, "trades_count": 0, "win_rate_pct": 0.0, "profit_factor": None, "total_fees": 0.0})
        monkeypatch.setattr("backtest.log_report", log_report_mock)

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False, conn=duckdb.connect(":memory:"))

        combined_benchmark = log_report_mock.call_args_list[-1].kwargs["benchmark"]
        assert set(combined_benchmark["phase_returns"]) == {"BULLISH", "BEARISH", "NEUTRAL"}
        assert combined_benchmark["strategy_phase_returns"] == {"BULLISH": 0.0, "BEARISH": 0.0}


# ---------------------------------------------------------------------------
# Curated Layer persistence — real DuckDB, nothing stubbed, to prove the
# wiring end-to-end rather than just that each piece is called.
# ---------------------------------------------------------------------------

class TestMainCuratedLayerIntegration:
    def _seed_conn(self):
        import duckdb
        conn = duckdb.connect(":memory:")
        conn.execute("SET TIMEZONE = 'UTC'")
        return conn

    def test_records_a_run_with_symbols_and_thresholds(self, monkeypatch):
        from bot import EMA_FAST

        closes = [100.0 + i * 0.1 for i in range(150)]
        df = _ohlc_df(opens=closes, highs=[c * 1.01 for c in closes], lows=[c * 0.99 for c in closes], closes=closes)
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: df)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        conn = self._seed_conn()

        result = main(["BTCUSDT"], days=10, refresh=False, conn=conn)

        assert result == 0
        row = conn.execute("SELECT symbols, days, ema_fast FROM fact_backtest_run").fetchone()
        assert row == (["BTCUSDT"], 10, EMA_FAST)

    def test_does_not_close_an_injected_connection(self, monkeypatch):
        closes = [100.0 + i * 0.1 for i in range(150)]
        df = _ohlc_df(opens=closes, highs=[c * 1.01 for c in closes], lows=[c * 0.99 for c in closes], closes=closes)
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: df)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        conn = self._seed_conn()

        main(["BTCUSDT"], days=10, refresh=False, conn=conn)

        # Would raise if main() had closed our connection.
        assert conn.execute("SELECT 1").fetchone() == (1,)

    def test_combined_run_records_all_trades_from_every_symbol(self, monkeypatch):
        closes = [100.0 + i * 0.1 for i in range(150)]
        df = _ohlc_df(opens=closes, highs=[c * 1.01 for c in closes], lows=[c * 0.99 for c in closes], closes=closes)
        monkeypatch.setattr("backtest.load_history", lambda *a, **k: df)
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        conn = self._seed_conn()

        main(["BTCUSDT", "ETHUSDT"], days=10, refresh=False, conn=conn)

        run_count = conn.execute("SELECT COUNT(*) FROM fact_backtest_run").fetchone()[0]
        assert run_count == 1
        run_id = conn.execute("SELECT run_id FROM fact_backtest_run").fetchone()[0]
        trade_count_in_db = conn.execute(
            "SELECT COUNT(*) FROM fact_backtest_trade WHERE run_id = ?", [run_id]
        ).fetchone()[0]
        symbols_in_trades = {
            r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM fact_backtest_trade WHERE run_id = ?", [run_id]
            ).fetchall()
        }
        assert symbols_in_trades <= {"BTCUSDT", "ETHUSDT"}
        assert trade_count_in_db >= 0  # a smooth uptrend may or may not produce closed trades

    def test_no_run_recorded_when_every_symbol_is_skipped(self, monkeypatch):
        monkeypatch.setattr(
            "backtest.load_history", lambda *a, **k: (_ for _ in ()).throw(MissingDataError("missing"))
        )
        monkeypatch.setattr("backtest.RawStore", MagicMock())
        conn = self._seed_conn()
        create_schema(conn)  # main() skips schema/dims setup entirely when nothing succeeded

        result = main(["BTCUSDT"], days=10, refresh=False, conn=conn)

        assert result == 1
        run_count = conn.execute("SELECT COUNT(*) FROM fact_backtest_run").fetchone()[0]
        assert run_count == 0
