import logging
import time
from datetime import datetime

import pandas as pd
import requests

from ingestion.base import MarketDataSource, OHLCV_COLUMNS
from ingestion.time_utils import to_ms

logger = logging.getLogger("ingestion.kraken")

_OHLC_URL = "https://api.kraken.com/0/public/OHLC"

# Kraken's public OHLC endpoint returns at most the ~720 most recent candles
# no matter what `since` is set to — confirmed against the live API: a
# `since` far enough in the past to request 1000+ candles still comes back
# with the same window ending "now" (~30 days at 1h, ~120 days at 4h). There
# is no way to page further into the past through this endpoint, so a
# single request always suffices — nothing like BinanceSource's pagination
# loop is possible or needed here.
_MAX_CANDLES = 720

# Our symbols -> Kraken's query pair string. Kraken has no single naming
# convention: BTC is "XBT", and legacy-listed pairs (BTC/ETH/XRP) come back
# from the OHLC endpoint under a different, X/Z-prefixed key than the one
# you queried with (e.g. query "XBTUSD", response key "XXBTZUSD"), while
# newer listings (SOL/ADA) use the same string both ways. This map only
# needs to cover the query side — the response-key quirk is sidestepped by
# reading whichever single non-"last" key comes back (see _to_dataframe).
_SYMBOL_MAP = {
    "BTCUSDT": "XBTUSD",
    "ETHUSDT": "ETHUSD",
    "SOLUSDT": "SOLUSD",
    "XRPUSDT": "XRPUSD",
    "ADAUSDT": "ADAUSD",
}

# Kraken's interval parameter is candle length in minutes, not the
# exchange-style strings ("1h"/"4h") used elsewhere in this codebase.
_INTERVAL_MINUTES = {"1h": 60, "4h": 240}

_RAW_OHLC_COLS = ["time", "open", "high", "low", "close", "vwap", "volume", "count"]

_RATE_LIMIT_MARKERS = ("rate limit", "too many requests")


class KrakenAPIError(Exception):
    """Raised when Kraken's OHLC endpoint returns a non-empty `error` list."""


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV_COLUMNS)


class KrakenSource(MarketDataSource):
    """MarketDataSource backed by Kraken's public OHLC REST endpoint.

    No API key required for this endpoint. Unlike BinanceSource, this
    cannot backfill deep history — see _MAX_CANDLES above. A request for an
    earlier `start` than Kraken can actually serve doesn't error; it just
    returns whatever's available, filtered to the requested range. Callers
    that need a specific historical depth should check the returned range
    rather than assume it matches what was asked for.
    """

    name = "kraken"

    def __init__(
        self,
        session: requests.Session | None = None,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        timeout: float = 10.0,
    ):
        self.session = session if session is not None else requests.Session()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout

    def fetch_ohlcv(
        self, symbol: str, interval: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        start_ms, end_ms = to_ms(start), to_ms(end)
        if start_ms > end_ms:
            raise ValueError(f"start ({start}) is after end ({end})")

        pair = self._kraken_pair(symbol)
        minutes = self._kraken_interval(interval)

        result = self._fetch_with_retry(pair, minutes, start_ms // 1000)
        data_keys = [k for k in result if k != "last"]
        rows = result[data_keys[0]] if data_keys else []

        if not rows:
            return _empty_df()

        return self._to_dataframe(rows, symbol, interval, start_ms, end_ms)

    def _kraken_pair(self, symbol: str) -> str:
        try:
            return _SYMBOL_MAP[symbol]
        except KeyError:
            raise ValueError(f"No Kraken pair mapping for symbol: {symbol!r}") from None

    def _kraken_interval(self, interval: str) -> int:
        try:
            return _INTERVAL_MINUTES[interval]
        except KeyError:
            raise ValueError(f"Unsupported interval for KrakenSource: {interval!r}") from None

    def _fetch_with_retry(self, pair: str, interval_minutes: int, since_s: int) -> dict:
        attempt = 0
        while True:
            response = self.session.get(
                _OHLC_URL,
                params={"pair": pair, "interval": interval_minutes, "since": since_s},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            errors = payload.get("error") or []

            if not errors:
                return payload["result"]

            attempt += 1
            if not self._is_rate_limit(errors) or attempt > self.max_retries:
                logger.error("Kraken OHLC error for %s: %s", pair, errors)
                raise KrakenAPIError(", ".join(errors))

            delay = self.backoff_base * (2 ** (attempt - 1))
            logger.warning(
                "Rate limited fetching %s (attempt %d/%d), backing off %.1fs",
                pair, attempt, self.max_retries, delay,
            )
            time.sleep(delay)

    @staticmethod
    def _is_rate_limit(errors: list) -> bool:
        text = " ".join(errors).lower()
        return any(marker in text for marker in _RATE_LIMIT_MARKERS)

    def _to_dataframe(
        self, rows: list, symbol: str, interval: str, start_ms: int, end_ms: int
    ) -> pd.DataFrame:
        df = pd.DataFrame(rows, columns=_RAW_OHLC_COLS)
        df = df[["time", "open", "high", "low", "close", "volume"]].copy()
        df[["open", "high", "low", "close", "volume"]] = df[
            ["open", "high", "low", "close", "volume"]
        ].astype(float)
        df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df["source"] = self.name
        df["symbol"] = symbol
        df["interval"] = interval
        df = df.drop(columns="time")

        # Kraken always returns its fixed ~720-candle window ending "now"
        # regardless of `since` — trim to what was actually requested.
        start_ts = pd.Timestamp(start_ms, unit="ms", tz="UTC")
        end_ts = pd.Timestamp(end_ms, unit="ms", tz="UTC")
        df = df[(df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)]

        df = df.drop_duplicates(subset=["timestamp"], keep="last")
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df[OHLCV_COLUMNS]
