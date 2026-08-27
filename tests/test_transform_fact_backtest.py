from datetime import datetime, timezone
from unittest.mock import MagicMock

import duckdb
import pytest

from transform.dims import populate_all_dims
from transform.fact_backtest import _git_info, record_backtest_run, record_backtest_trades
from transform.schema import create_schema


def _conn():
    conn = duckdb.connect(":memory:")
    conn.execute("SET TIMEZONE = 'UTC'")
    create_schema(conn)
    populate_all_dims(conn)
    return conn


_STRATEGY_METRICS = {
    "return_pct": 12.3, "max_drawdown_pct": 4.5, "return_to_dd_ratio": 2.73,
    "trades_count": 10, "win_rate_pct": 40.0, "profit_factor": 1.1, "total_fees": 20.0,
    "bullish_phase_return_pct": 3.0, "bearish_phase_return_pct": 9.3,
}
_BH_METRICS = {
    "return_pct": -5.0, "max_drawdown_pct": 30.0, "return_to_dd_ratio": -0.17,
    "bullish_phase_return_pct": 100.0, "bearish_phase_return_pct": -80.0, "neutral_phase_return_pct": 1.0,
}


class TestGitInfo:
    def test_returns_a_commit_hash_and_dirty_flag_inside_a_real_repo(self):
        # This test suite runs inside the project's own git repo — no need
        # to mock subprocess to prove the happy path works.
        commit_hash, is_dirty = _git_info()

        assert commit_hash is not None
        assert len(commit_hash) == 40  # full SHA-1 hex
        assert isinstance(is_dirty, bool)

    def test_returns_none_and_false_when_git_unavailable(self, monkeypatch):
        def raise_error(*a, **k):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr("transform.fact_backtest.subprocess.run", raise_error)

        commit_hash, is_dirty = _git_info()

        assert commit_hash is None
        assert is_dirty is False


class TestRecordBacktestRun:
    def test_inserts_one_row_with_requested_parameters(self):
        conn = _conn()
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 6, 1, tzinfo=timezone.utc)

        run_id = record_backtest_run(
            conn, ["BTCUSDT", "ETHUSDT"], 150, start, end, "1h", "4h", 0.001, 100,
            _STRATEGY_METRICS, _BH_METRICS,
        )

        row = conn.execute(
            "SELECT symbols, days, interval_lower, interval_higher, fee_rate, warmup "
            "FROM fact_backtest_run WHERE run_id = ?", [run_id],
        ).fetchone()
        assert row == (["BTCUSDT", "ETHUSDT"], 150, "1h", "4h", 0.001, 100)

    def test_run_id_is_a_fresh_uuid_each_call(self):
        conn = _conn()
        start, end = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 6, 1, tzinfo=timezone.utc)

        run_id_a = record_backtest_run(
            conn, ["BTCUSDT"], 150, start, end, "1h", "4h", 0.001, 100, _STRATEGY_METRICS, _BH_METRICS,
        )
        run_id_b = record_backtest_run(
            conn, ["BTCUSDT"], 150, start, end, "1h", "4h", 0.001, 100, _STRATEGY_METRICS, _BH_METRICS,
        )

        assert run_id_a != run_id_b
        count = conn.execute("SELECT COUNT(*) FROM fact_backtest_run").fetchone()[0]
        assert count == 2

    def test_strategy_thresholds_come_from_bot_py_constants_not_a_copy(self):
        from bot import ATR_SL_MULTIPLE, ATR_TP_MULTIPLE, EMA_FAST, EMA_SLOW, RSI_BULLISH_BAND

        conn = _conn()
        start, end = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 6, 1, tzinfo=timezone.utc)

        run_id = record_backtest_run(
            conn, ["BTCUSDT"], 150, start, end, "1h", "4h", 0.001, 100, _STRATEGY_METRICS, _BH_METRICS,
        )

        row = conn.execute(
            "SELECT ema_fast, ema_slow, rsi_bullish_low, atr_sl_multiple, atr_tp_multiple "
            "FROM fact_backtest_run WHERE run_id = ?", [run_id],
        ).fetchone()
        assert row == (EMA_FAST, EMA_SLOW, RSI_BULLISH_BAND[0], ATR_SL_MULTIPLE, ATR_TP_MULTIPLE)

    def test_reflects_a_monkeypatched_threshold_not_the_stale_import(self, monkeypatch):
        # Regression: reading via `from bot import ATR_SL_MULTIPLE` binds
        # once at import time and would still show the original value here
        # even though the run actually used 2.75 — exactly what a
        # parameter sweep needs to not misrecord.
        import bot
        monkeypatch.setattr(bot, "ATR_SL_MULTIPLE", 2.75)
        conn = _conn()
        start, end = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 6, 1, tzinfo=timezone.utc)

        run_id = record_backtest_run(
            conn, ["BTCUSDT"], 150, start, end, "1h", "4h", 0.001, 100, _STRATEGY_METRICS, _BH_METRICS,
        )

        stored = conn.execute("SELECT atr_sl_multiple FROM fact_backtest_run WHERE run_id = ?", [run_id]).fetchone()[0]
        assert stored == 2.75

    def test_stores_strategy_and_benchmark_metrics_side_by_side(self):
        conn = _conn()
        start, end = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 6, 1, tzinfo=timezone.utc)

        run_id = record_backtest_run(
            conn, ["BTCUSDT"], 150, start, end, "1h", "4h", 0.001, 100, _STRATEGY_METRICS, _BH_METRICS,
        )

        row = conn.execute(
            "SELECT strategy_return_pct, bh_return_pct, strategy_max_drawdown_pct, bh_max_drawdown_pct "
            "FROM fact_backtest_run WHERE run_id = ?", [run_id],
        ).fetchone()
        assert row == (12.3, -5.0, 4.5, 30.0)

    def test_strategy_name_defaults_to_ema_rsi_macd(self):
        conn = _conn()
        start, end = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 6, 1, tzinfo=timezone.utc)

        run_id = record_backtest_run(
            conn, ["BTCUSDT"], 150, start, end, "1h", "4h", 0.001, 100, _STRATEGY_METRICS, _BH_METRICS,
        )

        stored = conn.execute("SELECT strategy_name FROM fact_backtest_run WHERE run_id = ?", [run_id]).fetchone()[0]
        assert stored == "ema_rsi_macd"

    def test_strategy_name_can_be_overridden(self):
        conn = _conn()
        start, end = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 6, 1, tzinfo=timezone.utc)

        run_id = record_backtest_run(
            conn, ["BTCUSDT"], 150, start, end, "1h", "4h", 0.001, 100, _STRATEGY_METRICS, _BH_METRICS,
            strategy_name="random",
        )

        stored = conn.execute("SELECT strategy_name FROM fact_backtest_run WHERE run_id = ?", [run_id]).fetchone()[0]
        assert stored == "random"

    def test_records_git_commit_hash_and_dirty_flag(self):
        conn = _conn()
        start, end = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 6, 1, tzinfo=timezone.utc)

        run_id = record_backtest_run(
            conn, ["BTCUSDT"], 150, start, end, "1h", "4h", 0.001, 100, _STRATEGY_METRICS, _BH_METRICS,
        )

        commit_hash, is_dirty = conn.execute(
            "SELECT commit_hash, is_dirty FROM fact_backtest_run WHERE run_id = ?", [run_id]
        ).fetchone()
        assert commit_hash is not None
        assert isinstance(is_dirty, bool)


