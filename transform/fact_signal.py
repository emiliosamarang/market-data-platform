import logging
from datetime import datetime

import duckdb
import pandas as pd

from backtest import compute_market_phases
from bot import add_indicators, calculate_score, generate_entry_signal

log = logging.getLogger("transform.fact_signal")

_SIGNAL_COLUMNS = ["symbol", "interval", "timestamp", "higher_trend", "signal", "score"]


def _read_canonical_ohlc(
    conn: duckdb.DuckDBPyConnection, symbol: str, interval: str, start: datetime, end: datetime
) -> pd.DataFrame:
    df = conn.execute(
        "SELECT timestamp, open, high, low, close, volume FROM fact_ohlcv_canonical "
        "WHERE symbol = ? AND interval = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
        [symbol, interval, start, end],
    ).fetch_df()
    df = df.rename(
        columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "volume": "Volume"}
    )
    return df.set_index("timestamp")


def load_fact_signal(
    conn: duckdb.DuckDBPyConnection,
    symbols: list[str],
    lower_interval: str,
    higher_interval: str,
    start: datetime,
    end: datetime,
) -> int:
    """Populate fact_signal for `lower_interval` only — bot.py's strategy is
    inherently a lower-timeframe signal informed by a higher-timeframe trend
    filter; a standalone higher-interval "signal" isn't something the
    strategy actually has (see schema.py).

    Reuses bot.py's own get_trend_4h (via compute_market_phases),
    generate_entry_signal and calculate_score unchanged — this is
    orchestration (read canonical rows, call the existing functions once
    per candle, write the result), not a second strategy implementation.
    Idempotent: INSERT OR REPLACE keyed on (symbol, interval, timestamp).
    """
    total = 0
    for symbol in symbols:
        df_1h = _read_canonical_ohlc(conn, symbol, lower_interval, start, end)
        df_4h = _read_canonical_ohlc(conn, symbol, higher_interval, start, end)
        if df_1h.empty or df_4h.empty:
            continue

        df_1h = add_indicators(df_1h)
        df_4h = add_indicators(df_4h)

        # "As of" the higher-interval context known at each lower-interval
        # candle — the most recently *closed* higher-interval candle at
        # that point, same relationship run_backtest uses live via a
        # growing df_4h slice, just computed directly instead of looped.
        phases_4h = compute_market_phases(df_4h)
        higher_trend_1h = phases_4h.reindex(df_1h.index, method="ffill")
        df_4h_asof_1h = df_4h.reindex(df_1h.index, method="ffill")

        rows = []
        for ts in df_1h.index:
            trend = higher_trend_1h.loc[ts]
            if pd.isna(trend):
                continue  # before the first higher-interval candle has closed

            one_row_1h = df_1h.loc[[ts]]
            one_row_4h = df_4h_asof_1h.loc[[ts]]

            signal = generate_entry_signal(one_row_1h, trend)
            score = calculate_score(one_row_4h, one_row_1h)

            rows.append({
                "symbol": symbol, "interval": lower_interval, "timestamp": ts,
                "higher_trend": trend, "signal": signal, "score": score,
            })

        if not rows:
            continue

        out = pd.DataFrame(rows, columns=_SIGNAL_COLUMNS)
        conn.register("_fact_signal_batch", out)
        conn.execute(
            f"INSERT OR REPLACE INTO fact_signal ({', '.join(_SIGNAL_COLUMNS)}) "
            f"SELECT {', '.join(_SIGNAL_COLUMNS)} FROM _fact_signal_batch"
        )
        conn.unregister("_fact_signal_batch")

        log.info("%s %s | loaded %d rows into fact_signal", symbol, lower_interval, len(out))
        total += len(out)

    return total
