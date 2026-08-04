from abc import ABC, abstractmethod
from datetime import datetime

import pandas as pd

# Canonical column order for every DataFrame returned by a MarketDataSource
# and written by RawStore.
OHLCV_COLUMNS = [
    "timestamp", "open", "high", "low", "close", "volume",
    "source", "symbol", "interval",
]


class MarketDataSource(ABC):
    """Common interface for market data providers.

    Implementations fetch OHLCV candles for a symbol/interval/time range and
    return them in the schema defined by OHLCV_COLUMNS: timestamp (UTC,
    tz-aware), open, high, low, close, volume, source, symbol, interval.
    """

    name: str

    @abstractmethod
    def fetch_ohlcv(
        self, symbol: str, interval: str, start: datetime, end: datetime
    ) -> pd.DataFrame:
        """Fetch OHLCV candles for [start, end] (inclusive), oldest first."""
        raise NotImplementedError
