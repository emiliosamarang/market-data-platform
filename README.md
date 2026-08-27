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

### Buy-and-hold benchmark: risk, not just return, is the real story

Every backtest now runs against a buy-and-hold benchmark (same capital base, same fee rate) and reports max drawdown and return-per-unit-of-drawdown for both:

| Period | Strategy return | B&H return | Strategy max DD | B&H max DD | Strategy Return/DD | B&H Return/DD |
|---|---|---|---|---|---|---|
| 365 days, combined | **+17.5%** | −50.7% | **3.6%** | 69.2% | **4.82** | −0.73 |
| 2 years, combined | **+24.6%** | +14.5% | **8.8%** | 70.0% | **2.79** | 0.21 |

Raw return alone is not a clean story — buy-and-hold beats the strategy outright on BTC and (over 2 years) on XRP, where a held rally outperforms repeated in/out trading net of fees. But **one result is consistent across every symbol and both periods: max drawdown**. The strategy's drawdown never exceeds 25%; buy-and-hold's ranges from 54% to 89%. Even where buy-and-hold wins on raw return (XRP, 2 years: +143.2% vs +72.9%), the strategy wins on a risk-adjusted basis by nearly 6x (11.32 vs 1.96 return-per-unit-drawdown) — B&H's higher return comes with more than 10x the drawdown.

Splitting return by market regime (bullish/bearish, via `bot.py`'s own trend filter) shows why: in bullish phases the strategy captures only a small fraction of a held rally (repeated entries/exits, fees on every round trip), while in bearish phases buy-and-hold's compounded loss is severe (roughly −85% to −98% attributable to bearish-classified hours, combined) and the strategy stays flat to positive. It trades most of the upside for most of the downside — a real, quantifiable tradeoff, not free money either direction. Full per-symbol breakdown, the phase-return methodology, and the caveat on how to read those compounded percentages: `NOTES.md`.

### Were the original strategy thresholds ever actually justified?

A univariate parameter sweep (`scripts/parameter_sweep.py`) checked two of them — `ATR_SL_MULTIPLE` and `EMA20_DISTANCE_THRESHOLD` — against Return/MaxDD (not raw return) on two separate, non-overlapping 365-day windows: pick a good region on one, check it holds on the other. Result, not a disappointing one: **`EMA20_DISTANCE_THRESHOLD=0.03` sits in a genuine plateau (0.03–0.08 all positive on both windows) while tighter values (0.01–0.02) look fine on the selection window and go negative on the validation window** — exactly the overfitting trap the second window exists to catch. `ATR_SL_MULTIPLE=1.5` is more nuanced: it's the best of the values that are structurally even testable, but it also sits right at a hard edge — stop-loss and take-profit are both ATR multiples, so every trade's reward:risk ratio is fixed at `3.0 / ATR_SL_MULTIPLE` regardless of market conditions, and once that drops below `MIN_RR=2.0` (any value above 1.5), zero trades pass the filter at all, not fewer. A coupling nobody had surfaced before this sweep. Full tables and the honest verdict on each: `NOTES.md`.

This is exactly why the current priority is the `ingestion/` raw-data layer and its data-quality/reporting correctness rather than further strategy tuning: without a fixed, reproducible historical dataset and a backtest that accounts for capital correctly, no result from this strategy — good or bad — is trustworthy enough to act on yet. The parameter sweep above is a first, narrow slice of walk-forward validation (two parameters, two windows) — not the proper multi-window validation this conclusion is still waiting on.

### Three reference points, not just buy-and-hold

Every backtest so far had one comparison point (buy-and-hold) that isn't structurally similar to the strategy at all — it never trades, has no stop-loss, no fee-per-trade. `strategies/` adds two proper apples-to-apples baselines behind a shared `Strategy` interface (mirroring `ingestion.base.MarketDataSource`): `EmaRsiMacdStrategy` wraps the existing `bot.py` logic unchanged; `RandomStrategy` uses the *same* ATR-based stop-loss/take-profit (via `bot.create_trade_plan`) and the *same* number of trades per symbol per window as the real strategy, but picks entry side and timing at random, with no trend filter and no reward:risk gate — the floor. `SmaCrossoverStrategy` uses the same ATR stop again, but enters on the simplest non-random rule there is: price crossing a single 50-period moving average, no trend filter, no RR gate, on its own natural trade frequency — the "is the added complexity even worth it" reference.

`scripts/random_baseline.py` ran the real strategy and the SMA crossover once per window (deterministic), and `RandomStrategy` across 30 independent seeds per window, on the same two 365-day windows the parameter sweep used:

| Window | Real strategy Return/MaxDD | SMA crossover Return/MaxDD | Random median | Random range (30 seeds) |
|---|---|---|---|---|
| Selection (recent 365d) | **4.94** (746 trades) | −0.95 (1086 trades) | −0.90 | [−1.00, −0.05] |
| Validation (prior 365d) | **0.58** (757 trades) | −0.92 (1116 trades) | −0.89 | [−1.00, −0.57] |

The real strategy beats **every single one** of the 60 random draws across both windows — not just the median, the best-case random seed too. Since the stop-loss mechanics are identical between the two, the gap is attributable to the decision logic the strategy adds on top of the stop, not the stop-loss alone.

**Scoping that precisely, since it's easy to overstate:** `RandomStrategy` removes the trend filter and the RR gate *simultaneously*, not one at a time. This result shows that combination (trend filter + RR gate + the EMA/RSI/MACD confluence in `generate_entry_signal`) adds value over no filtering at all — it does **not** show which individual piece is doing the work. "The trend filter works" is a claim this design can't support; that would need the two removed independently, not together.

**The more informative result is the SMA crossover**, which sits *below* the random median in both windows (Selection: −0.95 vs. −0.90; Validation: −0.92 vs. −0.89) despite the same stop and nearly double the trade count. A simple, plausible-sounding rule isn't a cheaper substitute for the strategy's three-part entry logic here — it's not even competitive with picking entries at random. That's a stronger justification for the added complexity than the random comparison alone: the strategy isn't just beating "no signal," it's clearly beating "a simpler signal" too.

Full per-window trade counts, per-symbol targets, and methodology: `NOTES.md`.

## Roadmap

- **Backfill** — proactively bulk-load full symbol/interval history through `ingestion/` (rather than relying on ad-hoc `--refresh` calls) so the raw layer is complete before any backtest run
- **Data quality checks** — gap detection, duplicate/monotonicity checks, and schema validation on raw partitions before they're used downstream
- **Second data source** — add another `MarketDataSource` implementation (e.g. a different exchange) to cross-validate prices and reduce single-source risk
- **Azure migration** — move raw storage from local Parquet to Azure (Blob Storage / Data Lake), enabling shared access and scheduled ingestion jobs
- **Power BI** — dashboards on top of the raw/processed data for strategy and market analysis outside of log files
