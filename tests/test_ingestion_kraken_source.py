from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

from ingestion.base import OHLCV_COLUMNS
from ingestion.kraken_source import KrakenAPIError, KrakenSource


def _ohlc_row(time_s: int, price: float = 100.0, volume: float = 10.0) -> list:
    """One raw Kraken OHLC row, matching the real API's field order."""
    return [
        time_s,
        str(price),          # open
        str(price * 1.01),   # high
        str(price * 0.99),   # low
        str(price + 1),      # close
        str(price),          # vwap
        str(volume),         # volume
        1,                   # count
    ]


def _response(result: dict, error: list | None = None) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"error": error or [], "result": result}
    return resp


def _make_source(session, **kwargs) -> KrakenSource:
    return KrakenSource(session=session, backoff_base=0.0, **kwargs)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_returns_expected_columns_and_order(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_s = int(start.timestamp())
        session = MagicMock()
        session.get.return_value = _response({"XXBTZUSD": [_ohlc_row(start_s)], "last": start_s})

        df = _make_source(session).fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert list(df.columns) == OHLCV_COLUMNS

    def test_timestamp_is_utc_tz_aware(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_s = int(start.timestamp())
        session = MagicMock()
        session.get.return_value = _response({"XXBTZUSD": [_ohlc_row(start_s)], "last": start_s})

        df = _make_source(session).fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert str(df["timestamp"].dt.tz) == "UTC"
        assert df["timestamp"].iloc[0] == pd.Timestamp(start)

    def test_numeric_columns_are_float(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_s = int(start.timestamp())
        session = MagicMock()
        session.get.return_value = _response({"XXBTZUSD": [_ohlc_row(start_s)], "last": start_s})

        df = _make_source(session).fetch_ohlcv("BTCUSDT", "1h", start, start)

        for col in ["open", "high", "low", "close", "volume"]:
            assert df[col].dtype == float

    def test_source_symbol_interval_populated(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_s = int(start.timestamp())
        session = MagicMock()
        session.get.return_value = _response({"XETHZUSD": [_ohlc_row(start_s)], "last": start_s})

        df = _make_source(session).fetch_ohlcv("ETHUSDT", "4h", start, start)

        assert df["source"].iloc[0] == "kraken"
        assert df["symbol"].iloc[0] == "ETHUSDT"
        assert df["interval"].iloc[0] == "4h"

    def test_empty_result_returns_empty_frame_with_schema(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)
        session = MagicMock()
        session.get.return_value = _response({"XXBTZUSD": [], "last": int(start.timestamp())})

        df = _make_source(session).fetch_ohlcv("BTCUSDT", "1h", start, end)

        assert df.empty
        assert list(df.columns) == OHLCV_COLUMNS

    def test_unsupported_interval_raises(self):
        session = MagicMock()
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            _make_source(session).fetch_ohlcv("BTCUSDT", "1m", start, start)

    def test_unsupported_symbol_raises(self):
        session = MagicMock()
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            _make_source(session).fetch_ohlcv("DOGEUSDT", "1h", start, start)

    def test_start_after_end_raises(self):
        session = MagicMock()
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(ValueError):
            _make_source(session).fetch_ohlcv("BTCUSDT", "1h", start, end)

    def test_response_key_mismatch_is_handled(self):
        # Kraken returns "XXBTZUSD" for a query of "XBTUSD" — the source
        # must not assume the response key matches the query pair string.
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_s = int(start.timestamp())
        session = MagicMock()
        session.get.return_value = _response({"XXBTZUSD": [_ohlc_row(start_s)], "last": start_s})

        df = _make_source(session).fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert len(df) == 1


# ---------------------------------------------------------------------------
# Range filtering — Kraken always returns its fixed ~720-candle window
# ending "now" regardless of `since`, so the source must trim client-side.
# ---------------------------------------------------------------------------

class TestRangeFiltering:
    def test_trims_to_requested_range_even_when_server_returns_more(self):
        start = datetime(2024, 1, 2, tzinfo=timezone.utc)
        end = datetime(2024, 1, 3, tzinfo=timezone.utc)
        rows = [
            _ohlc_row(int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())),      # before start
            _ohlc_row(int(datetime(2024, 1, 2, 12, tzinfo=timezone.utc).timestamp())),  # in range
            _ohlc_row(int(datetime(2024, 1, 4, tzinfo=timezone.utc).timestamp())),      # after end
        ]
        session = MagicMock()
        session.get.return_value = _response({"XXBTZUSD": rows, "last": 0})

        df = _make_source(session).fetch_ohlcv("BTCUSDT", "1h", start, end)

        assert len(df) == 1
        assert df["timestamp"].iloc[0] == pd.Timestamp(datetime(2024, 1, 2, 12, tzinfo=timezone.utc))

    def test_deduplicates_on_timestamp(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_s = int(start.timestamp())
        session = MagicMock()
        session.get.return_value = _response(
            {"XXBTZUSD": [_ohlc_row(start_s, price=100.0), _ohlc_row(start_s, price=200.0)], "last": start_s}
        )

        df = _make_source(session).fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert len(df) == 1
        assert df["open"].iloc[0] == 200.0  # keep="last"


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------

class TestRetry:
    def test_retries_on_rate_limit_then_succeeds(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr("ingestion.kraken_source.time.sleep", lambda s: sleeps.append(s))

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        start_s = int(start.timestamp())
        session = MagicMock()
        session.get.side_effect = [
            _response({}, error=["EAPI:Rate limit exceeded"]),
            _response({}, error=["EGeneral:Too many requests"]),
            _response({"XXBTZUSD": [_ohlc_row(start_s)], "last": start_s}),
        ]

        source = KrakenSource(session=session, backoff_base=1.0)
        df = source.fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert len(df) == 1
        assert session.get.call_count == 3
        assert sleeps == [1.0, 2.0]

    def test_gives_up_after_max_retries(self, monkeypatch):
        monkeypatch.setattr("ingestion.kraken_source.time.sleep", lambda s: None)
        session = MagicMock()
        session.get.return_value = _response({}, error=["EAPI:Rate limit exceeded"])

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        source = _make_source(session, max_retries=2)

        with pytest.raises(KrakenAPIError):
            source.fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert session.get.call_count == 3  # initial attempt + 2 retries

    def test_non_rate_limit_error_raises_immediately(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr("ingestion.kraken_source.time.sleep", lambda s: sleeps.append(s))
        session = MagicMock()
        session.get.return_value = _response({}, error=["EQuery:Unknown asset pair"])

        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        source = _make_source(session)

        with pytest.raises(KrakenAPIError):
            source.fetch_ohlcv("BTCUSDT", "1h", start, start)

        assert session.get.call_count == 1
        assert sleeps == []
