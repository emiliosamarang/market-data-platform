import logging
from unittest.mock import MagicMock

import pandas as pd
import pytest

import bot
from bot import DataUnavailable, _load_recent
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
# _load_recent — refresh-then-read for the live scan
# ---------------------------------------------------------------------------

class TestLoadRecent:
    def test_refreshes_then_reads_in_order(self, monkeypatch):
        source = MagicMock()
        store = MagicMock()
        source.fetch_ohlcv.return_value = _raw_df(n=5)
        store.read.return_value = _raw_df(n=5)
        monkeypatch.setattr(bot, "_source", source)
        monkeypatch.setattr(bot, "_store", store)

        result = _load_recent("BTCUSDT", "1h", limit=3)

        source.fetch_ohlcv.assert_called_once()
        store.write.assert_called_once()
        store.read.assert_called_once()
        assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert result.index.name == "timestamp"
        assert len(result) == 3  # tail(limit) applied

    def test_write_receives_the_freshly_fetched_data(self, monkeypatch):
        source = MagicMock()
        store = MagicMock()
        fresh = _raw_df(n=2)
        source.fetch_ohlcv.return_value = fresh
        store.read.return_value = _raw_df(n=5)
        monkeypatch.setattr(bot, "_source", source)
        monkeypatch.setattr(bot, "_store", store)

        _load_recent("BTCUSDT", "1h", limit=3)

        written_df = store.write.call_args[0][0]
        assert written_df is fresh

    def test_incremental_window_is_smaller_than_full_read_window(self, monkeypatch):
        source = MagicMock()
        store = MagicMock()
        source.fetch_ohlcv.return_value = _raw_df(n=2)
        store.read.return_value = _raw_df(n=5)
        monkeypatch.setattr(bot, "_source", source)
        monkeypatch.setattr(bot, "_store", store)

        _load_recent("BTCUSDT", "1h", limit=200)

        fetch_start = source.fetch_ohlcv.call_args[0][2]
        read_start = store.read.call_args[0][4]
        # The incremental refresh only reaches back a handful of candles;
        # the subsequent read spans the full `limit`-candle window, so it
        # must start noticeably earlier.
        assert read_start < fetch_start

    def test_incremental_fetch_failure_raises_data_unavailable(self, monkeypatch):
        source = MagicMock()
        store = MagicMock()
        source.fetch_ohlcv.side_effect = RuntimeError("network down")
        monkeypatch.setattr(bot, "_source", source)
        monkeypatch.setattr(bot, "_store", store)

        with pytest.raises(DataUnavailable, match="incremental reload failed"):
            _load_recent("BTCUSDT", "1h", limit=200)

        store.write.assert_not_called()
        store.read.assert_not_called()

    def test_read_failure_after_successful_refresh_raises_data_unavailable(self, monkeypatch):
        source = MagicMock()
        store = MagicMock()
        source.fetch_ohlcv.return_value = _raw_df(n=2)
        store.read.side_effect = MissingDataError("gap in raw layer")
        monkeypatch.setattr(bot, "_source", source)
        monkeypatch.setattr(bot, "_store", store)

        with pytest.raises(DataUnavailable, match="raw layer incomplete after reload"):
            _load_recent("BTCUSDT", "1h", limit=200)

        store.write.assert_called_once()  # the refresh itself did succeed

    def test_write_failure_also_raises_data_unavailable(self, monkeypatch):
        source = MagicMock()
        store = MagicMock()
        source.fetch_ohlcv.return_value = _raw_df(n=2)
        store.write.side_effect = OSError("disk full")
        monkeypatch.setattr(bot, "_source", source)
        monkeypatch.setattr(bot, "_store", store)

        with pytest.raises(DataUnavailable, match="incremental reload failed"):
            _load_recent("BTCUSDT", "1h", limit=200)

        store.read.assert_not_called()


# ---------------------------------------------------------------------------
# scan() — a symbol with unavailable data must be skipped, not traded stale
# ---------------------------------------------------------------------------

class TestScanSkipsOnDataUnavailable:
    def _flat_frame(self, n=60):
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        return pd.DataFrame(
            {
                "Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n,
                "Close": [100.0] * n, "Volume": [1000.0] * n,
            },
            index=idx,
        )

    def _wire_common_mocks(self, monkeypatch):
        monkeypatch.setattr(bot.trader, "sync_open_trades", MagicMock())
        monkeypatch.setattr(bot.trader, "open_trade", MagicMock())
        monkeypatch.setattr(bot.database, "get_daily_pnl", MagicMock(return_value=0.0))
        monkeypatch.setattr(bot.database, "log_signal", MagicMock())
        monkeypatch.setattr(bot.notify, "send", MagicMock())
        monkeypatch.setattr(bot.sentiment, "get_fear_greed", MagicMock(return_value=50))
        monkeypatch.setattr(bot.sentiment, "get_news_sentiment", MagicMock(return_value=0.0))

    def test_symbol_with_unavailable_data_is_skipped_and_logged(self, monkeypatch, caplog):
        def fake_load_recent(symbol, interval, limit):
            if symbol == "BTCUSDT":
                raise DataUnavailable("BTCUSDT 1h | incremental reload failed: boom")
            return self._flat_frame()

        self._wire_common_mocks(monkeypatch)
        monkeypatch.setattr(bot, "_load_recent", fake_load_recent)
        monkeypatch.setattr(bot, "SYMBOLS", ["BTCUSDT", "ETHUSDT"])

        with caplog.at_level(logging.WARNING, logger="bot"):
            bot.scan()

        assert "BTCUSDT" in caplog.text
        assert "skipped" in caplog.text
        bot.database.log_signal.assert_not_called()

    def test_other_symbols_still_processed_after_a_skip(self, monkeypatch):
        processed = []

        def fake_load_recent(symbol, interval, limit):
            processed.append(symbol)
            if symbol == "BTCUSDT":
                raise DataUnavailable("BTCUSDT 1h | incremental reload failed: boom")
            return self._flat_frame()

        self._wire_common_mocks(monkeypatch)
        monkeypatch.setattr(bot, "_load_recent", fake_load_recent)
        monkeypatch.setattr(bot, "SYMBOLS", ["BTCUSDT", "ETHUSDT"])

        bot.scan()

        # 2 calls per symbol (4h + 1h) for both symbols — the skip must not
        # abort the loop for the remaining symbols.
        assert processed.count("BTCUSDT") >= 1
        assert processed.count("ETHUSDT") == 2

    def test_scan_does_not_crash_when_all_symbols_unavailable(self, monkeypatch):
        def fake_load_recent(symbol, interval, limit):
            raise DataUnavailable(f"{symbol} {interval} | incremental reload failed: boom")

        self._wire_common_mocks(monkeypatch)
        monkeypatch.setattr(bot, "_load_recent", fake_load_recent)
        monkeypatch.setattr(bot, "SYMBOLS", ["BTCUSDT", "ETHUSDT"])

        bot.scan()  # must not raise

        bot.trader.open_trade.assert_not_called()
