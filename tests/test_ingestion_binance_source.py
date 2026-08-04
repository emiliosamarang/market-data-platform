from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest
from binance.exceptions import BinanceAPIException, BinanceRequestException

from ingestion.base import OHLCV_COLUMNS
from ingestion.binance_source import BinanceSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INTERVAL_MS_1H = 3_600_000


def _kline(open_time_ms: int, price: float = 100.0, volume: float = 10.0) -> list:
    """One raw Binance kline row, matching the real API's field order."""
    return [
        open_time_ms,
        str(price),                    # open
        str(price * 1.01),             # high
        str(price * 0.99),             # low
        str(price + 1),                # close
        str(volume),                   # volume
        open_time_ms + _INTERVAL_MS_1H - 1,  # close_time
        "0",                           # quote_volume
        1,                             # trades
        "0",                           # taker_buy_base
        "0",                           # taker_buy_quote
        "0",                           # ignore
    ]


def _make_source(client, **kwargs) -> BinanceSource:
    return BinanceSource(client=client, backoff_base=0.0, **kwargs)


def _rate_limit_exception(status_code: int = 429, code: int = -1003) -> BinanceAPIException:
    response = MagicMock()
    text = f'{{"code": {code}, "msg": "Too many requests"}}'
    response.text = text
    return BinanceAPIException(response, status_code, text)


def _generic_api_exception(code: int = -1121, msg: str = "Invalid symbol") -> BinanceAPIException:
    response = MagicMock()
    text = f'{{"code": {code}, "msg": "{msg}"}}'
    response.text = text
    return BinanceAPIException(response, 400, text)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_returns_expected_columns_and_order(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)
        client = MagicMock()
        client.get_klines.return_value = [_kline(start_ms)]

        source = _make_source(client)
        df = source.fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert list(df.columns) == OHLCV_COLUMNS

    def test_timestamp_is_utc_tz_aware(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)
        client = MagicMock()
        client.get_klines.return_value = [_kline(start_ms)]

        df = _make_source(client).fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert str(df["timestamp"].dt.tz) == "UTC"
        assert df["timestamp"].iloc[0] == pd.Timestamp(start)

    def test_numeric_columns_are_float(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)
        client = MagicMock()
        client.get_klines.return_value = [_kline(start_ms)]

        df = _make_source(client).fetch_ohlcv("BTCUSDT", "1h", start, start)

        for col in ["open", "high", "low", "close", "volume"]:
            assert df[col].dtype == float

    def test_source_symbol_interval_populated(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)
        client = MagicMock()
        client.get_klines.return_value = [_kline(start_ms)]

        df = _make_source(client).fetch_ohlcv("ETHUSDT", "4h", start, start)

        assert df["source"].iloc[0] == "binance"
        assert df["symbol"].iloc[0] == "ETHUSDT"
        assert df["interval"].iloc[0] == "4h"

    def test_empty_response_returns_empty_frame_with_schema(self):
        client = MagicMock()
        client.get_klines.return_value = []
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)

        df = _make_source(client).fetch_ohlcv("BTCUSDT", "1h", start, end)

        assert df.empty
        assert list(df.columns) == OHLCV_COLUMNS

    def test_unsupported_interval_raises(self):
        client = MagicMock()
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            _make_source(client).fetch_ohlcv("BTCUSDT", "1M", start, start)

    def test_start_after_end_raises(self):
        client = MagicMock()
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            _make_source(client).fetch_ohlcv("BTCUSDT", "1h", start, end)


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

