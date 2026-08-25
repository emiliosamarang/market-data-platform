from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from config import INGESTION_SYMBOLS
from ingestion.base import OHLCV_COLUMNS
from ingestion.binance_source import BinanceSource
from ingestion.kraken_source import KrakenSource
from ingestion.load import _default_start, build_parser, main, run


def _df(symbol="BTCUSDT", interval="1h", n=2, start="2024-01-01T00:00:00Z"):
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
# Argument parsing
# ---------------------------------------------------------------------------

class TestArgParsing:
    def test_default_symbols_from_config(self):
        args = build_parser().parse_args(["--interval", "1h"])
        assert args.symbols == INGESTION_SYMBOLS

    def test_multiple_symbols(self):
        args = build_parser().parse_args(
            ["--symbol", "BTCUSDT", "ETHUSDT", "--interval", "1h"]
        )
        assert args.symbols == ["BTCUSDT", "ETHUSDT"]

    def test_interval_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--symbol", "BTCUSDT"])

    def test_dry_run_defaults_to_false(self):
        args = build_parser().parse_args(["--interval", "1h"])
        assert args.dry_run is False

    def test_dry_run_flag_sets_true(self):
        args = build_parser().parse_args(["--interval", "1h", "--dry-run"])
        assert args.dry_run is True

    def test_start_and_end_default_to_none(self):
        args = build_parser().parse_args(["--interval", "1h"])
        assert args.start is None
        assert args.end is None

    def test_start_parses_date_only_as_utc(self):
        args = build_parser().parse_args(["--interval", "1h", "--start", "2024-01-01"])
        assert args.start == datetime(2024, 1, 1, tzinfo=timezone.utc)

    def test_start_parses_full_datetime(self):
        args = build_parser().parse_args(
            ["--interval", "1h", "--start", "2024-01-01T12:30:00"]
        )
        assert args.start == datetime(2024, 1, 1, 12, 30, tzinfo=timezone.utc)

    def test_invalid_date_raises_system_exit(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--interval", "1h", "--start", "not-a-date"])

    def test_source_defaults_to_binance(self):
        args = build_parser().parse_args(["--interval", "1h"])
        assert args.source == "binance"

    def test_source_can_be_set_to_kraken(self):
        args = build_parser().parse_args(["--interval", "1h", "--source", "kraken"])
        assert args.source == "kraken"

    def test_unknown_source_raises_system_exit(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--interval", "1h", "--source", "coinbase"])


# ---------------------------------------------------------------------------
# Default start
# ---------------------------------------------------------------------------

class TestDefaultStart:
    def test_default_start_is_roughly_30_days_ago(self):
        now = datetime.now(timezone.utc)
        start = _default_start()
        delta = now - start
        assert timedelta(days=29, hours=23) < delta < timedelta(days=30, hours=1)


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

class TestRun:
    def test_dry_run_does_not_write(self):
        source = MagicMock()
        source.fetch_ohlcv.return_value = _df()
        store = MagicMock()
        store.preview.return_value = [("data/raw/crypto/binance/BTCUSDT/1h/2024-01-01.parquet", 2)]

        run(["BTCUSDT"], "1h", None, None, dry_run=True, source=source, store=store)

        store.write.assert_not_called()
        store.preview.assert_called_once()

    def test_normal_run_writes_and_skips_preview(self):
        source = MagicMock()
        source.fetch_ohlcv.return_value = _df()
        store = MagicMock()

        run(["BTCUSDT"], "1h", None, None, dry_run=False, source=source, store=store)

        store.write.assert_called_once()
        store.preview.assert_not_called()

    def test_multiple_symbols_each_fetched_and_written(self):
        source = MagicMock()
        source.fetch_ohlcv.side_effect = [_df(symbol="BTCUSDT"), _df(symbol="ETHUSDT")]
        store = MagicMock()

        run(["BTCUSDT", "ETHUSDT"], "1h", None, None, dry_run=False, source=source, store=store)

        assert source.fetch_ohlcv.call_count == 2
        assert store.write.call_count == 2

    def test_empty_result_skips_write(self):
        source = MagicMock()
        source.fetch_ohlcv.return_value = _df(n=0)
        store = MagicMock()

        run(["BTCUSDT"], "1h", None, None, dry_run=False, source=source, store=store)

        store.write.assert_not_called()

    def test_empty_result_skips_preview_in_dry_run(self):
        source = MagicMock()
        source.fetch_ohlcv.return_value = _df(n=0)
        store = MagicMock()

        run(["BTCUSDT"], "1h", None, None, dry_run=True, source=source, store=store)

        store.preview.assert_not_called()

    def test_default_start_end_used_when_not_provided(self):
        source = MagicMock()
        source.fetch_ohlcv.return_value = _df()
        store = MagicMock()

        run(["BTCUSDT"], "1h", None, None, dry_run=False, source=source, store=store)

        called_symbol, called_interval, called_start, called_end = source.fetch_ohlcv.call_args[0]
        assert called_symbol == "BTCUSDT"
        assert called_interval == "1h"
        assert called_end - called_start > timedelta(days=29)

    def test_explicit_start_end_passed_through_unchanged(self):
        source = MagicMock()
        source.fetch_ohlcv.return_value = _df()
        store = MagicMock()
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)

        run(["BTCUSDT"], "1h", start, end, dry_run=False, source=source, store=store)

        source.fetch_ohlcv.assert_called_once_with("BTCUSDT", "1h", start, end)

    def test_default_source_and_store_constructed_when_omitted(self, monkeypatch):
        # run() should build a real BinanceSource/RawStore when none are
        # injected, rather than requiring callers to always pass mocks.
        fake_source_cls = MagicMock()
        fake_source_instance = fake_source_cls.return_value
        fake_source_instance.fetch_ohlcv.return_value = _df(n=0)
        fake_store_cls = MagicMock()

        monkeypatch.setattr("ingestion.load.BinanceSource", fake_source_cls)
        monkeypatch.setattr("ingestion.load.RawStore", fake_store_cls)

        run(["BTCUSDT"], "1h", None, None, dry_run=False)

        fake_source_cls.assert_called_once_with()
        fake_store_cls.assert_called_once()


# ---------------------------------------------------------------------------
# main() — source registry wiring
# ---------------------------------------------------------------------------

class TestMainSourceSelection:
    def test_default_source_is_binance(self, monkeypatch):
        run_mock = MagicMock()
        monkeypatch.setattr("ingestion.load.run", run_mock)
        monkeypatch.setattr("sys.argv", ["prog", "--interval", "1h"])

        main()

        used_source = run_mock.call_args.kwargs["source"]
        assert isinstance(used_source, BinanceSource)

    def test_source_flag_selects_kraken(self, monkeypatch):
        run_mock = MagicMock()
        monkeypatch.setattr("ingestion.load.run", run_mock)
        monkeypatch.setattr("sys.argv", ["prog", "--interval", "1h", "--source", "kraken"])

        main()

        used_source = run_mock.call_args.kwargs["source"]
        assert isinstance(used_source, KrakenSource)
