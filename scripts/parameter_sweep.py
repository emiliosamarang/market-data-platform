"""One-off parameter sweep: are the original strategy thresholds actually
reasonable, or just plausible-looking defaults nobody validated?

Usage:
    python -m scripts.parameter_sweep

Methodology (results and interpretation: NOTES.md):

- Two parameters, swept univariately — one varied at a time, the other
  held at its baseline — not a full cross-product grid. Few enough runs
  that a genuine plateau, if one exists, stays visible instead of drowned
  out by chance ("bei genug Versuchen sieht immer irgendwas gut aus").
- Evaluated on strategy Return/MaxDD, not raw return — the metric that
  actually held up across the 365-day and 2-year benchmark windows
  earlier, not the one that swung wildly with the time window.
- Every level runs on two separate, non-overlapping 365-day windows: the
  most recent year (selection) and the year before it (validation). A
  level that only looks good in the window it was picked on is noise, not
  signal — the whole point of holding out a second window.
- Every run is persisted into fact_backtest_run/fact_backtest_trade like
  any other backtest.py run (same functions, not a parallel bookkeeping
  path), so the full parameter set and result are on record, not just
  what's printed here.
"""
import logging
from datetime import datetime, timedelta, timezone

import bot
from backtest import (
    FEE_RATE,
    WARMUP,
    combine_buy_and_hold,
    compute_buy_and_hold,
    compute_phase_returns_buy_and_hold,
    compute_phase_returns_strategy,
    load_history,
    log_report,
    run_backtest,
)
from config import ACCOUNT_SIZE, CURATED_DB_PATH, RAW_DATA_DIR, SYMBOLS, setup_logging
from ingestion.raw_store import RawStore
from transform.db import connect
from transform.dims import populate_all_dims
from transform.fact_backtest import record_backtest_run, record_backtest_trades
from transform.schema import create_schema

log = logging.getLogger("scripts.parameter_sweep")

# Baseline: 1.5. Levels above ~1.5 aren't just "different", they're
# structurally dead: stop-loss and take-profit are both ATR multiples, so
# every signal's reward:risk ratio is exactly ATR_TP_MULTIPLE / this value
# (3.0 fixed here) regardless of market conditions. Once that ratio drops
# below MIN_RR (2.0, held fixed), *no* trade can ever pass
# is_trade_worth_it — not "fewer trades", zero. The sweep deliberately
# crosses that boundary (1.75, 2.0) to show the cliff rather than avoid it.
ATR_SL_LEVELS = [1.0, 1.25, 1.5, 1.75, 2.0]

# Baseline: 0.03 (3%). Independent of the ratio coupling above — this only
# gates entry timing (how far price may sit from EMA20), not SL/TP
# geometry, so it doesn't share that cliff.
EMA20_DISTANCE_LEVELS = [0.01, 0.02, 0.03, 0.05, 0.08]

WINDOW_DAYS = 365


def _load_universe(symbols: list[str], start: datetime, end: datetime, refresh: bool):
    store = RawStore(base_dir=RAW_DATA_DIR)
    universe = {}
    for symbol in symbols:
        df_4h = load_history(symbol, "4h", start - timedelta(days=30), end, store, refresh)
        df_1h = load_history(symbol, "1h", start, end, store, refresh)
        universe[symbol] = (df_4h, df_1h)
    return universe


def _run_all_symbols(universe: dict) -> list[dict]:
    all_trades = []
    for symbol, (df_4h, df_1h) in universe.items():
        all_trades.extend(run_backtest(symbol, df_4h, df_1h))
    return all_trades


def _record_run(conn, symbols, days, start, end, all_trades, strategy_result, bh_combined) -> str:
    account = len(symbols) * ACCOUNT_SIZE
    phase = compute_phase_returns_strategy(all_trades, account)
    strategy_metrics = {**strategy_result, "bullish_phase_return_pct": phase["BULLISH"], "bearish_phase_return_pct": phase["BEARISH"]}
    bh_metrics = {
        "return_pct": bh_combined["return_pct"],
        "max_drawdown_pct": bh_combined["max_drawdown_pct"],
        "return_to_dd_ratio": bh_combined.get("return_to_dd_ratio"),
        "bullish_phase_return_pct": bh_combined["phase_returns"]["BULLISH"],
        "bearish_phase_return_pct": bh_combined["phase_returns"]["BEARISH"],
        "neutral_phase_return_pct": bh_combined["phase_returns"]["NEUTRAL"],
    }
    run_id = record_backtest_run(
        conn, symbols, days, start, end, "1h", "4h", FEE_RATE, WARMUP, strategy_metrics, bh_metrics,
    )
    record_backtest_trades(conn, run_id, all_trades)
    return run_id


