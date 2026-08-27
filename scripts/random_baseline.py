"""Reference-strategy baselines: two lower bars for the real strategy to
clear, on both sides of "no real signal at all" vs. "the simplest possible
non-random rule".

Usage:
    python -m scripts.random_baseline

Methodology (results and interpretation: NOTES.md):

- For each of the two windows already used by scripts/parameter_sweep.py
  (selection: most recent 365 days; validation: the 365 days before that),
  run the real EmaRsiMacdStrategy first. Its per-symbol closed-trade count
  becomes RandomStrategy's target for that symbol/window — the comparison
  is "same number of trades, same ATR-based stop, different entries", not
  "however many random trades happen to fire".
- RandomStrategy uses bot.create_trade_plan for stop-loss/take-profit (same
  mechanics as the real strategy) but has no trend filter and no
  is_trade_worth_it gate — entries are pure random side/timing draws. See
  strategies/random_strategy.py. IMPORTANT SCOPING NOTE: it removes both
  the trend filter and the RR gate *simultaneously* — a result here shows
  whether that combination (plus the underlying EMA/RSI/MACD confluence in
  generate_entry_signal) adds value over having neither, not which single
  piece of it is doing the work. Attributing the gap to "the trend filter"
  specifically would be a claim this design can't support.
- SmaCrossoverStrategy is the other reference point: not "no entry logic"
  but "the simplest non-random entry logic" — a single moving-average
  cross, same ATR-based stop, no trend filter, no RR gate, no trade-count
  matching (it trades on its own natural frequency). See
  strategies/sma_crossover.py. This tests whether EmaRsiMacdStrategy's
  extra complexity earns its keep against the cheapest alternative that
  isn't just noise.
- One random seed is one draw, and a single draw is itself noise — the same
  reason the parameter sweep needed two separate windows, not one. Run
  N_SEEDS=30 independent draws per window and report the distribution
  (median, min, max) of Return/MaxDD, not a single number.
- Evaluated on Return/MaxDD, same metric the parameter sweep settled on —
  the one that held up across time windows where raw return didn't.
- Every random seed's per-symbol RandomStrategy instance is reseeded with
  seed*1000 + symbol_index rather than the bare seed, so different symbols
  don't draw identical trigger-index/side sequences under the same outer
  seed (they'd otherwise all reset to the same random.Random(seed) state in
  RandomStrategy.prepare, since it takes a single scalar seed). Still fully
  deterministic given the outer seed.
- Every run is persisted into fact_backtest_run with strategy_name
  ("ema_rsi_macd" / "sma_crossover" for the two deterministic reference
  runs, "random" for each of the 30 seeds) via the same record_backtest_run
  used everywhere else. Random runs are run-level summaries only, not
  individual trades: fact_backtest_trade would otherwise gain 30 seeds x 2
  windows x per-symbol-trades rows of data nobody queries individually. The
  two deterministic runs (real strategy, SMA crossover) do get their trades
  recorded, same as any other single backtest run.
"""
import logging
import statistics
from datetime import datetime, timedelta, timezone

from backtest import FEE_RATE, WARMUP, load_history, log_report, run_backtest
from config import ACCOUNT_SIZE, CURATED_DB_PATH, RAW_DATA_DIR, SYMBOLS, setup_logging
from ingestion.raw_store import RawStore
from strategies.ema_rsi_macd import EmaRsiMacdStrategy
from strategies.random_strategy import RandomStrategy
from strategies.sma_crossover import SmaCrossoverStrategy
from transform.db import connect
from transform.dims import populate_all_dims
from transform.fact_backtest import record_backtest_run, record_backtest_trades
from transform.schema import create_schema

log = logging.getLogger("scripts.random_baseline")

N_SEEDS = 30
WINDOW_DAYS = 365

# record_backtest_run's bh_metrics columns are all nullable — this script
# isn't comparing against buy-and-hold, so every field is left unset rather
# than faked as zero.
_NO_BH_METRICS: dict = {}


def _load_universe(symbols: list[str], start: datetime, end: datetime, refresh: bool) -> dict:
    store = RawStore(base_dir=RAW_DATA_DIR)
    universe = {}
    for symbol in symbols:
        df_4h = load_history(symbol, "4h", start - timedelta(days=30), end, store, refresh)
        df_1h = load_history(symbol, "1h", start, end, store, refresh)
        universe[symbol] = (df_4h, df_1h)
    return universe


def _run_real_strategy(universe: dict) -> tuple[list[dict], dict[str, int]]:
    """Real EmaRsiMacdStrategy across the whole universe. Returns all closed
    trades plus each symbol's own closed-trade count — RandomStrategy has to
    match that count symbol by symbol, not in aggregate."""
    all_trades = []
    target_trades = {}
    for symbol, (df_4h, df_1h) in universe.items():
        trades = run_backtest(symbol, df_4h, df_1h, EmaRsiMacdStrategy())
        all_trades.extend(trades)
        target_trades[symbol] = len([t for t in trades if t.get("pnl") is not None])
    return all_trades, target_trades


