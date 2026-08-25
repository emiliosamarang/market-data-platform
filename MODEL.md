# Data Model — Curated Layer

Star schema for the Curated Layer (Phase 3 of `ROADMAP.md`). Written before
the first `CREATE TABLE`, per the project's own rule from Phase 1: decide
the load-bearing structure on paper first, not while writing DDL.

## Target system: DuckDB

Not Postgres. Reasoning:

- No server process to run, manage, or eventually tear down locally —
  DuckDB is embedded, like SQLite, but columnar and built for analytical
  (star-schema, aggregate-heavy) queries rather than OLTP.
- Reads Parquet natively (`SELECT * FROM 'data/raw/.../*.parquet'`), which
  is exactly the Raw Layer's format — no separate loader needed for the
  read side.
- SQL dialect is close enough to Azure SQL (Phase 5) that the eventual
  migration stays a schema/DDL port, not a rewrite of every query.

Postgres would mean an extra local service with its own lifecycle, useful
mainly for concurrent-write scenarios this project doesn't have (single
writer: the transform job).

## Grain decision: `fact_ohlcv`

**Grain: one row per (symbol, interval, source, timestamp).** `source` is
part of the grain, not a filter on top of it — this is the load-bearing
decision the rest of the model depends on, so it's written down before any
table exists.

Follows directly from the Phase 2 finding: Binance and Kraken are kept as
independent rows in the Raw Layer (no merge on load, see `NOTES.md`), and
`fact_ohlcv` preserves that rather than collapsing it. A `source`-less grain
would force a merge decision at load time — exactly the "quietly resolved
inside a transformation" outcome Phase 2's conflict-resolution note was
written to avoid.

**Canonical rows:** a view (`fact_ohlcv_canonical`, name TBD at build time)
or an `is_canonical` boolean column marks the row Binance provides for a
given (symbol, interval, timestamp), except where Binance has no row for
it — the only case Kraken's row becomes canonical. Downstream consumers
(indicators, signals, dashboards) read the canonical view by default; the
raw multi-source rows stay queryable underneath for anyone actually
investigating a disagreement.

## Tables

| Table | Type | Grain | Notes |
|---|---|---|---|
| `dim_symbol` | dimension | one row per symbol | BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, ADAUSDT |
| `dim_date` | dimension | one row per calendar date | standard date dimension (year, month, day, day-of-week, etc.) |
| `dim_interval` | dimension | one row per interval | 1h, 4h |
| `dim_source` | dimension | one row per source | binance, kraken |
| `fact_ohlcv` | fact | (symbol, interval, source, timestamp) | loaded from the Raw Parquet layer, see grain decision above |
| `fact_indicator` | fact | (symbol, interval, timestamp) | EMA/RSI/MACD/ATR, computed against the **canonical** OHLCV row only — one indicator value per candle, not per source |
| `fact_signal` | fact | (symbol, interval, timestamp) | BUY/SELL/HOLD + score, same canonical-row basis as `fact_indicator` |
| `fact_backtest_run` | fact | one row per backtest invocation | run id, timestamp, parameters (days, symbols, strategy constants), **plus the buy-and-hold benchmark for that run** (`bh_return_pct`, `bh_max_drawdown_pct`, `strategy_return_pct`, `strategy_max_drawdown_pct`, `return_to_drawdown_ratio_strategy`, `return_to_drawdown_ratio_bh`, `bullish_phase_return_pct`/`bearish_phase_return_pct` for both strategy and B&H) — not a derived/optional add-on. The 2026-08-25 backtest comparison (see `NOTES.md`) showed raw return alone can point to the wrong conclusion (XRP, 2yr: B&H's higher raw return came with 10x the drawdown) — a run without its benchmark and drawdown stored alongside it isn't complete enough to judge |
| `fact_backtest_trade` | fact | one row per closed trade | foreign key to `fact_backtest_run`; never overwritten — see Historisierung in `ROADMAP.md` |
| `fact_quality_check` | fact | one row per check per run | the eventual home for `data/quality/*.parquet` reports (see `ingestion/quality.py`) |

## Where indicators and signals get computed

**In the transformation layer (`transform/`), not in `bot.py`.**
`bot.py`'s indicator functions (`calculate_rsi`, `calculate_macd`,
`calculate_atr`, `add_indicators`, `generate_entry_signal`, ...) are
**reused by import**, the same way `backtest.py` already does
(`from bot import add_indicators, ...`) — there is no second
implementation to keep in sync. What's new in `transform/` is the
orchestration: read the canonical OHLCV rows from `fact_ohlcv`, call
`bot.py`'s existing functions, write the results into `fact_indicator` /
`fact_signal`. `bot.py`'s live scan and `backtest.py`'s simulation both
keep computing indicators in-memory as they do today — this only adds a
third caller of the same functions, not a competing implementation. The
risk this avoids: `fact_indicator` showing values that don't match what
the backtest actually simulated because two independent implementations
drifted apart.

## Build order — complete ✅

Dependencies point downward — each step needed the previous one to exist
and hold plausible data before the next started. All four are built and
verified against real data (row counts, idempotent reruns):

1. ✅ **`dim_symbol`, `dim_date`, `dim_interval`, `dim_source`** — small,
   static-ish, fast to get right. No dependency on anything else.
2. ✅ **`fact_ohlcv`** — load path from the Raw Parquet layer via `RawStore`.
   Once this held plausible row counts (cross-checked against the Raw
   Layer itself), the foundation was trustworthy enough to build on.
3. ✅ **`fact_indicator`, `fact_signal`** — depend on `fact_ohlcv` (specifically
   its canonical view) being loaded and correct.
4. ✅ **`fact_backtest_run`, `fact_backtest_trade`** — last, since they
   depended on `backtest.py` writing into the DB (additively — the console
   report is unchanged) rather than only printing to the console.

`fact_quality_check` isn't in this ordered list — it's a straightforward
load of the existing `data/quality/*.parquet` reports (see
`ingestion/quality.py`) and can slot in whenever convenient; not built yet.

**Now unblocked:** `fact_backtest_run` stores full parameters (including
git commit hash) per run, so multiple strategy parametrizations can finally
be run and compared against each other — recommended before Phase 5 (Azure
migration), see `ROADMAP.md`.

`fact_quality_check` isn't in this ordered list — it's a straightforward
load of the existing `data/quality/*.parquet` reports (see
`ingestion/quality.py`) and can slot in whenever convenient once
`dim_symbol`/`dim_interval`/`dim_source` exist.