def sweep_parameter(
    param_name: str, attr: str, levels: list[float],
    universe: dict, symbols: list[str], days: int, start: datetime, end: datetime,
    conn, bh_combined: dict,
) -> list[dict]:
    """Vary bot.<attr> across `levels`, holding everything else fixed. Each
    level is a full backtest across the whole symbol universe, recorded
    into the Curated Layer, then bot.<attr> is restored — never left
    mutated for anything running after this function returns."""
    baseline = getattr(bot, attr)
    results = []
    try:
        for level in levels:
            setattr(bot, attr, level)
            all_trades = _run_all_symbols(universe)
            account = len(symbols) * ACCOUNT_SIZE
            result = log_report(f"{param_name}={level}", all_trades, account)
            run_id = _record_run(conn, symbols, days, start, end, all_trades, result, bh_combined)
            results.append({"level": level, "run_id": run_id, **result})
    finally:
        setattr(bot, attr, baseline)
    return results


def _log_sweep_table(window_label: str, param_name: str, results: list[dict], baseline: float) -> None:
    log.info("--- %s: %s sweep ---", window_label, param_name)
    for r in results:
        marker = "  (baseline)" if r["level"] == baseline else ""
        log.info(
            "  %s=%-6s trades=%-4d return=%+7.1f%%  maxDD=%6.1f%%  Return/MaxDD=%7.2f%s",
            param_name, r["level"], r["trades_count"], r["return_pct"], r["max_drawdown_pct"],
            r["return_to_dd_ratio"], marker,
        )


def run_window(window_label: str, symbols: list[str], start: datetime, end: datetime, conn, refresh: bool) -> dict:
    log.info("=" * 70)
    log.info("WINDOW: %s  (%s to %s)", window_label, start.date(), end.date())
    log.info("=" * 70)

    universe = _load_universe(symbols, start, end, refresh)
    days = (end - start).days

    # Buy-and-hold doesn't depend on strategy parameters — compute once per
    # window, reuse as the benchmark columns for every recorded run below.
    bh_results = []
    for symbol, (df_4h, df_1h) in universe.items():
        bh = compute_buy_and_hold(df_1h, ACCOUNT_SIZE)
        bh["symbol"] = symbol
        bh["phase_returns"] = compute_phase_returns_buy_and_hold(df_1h, df_4h, ACCOUNT_SIZE)
        bh_results.append(bh)
    bh_combined = combine_buy_and_hold(bh_results, ACCOUNT_SIZE)

    atr_sl_baseline = bot.ATR_SL_MULTIPLE
    ema20_baseline = bot.EMA20_DISTANCE_THRESHOLD

    atr_results = sweep_parameter(
        "ATR_SL_MULTIPLE", "ATR_SL_MULTIPLE", ATR_SL_LEVELS, universe, symbols, days, start, end, conn, bh_combined,
    )
    ema_results = sweep_parameter(
        "EMA20_DISTANCE_THRESHOLD", "EMA20_DISTANCE_THRESHOLD", EMA20_DISTANCE_LEVELS,
        universe, symbols, days, start, end, conn, bh_combined,
    )

    _log_sweep_table(window_label, "ATR_SL_MULTIPLE", atr_results, atr_sl_baseline)
    _log_sweep_table(window_label, "EMA20_DISTANCE_THRESHOLD", ema_results, ema20_baseline)

    return {"atr_sl": atr_results, "ema20_dist": ema_results, "bh_combined_return_pct": bh_combined["return_pct"]}


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
    log.info("Sweep complete. %d runs recorded into fact_backtest_run.", (
        len(selection["atr_sl"]) + len(selection["ema20_dist"])
        + len(validation["atr_sl"]) + len(validation["ema20_dist"])
    ))


if __name__ == "__main__":
    main()
