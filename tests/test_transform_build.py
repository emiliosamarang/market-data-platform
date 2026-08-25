from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from transform.build import _default_start, build, build_parser


class TestArgParsing:
    def test_start_and_end_default_to_none(self):
        args = build_parser().parse_args([])
        assert args.start is None
        assert args.end is None

    def test_start_parses_date_only_as_utc(self):
        args = build_parser().parse_args(["--start", "2024-01-01"])
        assert args.start == datetime(2024, 1, 1, tzinfo=timezone.utc)

    def test_start_parses_full_datetime(self):
        args = build_parser().parse_args(["--start", "2024-01-01T12:30:00"])
        assert args.start == datetime(2024, 1, 1, 12, 30, tzinfo=timezone.utc)

    def test_invalid_date_raises_system_exit(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--start", "not-a-date"])


class TestDefaultStart:
    def test_default_start_is_roughly_730_days_ago(self):
        now = datetime.now(timezone.utc)
        start = _default_start()
        delta = now - start
        assert timedelta(days=729, hours=23) < delta < timedelta(days=730, hours=1)


class TestBuild:
    def test_calls_steps_in_order(self, monkeypatch):
        calls = []
        monkeypatch.setattr("transform.build.create_schema", lambda conn: calls.append("schema"))
        monkeypatch.setattr("transform.build.populate_all_dims", lambda conn: calls.append("dims"))
        monkeypatch.setattr(
            "transform.build.load_fact_ohlcv",
            lambda *a, **k: calls.append("fact_ohlcv") or 0,
        )

        build(
            MagicMock(), MagicMock(), ["BTCUSDT"], ["1h"], ["binance"],
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc),
        )

        assert calls == ["schema", "dims", "fact_ohlcv"]

    def test_returns_row_count_from_fact_ohlcv_load(self, monkeypatch):
        monkeypatch.setattr("transform.build.create_schema", lambda conn: None)
        monkeypatch.setattr("transform.build.populate_all_dims", lambda conn: None)
        monkeypatch.setattr("transform.build.load_fact_ohlcv", lambda *a, **k: 42)

        result = build(
            MagicMock(), MagicMock(), ["BTCUSDT"], ["1h"], ["binance"],
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc),
        )

        assert result == 42

    def test_passes_requested_symbols_intervals_sources_through(self, monkeypatch):
        received = {}
        monkeypatch.setattr("transform.build.create_schema", lambda conn: None)
        monkeypatch.setattr("transform.build.populate_all_dims", lambda conn: None)

        def fake_load(conn, store, symbols, intervals, sources, start, end):
            received.update(symbols=symbols, intervals=intervals, sources=sources)
            return 0

        monkeypatch.setattr("transform.build.load_fact_ohlcv", fake_load)

        build(
            MagicMock(), MagicMock(), ["BTCUSDT", "ETHUSDT"], ["1h", "4h"], ["binance", "kraken"],
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc),
        )

        assert received == {
            "symbols": ["BTCUSDT", "ETHUSDT"],
            "intervals": ["1h", "4h"],
            "sources": ["binance", "kraken"],
        }