class TestPagination:
    def test_paginates_across_multiple_pages(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)
        page_limit = 3
        total_candles = 7

        all_candles = [
            _kline(start_ms + i * _INTERVAL_MS_1H) for i in range(total_candles)
        ]
        pages = [
            all_candles[i : i + page_limit] for i in range(0, total_candles, page_limit)
        ]

        client = MagicMock()
        client.get_klines.side_effect = pages

        end = start.replace(hour=0) + pd.Timedelta(hours=total_candles - 1)
        source = _make_source(client, page_limit=page_limit)
        df = source.fetch_ohlcv("BTCUSDT", "1h", start, end)

        assert len(df) == total_candles
        assert client.get_klines.call_count == len(pages)
        assert df["timestamp"].is_monotonic_increasing

    def test_no_duplicate_rows_on_overlapping_pages(self):
        # Simulates the API returning the boundary candle again in the next page.
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)

        page1 = [_kline(start_ms + i * _INTERVAL_MS_1H) for i in range(3)]  # t0,t1,t2
        page2 = [_kline(start_ms + i * _INTERVAL_MS_1H) for i in range(2, 4)]  # t2,t3 (overlap)

        client = MagicMock()
        client.get_klines.side_effect = [page1, page2, []]

        end = start + pd.Timedelta(hours=3)
        source = _make_source(client, page_limit=3)
        df = source.fetch_ohlcv("BTCUSDT", "1h", start, end)

        assert df["timestamp"].is_unique
        assert len(df) == 4

    def test_short_page_stops_pagination(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)
        client = MagicMock()
        # Fewer rows than page_limit -> no further pages requested.
        client.get_klines.return_value = [_kline(start_ms)]

        end = start + pd.Timedelta(days=30)
        source = _make_source(client, page_limit=1000)
        source.fetch_ohlcv("BTCUSDT", "1h", start, end)

        assert client.get_klines.call_count == 1


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------

class TestRetry:
    def test_retries_on_rate_limit_then_succeeds(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr("ingestion.binance_source.time.sleep", lambda s: sleeps.append(s))

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)
        client = MagicMock()
        client.get_klines.side_effect = [
            _rate_limit_exception(),
            _rate_limit_exception(),
            [_kline(start_ms)],
        ]

        source = BinanceSource(client=client, backoff_base=1.0)
        df = source.fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert len(df) == 1
        assert client.get_klines.call_count == 3
        # exponential backoff: 1.0, 2.0
        assert sleeps == [1.0, 2.0]

    def test_retries_on_418_ip_ban(self, monkeypatch):
        monkeypatch.setattr("ingestion.binance_source.time.sleep", lambda s: None)
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_ms = int(start.timestamp() * 1000)
        client = MagicMock()
        client.get_klines.side_effect = [
            _rate_limit_exception(status_code=418, code=-1003),
            [_kline(start_ms)],
        ]

        df = _make_source(client).fetch_ohlcv("BTCUSDT", "1h", start, start)
        assert len(df) == 1

    def test_gives_up_after_max_retries(self, monkeypatch):
        monkeypatch.setattr("ingestion.binance_source.time.sleep", lambda s: None)
        client = MagicMock()
        client.get_klines.side_effect = _rate_limit_exception()

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        source = _make_source(client, max_retries=2)

        with pytest.raises(BinanceAPIException):
            source.fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert client.get_klines.call_count == 3  # initial attempt + 2 retries

    def test_non_rate_limit_api_error_raises_immediately(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr("ingestion.binance_source.time.sleep", lambda s: sleeps.append(s))
        client = MagicMock()
        client.get_klines.side_effect = _generic_api_exception()

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        source = _make_source(client)

        with pytest.raises(BinanceAPIException):
            source.fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert client.get_klines.call_count == 1
        assert sleeps == []

    def test_request_exception_raises_immediately(self, monkeypatch):
        # BinanceRequestException (network/JSON errors) has no status_code or
        # error code, so it can never look like a rate-limit response and
        # must not be retried.
        sleeps = []
        monkeypatch.setattr("ingestion.binance_source.time.sleep", lambda s: sleeps.append(s))
        client = MagicMock()
        client.get_klines.side_effect = BinanceRequestException("connection reset")

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        source = _make_source(client)

        with pytest.raises(BinanceRequestException):
            source.fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert client.get_klines.call_count == 1
        assert sleeps == []
