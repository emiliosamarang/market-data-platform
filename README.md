# Market Data Platform

A data platform for financial market data covering ingestion, storage, analysis, and backtesting. It started as a Binance spot trading bot; the current focus is building a reproducible, idempotent raw-data layer underneath it before drawing any further conclusions from the strategy itself.

## Architecture

```mermaid
flowchart LR
    subgraph Ingestion["ingestion/ — raw data layer"]
        SRC["MarketDataSource\n(base.py, abstract)"] --> BSRC["BinanceSource\n(binance_source.py)\nretry + backoff, pagination"]
        BSRC --> RAW["RawStore\n(raw_store.py)\nmerge + dedupe, atomic write"]
        RAW --> PARQUET[("data/raw/{asset_class}/{source}/\n{symbol}/{interval}/{date}.parquet")]
    end

    subgraph Live["Live trading loop"]
        BOT["bot.py\nscheduler + signal scan"] --> RAW
        BOT -.incremental refresh.-> BSRC
        BOT --> TRADER["trader.py\ntrade lifecycle, OCO orders"]
        TRADER --> EX["exchange.py\nBinance order execution"]
        TRADER --> DB[("database.py\nSQLite: signals, trades")]
        BOT --> SENT["sentiment.py\nFear & Greed + Claude news"]
        TRADER --> NOTIFY["notify.py\nTelegram alerts"]
    end

    subgraph Research["Research"]
        BACKTEST["backtest.py\nhistorical simulation"] --> RAW
        BACKTEST -.--refresh.-> BSRC
    end

    CONFIG["config.py\n.env via python-dotenv"] -.-> BOT
    CONFIG -.-> EX
    CONFIG -.-> BSRC
    CONFIG -.-> RAW
```

**How the pieces fit together:**

| Module | Responsibility |
|---|---|
| `config.py` | Central configuration; secrets loaded from `.env` via `python-dotenv`, never hardcoded |
| `exchange.py` | Thin Binance client wrapper for order execution: places market/OCO orders, rounds lot size/tick size |
| `bot.py` | Indicator calculations, entry-signal strategy, APScheduler loop that scans symbols hourly. Before each scan, incrementally refreshes the raw layer via `BinanceSource` and reads through `RawStore.read()` — no direct kline fetch. A symbol whose refresh fails is skipped for that cycle rather than traded on stale data |
| `trader.py` | Trade lifecycle: opens positions, places OCO take-profit/stop-loss, emergency-closes on OCO failure, syncs fills back into the database |
| `database.py` | SQLite persistence for generated signals and the full trade lifecycle |
| `notify.py` | Telegram push notifications for trade events and circuit breakers |
| `sentiment.py` | Fear & Greed Index + Claude-Haiku-based news sentiment, used as a trade filter |
| `backtest.py` | Historical strategy simulation with equity curve, drawdown, and fee accounting. Reads from the raw layer via `RawStore.read()`, not live from Binance — `--refresh` backfills gaps through `ingestion/` first |
| `ingestion/` | Source-agnostic data ingestion layer (see below), used by both `backtest.py` and `bot.py` |

**`ingestion/` in detail:**

- `MarketDataSource` (`base.py`) — abstract base class defining `fetch_ohlcv(symbol, interval, start, end) -> DataFrame` and the canonical output schema: `timestamp` (UTC, tz-aware), `open`, `high`, `low`, `close`, `volume`, `source`, `symbol`, `interval`.
- `BinanceSource` (`binance_source.py`) — first concrete implementation. Paginates over long date ranges via `startTime`/`endTime`, retries with exponential backoff on rate-limit responses (HTTP 429/418 or Binance error `-1003`), and deduplicates any candle overlap at page boundaries.
- `RawStore` (`raw_store.py`) — writes OHLCV DataFrames to Parquet, partitioned by day, at `data/raw/{asset_class}/{source}/{symbol}/{interval}/{date}.parquet`. Writing is idempotent: it reads the existing partition (if any), merges in the new rows, deduplicates on `(timestamp, source, symbol, interval)`, and writes back atomically (temp file + rename) — loading the same or an overlapping time range twice never produces duplicate rows.

`backtest.py` and `bot.py` use the raw layer in opposite orders, matching their different freshness needs: `backtest.py` reads first and only refreshes on a `MissingDataError` (a slightly stale historical cache is fine); `bot.py` always refreshes a small trailing window first and reads after, since a live scan needs the freshest — possibly still-forming — candle and can't tolerate serving a stale one.

## Tech stack

- **Language:** Python 3.13
- **Data:** pandas, numpy, pyarrow (Parquet)
- **Exchange:** python-binance (REST client)
- **Storage:** SQLite (live trade/signal state), Parquet on local disk (raw market data)
- **Scheduling:** APScheduler
- **Sentiment:** Anthropic Claude (Haiku) + RSS news feeds
- **Notifications:** Telegram Bot API via `requests`
- **Config:** `python-dotenv`
- **Testing:** pytest, pytest-cov, unittest.mock

## Setup

```bash
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt          # runtime dependencies
pip install -r requirements-dev.txt       # + pytest/pytest-cov for development

cp .env.example .env                      # then fill in your real credentials
```

`.env` variables (see `.env.example`):

