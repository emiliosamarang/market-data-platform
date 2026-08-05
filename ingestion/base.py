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


# Maps the ingestion schema's lowercase OHLCV columns onto the capitalized
# names bot.py's indicator/strategy functions expect.
_OHLC_RENAME = {"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}


def to_ohlc_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Reshape an OHLCV_COLUMNS DataFrame into bot.py's expected shape:
    capitalized Open/High/Low/Close/Volume columns on a DatetimeIndex named
    "timestamp" — the shape exchange.get_data() historically produced.
    """
    result = df.set_index("timestamp")[list(_OHLC_RENAME)].rename(columns=_OHLC_RENAME)
    result.index.name = "timestamp"
    return result
