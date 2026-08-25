"""Star schema DDL for the Curated Layer. See MODEL.md for the reasoning
behind the grain and canonical-row decisions before touching this file.
"""

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS dim_symbol (
    symbol VARCHAR PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS dim_interval (
    interval VARCHAR PRIMARY KEY,
    interval_minutes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_source (
    source VARCHAR PRIMARY KEY,
    -- Canonical-row priority for fact_ohlcv_canonical below. Lower wins.
    -- Binance has full history, Kraken is validation-only (see NOTES.md) —
    -- this ordering is the documented decision from ROADMAP.md Phase 2/3,
    -- not an implicit choice buried in a query.
    priority INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dim_date (
    date DATE PRIMARY KEY,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    day INTEGER NOT NULL,
    day_of_week INTEGER NOT NULL,  -- 0=Monday .. 6=Sunday (pandas .dayofweek convention)
    day_name VARCHAR NOT NULL,
    is_weekend BOOLEAN NOT NULL
);

-- Grain: one row per (symbol, interval, source, timestamp) — source is
-- part of the grain, not a filter on top of it. See MODEL.md.
CREATE TABLE IF NOT EXISTS fact_ohlcv (
    symbol VARCHAR NOT NULL REFERENCES dim_symbol(symbol),
    interval VARCHAR NOT NULL REFERENCES dim_interval(interval),
    source VARCHAR NOT NULL REFERENCES dim_source(source),
    timestamp TIMESTAMPTZ NOT NULL,
    date DATE NOT NULL REFERENCES dim_date(date),
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    volume DOUBLE NOT NULL,
    PRIMARY KEY (symbol, interval, source, timestamp)
);

-- One row per (symbol, interval, timestamp): the row from the
-- highest-priority source that has data for that candle. Binance unless
-- it's missing there, per dim_source.priority — a view, not a stored flag,
-- so it can never go stale as sources are added or backfilled.
CREATE OR REPLACE VIEW fact_ohlcv_canonical AS
SELECT f.symbol, f.interval, f.timestamp, f.date,
       f.open, f.high, f.low, f.close, f.volume, f.source
FROM fact_ohlcv f
JOIN dim_source s ON f.source = s.source
QUALIFY ROW_NUMBER() OVER (
    PARTITION BY f.symbol, f.interval, f.timestamp
    ORDER BY s.priority
) = 1;

-- Grain: (symbol, interval, timestamp) — computed from the *canonical*
-- OHLCV row only (see fact_ohlcv_canonical above), one indicator reading
-- per candle, not per source. Values reuse bot.py's own indicator
-- functions (add_indicators) rather than a second implementation — see
-- MODEL.md "Where indicators and signals get computed". NULL-able: RSI,
-- ATR and Volume_MA need a warmup window before they're defined, and
-- rows without them aren't loaded (see transform/fact_indicator.py).
CREATE TABLE IF NOT EXISTS fact_indicator (
    symbol VARCHAR NOT NULL REFERENCES dim_symbol(symbol),
    interval VARCHAR NOT NULL REFERENCES dim_interval(interval),
    timestamp TIMESTAMPTZ NOT NULL,
    ema_20 DOUBLE,
    ema_50 DOUBLE,
    rsi DOUBLE,
    macd DOUBLE,
    macd_signal DOUBLE,
    macd_hist DOUBLE,
    atr DOUBLE,
    volume_ma DOUBLE,
    PRIMARY KEY (symbol, interval, timestamp)
);

-- Grain: (symbol, interval, timestamp), but only ever populated for
-- config.LOWER_INTERVAL ("1h") — the strategy's signal is inherently a
-- lower-timeframe read informed by a higher-timeframe trend filter (see
-- bot.generate_entry_signal), not something that exists per-interval
-- independently. `interval` stays in the grain for schema consistency
-- with the other fact tables, not because a standalone 4h signal is a
-- real concept here.
CREATE TABLE IF NOT EXISTS fact_signal (
    symbol VARCHAR NOT NULL REFERENCES dim_symbol(symbol),
    interval VARCHAR NOT NULL REFERENCES dim_interval(interval),
    timestamp TIMESTAMPTZ NOT NULL,
    higher_trend VARCHAR NOT NULL,  -- BULLISH/BEARISH/NEUTRAL, from bot.get_trend_4h via the higher interval
    signal VARCHAR NOT NULL,        -- BUY/SELL/HOLD/NOT ENOUGH DATA, from bot.generate_entry_signal
    score DOUBLE,                   -- bot.calculate_score; -999 sentinel on missing inputs, same as bot.py's own convention
    PRIMARY KEY (symbol, interval, timestamp)
);
"""


def create_schema(conn) -> None:
    conn.execute(SCHEMA_DDL)
