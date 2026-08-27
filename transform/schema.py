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

-- One row per backtest.py invocation. Wide and parameter-heavy on purpose:
-- the point is that in three months you can tell exactly how a run came
-- about without re-deriving it. commit_hash + is_dirty is what makes a run
-- reproducible rather than just "some result" — see NOTES.md. Never
-- overwritten (see ROADMAP.md Historisierung); run_id is a fresh uuid4 per
-- invocation, not derived from run_ts, so two runs in the same second
-- can't collide.
CREATE TABLE IF NOT EXISTS fact_backtest_run (
    run_id VARCHAR PRIMARY KEY,
    run_ts TIMESTAMPTZ NOT NULL,
    -- Which Strategy implementation produced this run (see strategies/).
    -- The ema_fast/rsi_period/... columns below are still stamped with
    -- bot.py's current constants regardless of strategy_name (they're
    -- real values, not fabricated), but only meaningfully *used* by the
    -- decision logic when strategy_name == "ema_rsi_macd" — a query
    -- comparing strategies should filter/group on this column, not assume
    -- every column applies to every row.
    strategy_name VARCHAR NOT NULL DEFAULT 'ema_rsi_macd',
    commit_hash VARCHAR,      -- NULL if git wasn't available — the run is still recorded, just not reproducible
    is_dirty BOOLEAN,         -- true if the working tree had uncommitted changes at run time
    symbols VARCHAR[] NOT NULL,
    days INTEGER NOT NULL,
    start_ts TIMESTAMPTZ NOT NULL,
    end_ts TIMESTAMPTZ NOT NULL,
    interval_lower VARCHAR NOT NULL,
    interval_higher VARCHAR NOT NULL,
    account_size DOUBLE NOT NULL,
    fee_rate DOUBLE NOT NULL,
    risk_per_trade DOUBLE NOT NULL,
    min_rr DOUBLE NOT NULL,
    warmup INTEGER NOT NULL,
    -- Strategy thresholds — snapshotted from bot.py's own named constants
    -- (see bot.py), not a hand-copied second definition.
    ema_fast INTEGER NOT NULL,
    ema_slow INTEGER NOT NULL,
    rsi_period INTEGER NOT NULL,
    rsi_bullish_low DOUBLE NOT NULL,
    rsi_bullish_high DOUBLE NOT NULL,
    rsi_bearish_low DOUBLE NOT NULL,
    rsi_bearish_high DOUBLE NOT NULL,
    macd_fast INTEGER NOT NULL,
    macd_slow INTEGER NOT NULL,
    macd_signal_period INTEGER NOT NULL,
    atr_period INTEGER NOT NULL,
    atr_sl_multiple DOUBLE NOT NULL,
    atr_tp_multiple DOUBLE NOT NULL,
    ema20_distance_threshold DOUBLE NOT NULL,
    volume_ma_window INTEGER NOT NULL,
    -- Result: strategy and buy-and-hold benchmark side by side, not just
    -- strategy return — see backtest.py's buy-and-hold benchmark work.
    trades_count INTEGER,
    win_rate_pct DOUBLE,
    profit_factor DOUBLE,
    total_fees DOUBLE,
    strategy_return_pct DOUBLE,
    strategy_max_drawdown_pct DOUBLE,
    strategy_return_to_dd_ratio DOUBLE,
    strategy_bullish_phase_return_pct DOUBLE,
    strategy_bearish_phase_return_pct DOUBLE,
    bh_return_pct DOUBLE,
    bh_max_drawdown_pct DOUBLE,
    bh_return_to_dd_ratio DOUBLE,
    bh_bullish_phase_return_pct DOUBLE,
    bh_bearish_phase_return_pct DOUBLE,
    bh_neutral_phase_return_pct DOUBLE
);

-- One row per closed trade produced by a run. trade_seq is an ordinal
-- within the run (trades have no natural unique id otherwise). Never
-- overwritten, same as fact_backtest_run — a run's trades are exactly
-- what backtest.py's own trade dicts contain, not a derived summary.
CREATE TABLE IF NOT EXISTS fact_backtest_trade (
    run_id VARCHAR NOT NULL REFERENCES fact_backtest_run(run_id),
    trade_seq INTEGER NOT NULL,
    symbol VARCHAR NOT NULL REFERENCES dim_symbol(symbol),
    side VARCHAR NOT NULL,
    entry_time TIMESTAMPTZ NOT NULL,
    exit_time TIMESTAMPTZ,
    entry DOUBLE NOT NULL,
    exit_price DOUBLE,
    stop_loss DOUBLE NOT NULL,
    take_profit DOUBLE NOT NULL,
    size DOUBLE NOT NULL,
    exit_reason VARCHAR,
    fee DOUBLE,
    pnl DOUBLE,
    PRIMARY KEY (run_id, trade_seq)
);
"""


def create_schema(conn) -> None:
    conn.execute(SCHEMA_DDL)
    # Migration for databases created before strategy_name existed: the
    # CREATE TABLE above only applies to brand-new tables, so an already
    # populated fact_backtest_run needs this explicitly. Idempotent and
    # backfilling, same as CREATE TABLE IF NOT EXISTS above — existing rows
    # get the default rather than losing their history.
    conn.execute(
        "ALTER TABLE fact_backtest_run ADD COLUMN IF NOT EXISTS "
        "strategy_name VARCHAR DEFAULT 'ema_rsi_macd'"
    )