| Variable | Required for | Notes |
|---|---|---|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Live trading, backtesting, ingestion | Read-only keys are sufficient for ingestion/backtesting |
| `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` | Trade notifications | Optional — notifications are silently skipped if unset |
| `ANTHROPIC_API_KEY` | News sentiment filter | Optional — sentiment defaults to neutral (0.0) if unset |
| `LOG_LEVEL` / `LOG_FILE` | Logging | Optional overrides, see `config.py` |

`.env` is gitignored; never commit real credentials.

## Running tests

```bash
python -m pytest tests/ -v

# with coverage
python -m pytest tests/ --cov=bot --cov-report=term-missing
```

All tests run against synthetic data or mocked clients (`unittest.mock`) — no real API calls, no real orders.

## Running a backtest

```bash
python backtest.py                          # 365 days, all symbols from config.py
python backtest.py --days 730                # 2 years
python backtest.py --symbols BTCUSDT ETHUSDT  # subset of symbols
python backtest.py --refresh                 # backfill missing raw data first
```

Reports per-symbol and combined win rate, profit factor, fees, drawdown, and final equity. Data is read from the raw Parquet layer (`data/raw/`, populated via `ingestion/`) rather than fetched live from Binance. If the raw layer doesn't fully cover the requested range, the run fails with a clear error naming the missing candles — pass `--refresh` to backfill the gap through `ingestion` before backtesting.

## Results and findings

Backtest of the EMA/RSI/MACD/ATR strategy (`bot.py`) against historical Binance spot data, including a realistic 0.1%-per-side taker fee:

| Period | Scope | Net return | Profit factor | Fees | Gross profit |
|---|---|---|---|---|---|
| 90 days | BTCUSDT only | **−6.8%** | **0.75** | — | — |
| 365 days | 3 symbols combined | **+4.2%** *(corrected — originally reported as +12.5%, see below)* | **1.04** | **$847** | **~$972** |
| 365 days | 5 symbols combined | **+17.2%** *(corrected — originally reported as +86.1%)* | **1.17** | **$1312** | **$2174** |

**A reporting bug, found and fixed, not a real return jump:** the combined-report line divided total PnL by a single fixed `ACCOUNT_SIZE` regardless of how many symbols contributed trades — but each symbol trades its own independent `ACCOUNT_SIZE`-sized sleeve, with no shared capital or `MAX_OPEN_POSITIONS` cap across symbols in the backtest. Both rows above were originally misreported (the 3-symbol figure divided $125 net by $1000 instead of $3000; the 5-symbol figure divided $861 by $1000 instead of $5000) — the more symbols in a run, the more inflated the number looked. Fixed in `backtest.py` (`successful * ACCOUNT_SIZE` as the denominator) with a regression test asserting the denominator scales with symbol count. Full root-cause writeup, including the counter-check that closes it out, in `NOTES.md`.

**The result that does survive scrutiny** is the per-symbol breakdown of the 5-symbol run — and it's not flattering:

| Symbol | Trades | Gross | Fees | Net | Fee/Gross |
|---|---|---|---|---|---|
| BTCUSDT | 159 | $389.99 | $372.06 | $17.93 | 95.4% |
| ETHUSDT | 146 | $219.94 | $272.20 | **−$52.26** | 123.8% |
| SOLUSDT | 141 | $452.18 | $215.25 | $236.92 | 47.6% |
| XRPUSDT | 158 | $581.36 | $265.23 | $316.13 | 45.6% |
| ADAUSDT | 145 | $530.02 | $187.30 | $342.73 | 35.3% |

On the two most liquid pairs — BTC and ETH — the strategy is a fee machine with no real edge: $18 net on 159 BTC trades, and ETH loses money outright once fees are subtracted. The entire combined result comes from three alts (ADA/XRP/SOL) during a period when alts happened to run; that's concentration in a favorable window, not a validated edge. And the improved 60.4% fee-to-gross ratio (vs. 87% previously) is not because fees got cheaper — the formula and the 0.1%-per-side rate are byte-identical — it's purely because gross profit was larger this time.

**Conclusion:** this is not just "not yet validated" — it's a concrete negative result on the pairs that matter most. After fees, the strategy has no edge on BTC or ETH, the two most liquid symbols and the ones closest to what could actually be traded at size. The positive combined return is not broad-based: it comes entirely from three smaller-cap alts (ADA/XRP/SOL) during a window in which alts happened to run well — concentration in a favorable period, not a demonstrated edge. Results also swing sharply with the chosen time window (a strongly profitable year vs. a losing quarter), so a single-run backtest number, good or bad, isn't a reliable basis for judging the strategy on its own.

This is exactly why the current priority is the `ingestion/` raw-data layer and its data-quality/reporting correctness rather than further strategy tuning: without a fixed, reproducible historical dataset, proper walk-forward validation, and a backtest that accounts for capital correctly, no result from this strategy — good or bad — is trustworthy enough to act on yet.

## Roadmap

- **Backfill** — proactively bulk-load full symbol/interval history through `ingestion/` (rather than relying on ad-hoc `--refresh` calls) so the raw layer is complete before any backtest run
- **Data quality checks** — gap detection, duplicate/monotonicity checks, and schema validation on raw partitions before they're used downstream
- **Second data source** — add another `MarketDataSource` implementation (e.g. a different exchange) to cross-validate prices and reduce single-source risk
- **Azure migration** — move raw storage from local Parquet to Azure (Blob Storage / Data Lake), enabling shared access and scheduled ingestion jobs
- **Power BI** — dashboards on top of the raw/processed data for strategy and market analysis outside of log files
