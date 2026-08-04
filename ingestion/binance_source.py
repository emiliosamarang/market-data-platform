import logging
import time
from datetime import datetime, timezone

import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException

from config import API_KEY, API_SECRET
from ingestion.base import MarketDataSource, OHLCV_COLUMNS

logger = logging.getLogger("ingestion.binance")

# Binance signals "back off" via these HTTP status codes (429 = rate limit
# exceeded, 418 = IP auto-banned for repeated 429s) or this API error code.
_RATE_LIMIT_STATUS_CODES = {418, 429}
_RATE_LIMIT_ERROR_CODE = -1003

_RAW_KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]

# Fixed-duration intervals only — calendar-based ones (e.g. "1M") are
# excluded because their length in ms varies.
_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 3_600_000,
    "2h": 2 * 3_600_000,
    "4h": 4 * 3_600_000,
    "6h": 6 * 3_600_000,
    "8h": 8 * 3_600_000,
    "12h": 12 * 3_600_000,
    "1d": 86_400_000,
    "3d": 3 * 86_400_000,
    "1w": 7 * 86_400_000,
}


def _interval_to_ms(interval: str) -> int:
    try:
        return _INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(f"Unsupported interval for pagination: {interval!r}") from None


def _to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _is_rate_limit(exc: Exception) -> bool:
    if not isinstance(exc, BinanceAPIException):
        return False
    return (
        exc.status_code in _RATE_LIMIT_STATUS_CODES
        or exc.code == _RATE_LIMIT_ERROR_CODE
    )


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV_COLUMNS)


class BinanceSource(MarketDataSource):
    """MarketDataSource backed by the Binance REST API (via exchange.py's client shape)."""

    name = "binance"

    def __init__(
        self,
        client: Client | None = None,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        page_limit: int = 1000,
    ):
        self.client = client if client is not None else Client(API_KEY, API_SECRET)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.page_limit = page_limit

    def fetch_ohlcv(
        self, symbol: str, interval: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        interval_ms = _interval_to_ms(interval)
        start_ms = _to_ms(start)
        end_ms = _to_ms(end)

        if start_ms > end_ms:
            raise ValueError(f"start ({start}) is after end ({end})")

        pages: list[list] = []
        cursor = start_ms
        while cursor <= end_ms:
            raw = self._fetch_page_with_retry(symbol, interval, cursor, end_ms)
            if not raw:
                break

            pages.append(raw)
            last_open_time = raw[-1][0]
            next_cursor = last_open_time + interval_ms

            if next_cursor <= cursor:
                # Defensive: guarantees forward progress even on unexpected
                # API responses, so we never spin forever.
                break
            cursor = next_cursor

            if len(raw) < self.page_limit:
                break  # short page == last page

        if not pages:
            return _empty_df()

        rows = [row for page in pages for row in page]
        return self._to_dataframe(rows, symbol, interval)

    def _fetch_page_with_retry(
        self, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> list:
        attempt = 0
        while True:
            try:
                return self.client.get_klines(
                    symbol=symbol,
                    interval=interval,
                    startTime=start_ms,
                    endTime=end_ms,
                    limit=self.page_limit,
                )
            except (BinanceAPIException, BinanceRequestException) as exc:
                attempt += 1
                if not _is_rate_limit(exc) or attempt > self.max_retries:
                    logger.error(
                        "Failed fetching %s %s klines: %s", symbol, interval, exc
                    )
                    raise

                delay = self.backoff_base * (2 ** (attempt - 1))
                logger.warning(
                    "Rate limited fetching %s %s (attempt %d/%d), backing off %.1fs",
                    symbol, interval, attempt, self.max_retries, delay,
                )
                time.sleep(delay)

    def _to_dataframe(self, rows: list, symbol: str, interval: str) -> pd.DataFrame:
        df = pd.DataFrame(rows, columns=_RAW_KLINE_COLS)
        df = df[["open_time", "open", "high", "low", "close", "volume"]].copy()
        df[["open", "high", "low", "close", "volume"]] = df[
            ["open", "high", "low", "close", "volume"]
        ].astype(float)
        df["timestamp"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["source"] = self.name
        df["symbol"] = symbol
        df["interval"] = interval

        df = df.drop(columns="open_time")
        # Safety net: pagination boundaries can overlap by one candle.
        df = df.drop_duplicates(subset=["timestamp"], keep="last")
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df[OHLCV_COLUMNS]