def _run_random_seed(universe: dict, target_trades: dict[str, int], seed: int) -> list[dict]:
    all_trades = []
    for i, symbol in enumerate(sorted(universe)):
        df_4h, df_1h = universe[symbol]
        strategy = RandomStrategy(target_trades=target_trades.get(symbol, 0), seed=seed * 1000 + i, warmup=WARMUP)
        all_trades.extend(run_backtest(symbol, df_4h, df_1h, strategy))
    return all_trades


def _run_strategy_once(universe: dict, strategy_factory) -> list[dict]:
    """A deterministic strategy (no per-run randomness), run across the
    whole universe on its own natural trade frequency — no target-trades
    matching, unlike RandomStrategy. Used for SmaCrossoverStrategy."""
    all_trades = []
    for symbol, (df_4h, df_1h) in universe.items():
        all_trades.extend(run_backtest(symbol, df_4h, df_1h, strategy_factory()))
    return all_trades


def _log_distribution(window_label: str, real_result: dict, sma_result: dict, random_results: list[dict]) -> None:
    returns = [r["return_pct"] for r in random_results]
    drawdowns = [r["max_drawdown_pct"] for r in random_results]
    ratios = [r["return_to_dd_ratio"] for r in random_results]

    log.info("--- %s: real strategy vs. SMA crossover vs. %d random-seed draws ---", window_label, len(random_results))
    log.info(
        "  Real strategy       — trades=%-4d return=%+7.1f%%  maxDD=%6.1f%%  Return/MaxDD=%7.2f",
        real_result["trades_count"], real_result["return_pct"],
        real_result["max_drawdown_pct"], real_result["return_to_dd_ratio"],
    )
    log.info(
        "  SMA crossover       — trades=%-4d return=%+7.1f%%  maxDD=%6.1f%%  Return/MaxDD=%7.2f",
        sma_result["trades_count"], sma_result["return_pct"],
        sma_result["max_drawdown_pct"], sma_result["return_to_dd_ratio"],
    )
    log.info(
        "  Random (median)     — return=%+7.1f%%  maxDD=%6.1f%%  Return/MaxDD=%7.2f",
        statistics.median(returns), statistics.median(drawdowns), statistics.median(ratios),
    )
    log.info(
        "  Random (min..max)   — return=[%+.1f%%, %+.1f%%]  maxDD=[%.1f%%, %.1f%%]  Return/MaxDD=[%.2f, %.2f]",
        min(returns), max(returns), min(drawdowns), max(drawdowns), min(ratios), max(ratios),
    )


def run_window(window_label: str, symbols: list[str], start: datetime, end: datetime, conn, refresh: bool) -> dict:
    log.info("=" * 70)
    log.info("WINDOW: %s  (%s to %s)", window_label, start.date(), end.date())
    log.info("=" * 70)

    universe = _load_universe(symbols, start, end, refresh)
    days = (end - start).days
    account = len(symbols) * ACCOUNT_SIZE

    real_trades, target_trades = _run_real_strategy(universe)
    real_result = log_report(f"{window_label} — real strategy (EmaRsiMacdStrategy)", real_trades, account)
    real_run_id = record_backtest_run(
        conn, symbols, days, start, end, "1h", "4h", FEE_RATE, WARMUP,
        real_result, _NO_BH_METRICS, strategy_name="ema_rsi_macd",
    )
    record_backtest_trades(conn, real_run_id, real_trades)
    log.info("  target trade counts per symbol: %s", target_trades)

    sma_trades = _run_strategy_once(universe, SmaCrossoverStrategy)
    sma_result = log_report(f"{window_label} — SMA crossover baseline", sma_trades, account)
    sma_run_id = record_backtest_run(
        conn, symbols, days, start, end, "1h", "4h", FEE_RATE, WARMUP,
        sma_result, _NO_BH_METRICS, strategy_name="sma_crossover",
    )
    record_backtest_trades(conn, sma_run_id, sma_trades)

    random_results = []
    for seed in range(N_SEEDS):
        random_trades = _run_random_seed(universe, target_trades, seed)
        result = log_report(f"{window_label} — random seed={seed}", random_trades, account)
        record_backtest_run(
            conn, symbols, days, start, end, "1h", "4h", FEE_RATE, WARMUP,
            result, _NO_BH_METRICS, strategy_name="random",
        )
        random_results.append(result)

    _log_distribution(window_label, real_result, sma_result, random_results)

    return {"real": real_result, "sma": sma_result, "random": random_results}


def main() -> None:
    setup_logging()

    end_selection = datetime.now(timezone.utc)
    start_selection = end_selection - timedelta(days=WINDOW_DAYS)
    end_validation = start_selection
    start_validation = end_validation - timedelta(days=WINDOW_DAYS)

    conn = connect(CURATED_DB_PATH)
    try:
        create_schema(conn)
        populate_all_dims(conn)

        selection = run_window("SELECTION (recent 365d)", SYMBOLS, start_selection, end_selection, conn, refresh=True)
        validation = run_window("VALIDATION (prior 365d)", SYMBOLS, start_validation, end_validation, conn, refresh=True)
    finally:
        conn.close()

    log.info("=" * 70)
    n_random = len(selection["random"]) + len(validation["random"])
    log.info(
        "Reference-strategy baselines complete. %d runs recorded into fact_backtest_run "
        "(2 real + 2 SMA crossover + %d random).",
        4 + n_random, n_random,
    )


if __name__ == "__main__":
    main()
