import logging
from datetime import datetime

import duckdb
import pandas as pd

from bot import add_indicators

log = logging.getLogger("transform.fact_indicator")

_INDICATOR_COLUMNS = [
    "symbol", "interval", "timestamp",
    "ema_20", "ema_50", "rsi", "macd", "macd_signal", "macd_hist", "atr", "volume_ma",
]

# Columns whose warmup window must have passed before a row is meaningful.
# EMA never produces NaN with .ewm(adjust=False) (it "starts" immediately,
# just hasn't converged) — RSI/ATR/Volume_MA are the ones that actually
# gate on enough history.
_WARMUP_GATE_COLUMNS = ["rsi", "atr", "volume_ma"]


def _read_canonical_ohlc(
    conn: duckdb.DuckDBPyConnection, symbol: str, interval: str, start: datetime, end: datetime
) -> pd.DataFrame:
    """Read canonical OHLCV rows from DuckDB and reshape to bot.py's
    expected Open/High/Low/Close/Volume + DatetimeIndex shape."""
    df = conn.execute(
        "SELECT timestamp, open, high, low, close, volume FROM fact_ohlcv_canonical "
        "WHERE symbol = ? AND interval = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
        [symbol, interval, start, end],
    ).fetch_df()
    df = df.rename(
        columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
    )
    return df.set_index("timestamp")


def load_fact_indicator(
    conn: duckdb.DuckDBPyConnection,
    symbols: list[str],
    intervals: list[str],
    start: datetime,
    end: datetime,
) -> int:
    """Compute and load indicators for every (symbol, interval) combination,
    reading canonical OHLCV from fact_ohlcv_canonical and reusing bot.py's
    own add_indicators — no second indicator implementation. Idempotent:
    INSERT OR REPLACE keyed on (symbol, interval, timestamp).
    """
    total = 0
    for symbol in symbols:
        for interval in intervals:
            df = _read_canonical_ohlc(conn, symbol, interval, start, end)
            if df.empty:
                continue

            df = add_indicators(df)
            out = df.reset_index()[
                ["timestamp", "EMA_20", "EMA_50", "RSI", "MACD", "MACD_SIGNAL", "MACD_HIST", "ATR", "Volume_MA"]
            ].copy()
            out.columns = [
                "timestamp", "ema_20", "ema_50", "rsi", "macd", "macd_signal", "macd_hist", "atr", "volume_ma",
            ]
            out.insert(0, "interval", interval)
            out.insert(0, "symbol", symbol)
            out = out.dropna(subset=_WARMUP_GATE_COLUMNS)

            if out.empty:
                continue

            conn.register("_fact_indicator_batch", out)
            conn.execute(
                f"INSERT OR REPLACE INTO fact_indicator ({', '.join(_INDICATOR_COLUMNS)}) "
                f"SELECT {', '.join(_INDICATOR_COLUMNS)} FROM _fact_indicator_batch"
            )
            conn.unregister("_fact_indicator_batch")

            log.info("%s %s | loaded %d rows into fact_indicator", symbol, interval, len(out))
            total += len(out)

    return total
