"""Persists backtest.py's run results into the Curated Layer.

Additive, not a replacement: backtest.py's console report (log_report) is
unchanged and stays the immediate, no-DB-query feedback loop for
development. This just also writes the run so it can be compared against
others later — the last step in MODEL.md's build order, and the
prerequisite for actually sweeping strategy parameters against each other.
"""
import logging
import subprocess
import uuid
from datetime import datetime, timezone

import duckdb
import pandas as pd

import bot
import config

log = logging.getLogger("transform.fact_backtest")

_TRADE_COLUMNS = [
    "run_id", "trade_seq", "symbol", "side", "entry_time", "exit_time",
    "entry", "exit_price", "stop_loss", "take_profit", "size", "exit_reason", "fee", "pnl",
]


def _git_info() -> tuple[str | None, bool]:
    """(commit_hash, is_dirty). None/False if git isn't available — a run
    is still recorded, just without the reproducibility marker."""
    try:
        commit_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True, timeout=5,
        ).stdout
        return commit_hash, bool(status.strip())
    except Exception:
        return None, False


def record_backtest_run(
    conn: duckdb.DuckDBPyConnection,
    symbols: list[str],
    days: int,
    start: datetime,
    end: datetime,
    interval_lower: str,
    interval_higher: str,
    fee_rate: float,
    warmup: int,
    strategy_metrics: dict,
    bh_metrics: dict,
    strategy_name: str = "ema_rsi_macd",
) -> str:
    """Insert one row into fact_backtest_run. Returns the new run_id.

    Reads bot.* / config.* constants through the module object, not via a
    top-level `from bot import X` — those bind once at import time and go
    stale the moment anything (e.g. a parameter sweep) monkeypatches
    bot.ATR_SL_MULTIPLE for a run. Reading through the module reflects
    whatever value was actually in effect for *this* run.
    """
    run_id = uuid.uuid4().hex
    commit_hash, is_dirty = _git_info()

    row = pd.DataFrame([{
        "run_id": run_id,
        "run_ts": datetime.now(timezone.utc),
        "strategy_name": strategy_name,
        "commit_hash": commit_hash,
        "is_dirty": is_dirty,
        "symbols": list(symbols),
        "days": days,
        "start_ts": start,
        "end_ts": end,
        "interval_lower": interval_lower,
        "interval_higher": interval_higher,
        "account_size": config.ACCOUNT_SIZE,
        "fee_rate": fee_rate,
        "risk_per_trade": config.RISK_PER_TRADE,
        "min_rr": config.MIN_RR,
        "warmup": warmup,
        "ema_fast": bot.EMA_FAST,
        "ema_slow": bot.EMA_SLOW,
        "rsi_period": bot.RSI_PERIOD,
        "rsi_bullish_low": bot.RSI_BULLISH_BAND[0],
        "rsi_bullish_high": bot.RSI_BULLISH_BAND[1],
        "rsi_bearish_low": bot.RSI_BEARISH_BAND[0],
        "rsi_bearish_high": bot.RSI_BEARISH_BAND[1],
        "macd_fast": bot.MACD_FAST,
        "macd_slow": bot.MACD_SLOW,
        "macd_signal_period": bot.MACD_SIGNAL_PERIOD,
        "atr_period": bot.ATR_PERIOD,
        "atr_sl_multiple": bot.ATR_SL_MULTIPLE,
        "atr_tp_multiple": bot.ATR_TP_MULTIPLE,
        "ema20_distance_threshold": bot.EMA20_DISTANCE_THRESHOLD,
        "volume_ma_window": bot.VOLUME_MA_WINDOW,
        "trades_count": strategy_metrics.get("trades_count"),
        "win_rate_pct": strategy_metrics.get("win_rate_pct"),
        "profit_factor": strategy_metrics.get("profit_factor"),
        "total_fees": strategy_metrics.get("total_fees"),
        "strategy_return_pct": strategy_metrics.get("return_pct"),
        "strategy_max_drawdown_pct": strategy_metrics.get("max_drawdown_pct"),
        "strategy_return_to_dd_ratio": strategy_metrics.get("return_to_dd_ratio"),
        "strategy_bullish_phase_return_pct": strategy_metrics.get("bullish_phase_return_pct"),
        "strategy_bearish_phase_return_pct": strategy_metrics.get("bearish_phase_return_pct"),
        "bh_return_pct": bh_metrics.get("return_pct"),
        "bh_max_drawdown_pct": bh_metrics.get("max_drawdown_pct"),
        "bh_return_to_dd_ratio": bh_metrics.get("return_to_dd_ratio"),
        "bh_bullish_phase_return_pct": bh_metrics.get("bullish_phase_return_pct"),
        "bh_bearish_phase_return_pct": bh_metrics.get("bearish_phase_return_pct"),
        "bh_neutral_phase_return_pct": bh_metrics.get("neutral_phase_return_pct"),
    }])

    columns = list(row.columns)
    conn.register("_backtest_run_batch", row)
    conn.execute(
        f"INSERT INTO fact_backtest_run ({', '.join(columns)}) "
        f"SELECT {', '.join(columns)} FROM _backtest_run_batch"
    )
    conn.unregister("_backtest_run_batch")

    log.info(
        "Recorded backtest run %s (commit %s%s)",
        run_id, commit_hash or "unknown", ", dirty tree" if is_dirty else "",
    )
    return run_id


def record_backtest_trades(conn: duckdb.DuckDBPyConnection, run_id: str, trades: list[dict]) -> int:
    """Insert one row per closed trade for `run_id`. Open (unclosed) trades
    aren't recorded — a run's trade history is only meaningful once
    resolved. Returns the number of rows written."""
    closed = [t for t in trades if t.get("pnl") is not None]
    if not closed:
        return 0

    rows = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "trade_seq": i,
                "symbol": t["symbol"],
                "side": t["side"],
                "entry_time": t["entry_time"],
                "exit_time": t["exit_time"],
                "entry": t["entry"],
                "exit_price": t["exit_price"],
                "stop_loss": t["sl"],
                "take_profit": t["tp"],
                "size": t["size"],
                "exit_reason": t["exit_reason"],
                "fee": t.get("fee"),
                "pnl": t["pnl"],
            }
            for i, t in enumerate(closed)
        ],
        columns=_TRADE_COLUMNS,
    )

    conn.register("_backtest_trade_batch", rows)
    conn.execute(
        f"INSERT INTO fact_backtest_trade ({', '.join(_TRADE_COLUMNS)}) "
        f"SELECT {', '.join(_TRADE_COLUMNS)} FROM _backtest_trade_batch"
    )
    conn.unregister("_backtest_trade_batch")

    log.info("Recorded %d trades for run %s", len(rows), run_id)
    return len(rows)