class TestRecordBacktestTrades:
    def _trade(self, symbol="BTCUSDT", side="BUY", pnl=10.0):
        return {
            "symbol": symbol, "side": side,
            "entry_time": datetime(2024, 1, 1, tzinfo=timezone.utc),
            "exit_time": datetime(2024, 1, 2, tzinfo=timezone.utc),
            "entry": 100.0, "exit_price": 110.0, "sl": 95.0, "tp": 115.0,
            "size": 1.0, "exit_reason": "TP", "fee": 0.2, "pnl": pnl,
        }

    def test_inserts_one_row_per_closed_trade(self):
        conn = _conn()
        run_id = record_backtest_run(
            conn, ["BTCUSDT"], 150,
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 6, 1, tzinfo=timezone.utc),
            "1h", "4h", 0.001, 100, _STRATEGY_METRICS, _BH_METRICS,
        )
        trades = [self._trade(pnl=10.0), self._trade(pnl=-5.0)]

        count = record_backtest_trades(conn, run_id, trades)

        assert count == 2
        rows = conn.execute("SELECT pnl FROM fact_backtest_trade WHERE run_id = ? ORDER BY trade_seq", [run_id]).fetchall()
        assert rows == [(10.0,), (-5.0,)]

    def test_open_trades_are_excluded(self):
        conn = _conn()
        run_id = record_backtest_run(
            conn, ["BTCUSDT"], 150,
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 6, 1, tzinfo=timezone.utc),
            "1h", "4h", 0.001, 100, _STRATEGY_METRICS, _BH_METRICS,
        )
        open_trade = self._trade()
        open_trade["pnl"] = None
        trades = [self._trade(pnl=10.0), open_trade]

        count = record_backtest_trades(conn, run_id, trades)

        assert count == 1

    def test_empty_trades_returns_zero_without_error(self):
        conn = _conn()
        run_id = record_backtest_run(
            conn, ["BTCUSDT"], 150,
            datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 6, 1, tzinfo=timezone.utc),
            "1h", "4h", 0.001, 100, _STRATEGY_METRICS, _BH_METRICS,
        )

        count = record_backtest_trades(conn, run_id, [])

        assert count == 0
