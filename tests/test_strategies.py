import pandas as pd
import pytest

import bot
import config
from strategies.base import Strategy
from strategies.ema_rsi_macd import EmaRsiMacdStrategy
from strategies.random_strategy import RandomStrategy
from strategies.sma_crossover import SmaCrossoverStrategy


# ---------------------------------------------------------------------------
# Helpers — same shape as tests/test_bot.py's _indicator_row: a single-row
# DataFrame with pre-filled indicator columns, since get_trend_4h /
# generate_entry_signal / create_trade_plan only ever read .iloc[-1].
# ---------------------------------------------------------------------------

def _indicator_row(**overrides):
    row = {
        "Open": 100.0, "High": 101.0, "Low": 99.0,
        "Close": 100.0, "Volume": 1100.0,
        "EMA_20": 100.0, "EMA_50": 100.0,
        "RSI": 50.0,
        "MACD": 0.0, "MACD_SIGNAL": 0.0, "MACD_HIST": 0.0,
        "ATR": 1.0, "Volume_MA": 1000.0,
    }
    row.update(overrides)
    return pd.DataFrame([row], index=pd.date_range("2024-01-01", periods=1, freq="1h"))


def _buy_row(**kw):
    defaults = dict(
        Close=102.0, EMA_20=100.0, EMA_50=95.0,
        RSI=55.0, MACD=1.0, MACD_SIGNAL=0.5,
        Volume=1100.0, Volume_MA=1000.0, ATR=1.0,
    )
    defaults.update(kw)
    return _indicator_row(**defaults)


def _bullish_4h(rows=50, **kw):
    """A 4h slice with `rows` rows so the >=50 length gate passes; only the
    last row's values matter to get_trend_4h."""
    defaults = dict(EMA_20=110.0, EMA_50=100.0, Close=115.0, MACD=1.0, MACD_SIGNAL=0.5)
    defaults.update(kw)
    row = {
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Volume": 1100.0,
        "RSI": 50.0, "MACD_HIST": 0.0, "ATR": 1.0, "Volume_MA": 1000.0,
    }
    row.update(defaults)
    return pd.DataFrame([row] * rows, index=pd.date_range("2024-01-01", periods=rows, freq="4h"))


# ---------------------------------------------------------------------------
# Strategy (ABC)
# ---------------------------------------------------------------------------

class TestStrategyBase:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            Strategy()

    def test_prepare_defaults_to_noop(self):
        class _Minimal(Strategy):
            name = "minimal"

            def decide(self, df_4h_slice, df_1h_slice):
                return None

        # Must not raise, and must not require overriding.
        _Minimal().prepare("BTCUSDT", pd.DataFrame(), pd.DataFrame())


# ---------------------------------------------------------------------------
# EmaRsiMacdStrategy — a pure wrapper around bot.py's own functions
# ---------------------------------------------------------------------------

class TestEmaRsiMacdStrategy:
    def test_name(self):
        assert EmaRsiMacdStrategy().name == "ema_rsi_macd"

    def test_returns_none_when_4h_slice_too_short(self):
        strategy = EmaRsiMacdStrategy()
        short_4h = _bullish_4h(rows=49)
        assert strategy.decide(short_4h, _buy_row()) is None

    def test_returns_none_on_wrong_trend(self):
        strategy = EmaRsiMacdStrategy()
        bearish_4h = _bullish_4h(EMA_20=90.0, EMA_50=100.0, Close=85.0, MACD=-1.0, MACD_SIGNAL=-0.5)
        assert strategy.decide(bearish_4h, _buy_row()) is None

    def test_buy_decision_matches_bot_create_trade_plan(self):
        strategy = EmaRsiMacdStrategy()
        df_1h = _buy_row(ATR=2.0)
        decision = strategy.decide(_bullish_4h(), df_1h)

        expected_plan = bot.create_trade_plan(df_1h, "BUY")
        assert decision == {
            "side": "BUY",
            "stop_loss": expected_plan["stop_loss"],
            "take_profit": expected_plan["take_profit"],
        }

    def test_returns_none_when_rr_below_min(self, monkeypatch):
        # ATR_TP_MULTIPLE lowered → rr_ratio = ATR_TP_MULTIPLE / ATR_SL_MULTIPLE
        # drops below MIN_RR, so a signal that would otherwise pass is rejected.
        monkeypatch.setattr(bot, "ATR_TP_MULTIPLE", 1.0)
        strategy = EmaRsiMacdStrategy()
        assert strategy.decide(_bullish_4h(), _buy_row()) is None

    def test_reflects_monkeypatched_bot_constant_not_stale_import(self, monkeypatch):
        # Regression test for the staleness bug already fixed once in
        # transform/fact_backtest.py: decide() must read bot.ATR_SL_MULTIPLE
        # through the module at call time, not a value bound at import time.
        monkeypatch.setattr(bot, "ATR_SL_MULTIPLE", 3.0)  # RR = 3.0/3.0 = 1.0, still >= MIN_RR? no
        monkeypatch.setattr(bot, "ATR_TP_MULTIPLE", 9.0)  # RR = 9.0/3.0 = 3.0, passes
        strategy = EmaRsiMacdStrategy()
        df_1h = _buy_row(ATR=2.0)
        decision = strategy.decide(_bullish_4h(), df_1h)
        assert decision["stop_loss"] == pytest.approx(102.0 - 3.0 * 2.0)
        assert decision["take_profit"] == pytest.approx(102.0 + 9.0 * 2.0)

    def test_resolves_min_rr_from_config_at_call_time(self, monkeypatch):
        monkeypatch.setattr(config, "MIN_RR", 100.0)
        strategy = EmaRsiMacdStrategy()
        # Baseline plan's rr_ratio is exactly 2.0 (3.0/1.5) — would pass the
        # module's default MIN_RR but must fail this inflated one.
        assert strategy.decide(_bullish_4h(), _buy_row()) is None


# ---------------------------------------------------------------------------
# RandomStrategy — same stop mechanics, matched trade frequency, no filter
# ---------------------------------------------------------------------------

def _flat_1h(n=200, atr=2.0):
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame(
        {
            "Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n, "Close": [100.0] * n,
            "Volume": [1000.0] * n,
            "EMA_20": [100.0] * n, "EMA_50": [100.0] * n, "RSI": [50.0] * n,
            "MACD": [0.0] * n, "MACD_SIGNAL": [0.0] * n, "MACD_HIST": [0.0] * n,
            "ATR": [atr] * n, "Volume_MA": [1000.0] * n,
        },
        index=idx,
    )


class TestRandomStrategy:
    def test_name(self):
        assert RandomStrategy(target_trades=1, seed=1, warmup=0).name == "random"

    def test_samples_exactly_target_trades_unique_triggers(self):
        strategy = RandomStrategy(target_trades=5, seed=42, warmup=10)
        df_1h = _flat_1h(n=100)
        strategy.prepare("BTCUSDT", pd.DataFrame(), df_1h)
        assert len(strategy._triggers) == 5
        assert all(10 <= idx < len(df_1h) - 1 for idx in strategy._triggers)

    def test_caps_target_trades_at_available_population(self):
        strategy = RandomStrategy(target_trades=10_000, seed=1, warmup=90)
        df_1h = _flat_1h(n=100)  # population = range(90, 99) → 9 candidates
        strategy.prepare("BTCUSDT", pd.DataFrame(), df_1h)
        assert len(strategy._triggers) == 9

    def test_reproducible_with_same_seed(self):
        df_1h = _flat_1h(n=100)
        a = RandomStrategy(target_trades=5, seed=7, warmup=10)
        b = RandomStrategy(target_trades=5, seed=7, warmup=10)
        a.prepare("BTCUSDT", pd.DataFrame(), df_1h)
        b.prepare("BTCUSDT", pd.DataFrame(), df_1h)
        assert a._triggers == b._triggers

        decisions_a = [a.decide(pd.DataFrame(), df_1h.iloc[: i + 1]) for i in sorted(a._triggers)]
        decisions_b = [b.decide(pd.DataFrame(), df_1h.iloc[: i + 1]) for i in sorted(b._triggers)]
        assert decisions_a == decisions_b

    def test_different_seeds_produce_different_triggers(self):
        df_1h = _flat_1h(n=200)
        a = RandomStrategy(target_trades=5, seed=1, warmup=10)
        b = RandomStrategy(target_trades=5, seed=2, warmup=10)
        a.prepare("BTCUSDT", pd.DataFrame(), df_1h)
        b.prepare("BTCUSDT", pd.DataFrame(), df_1h)
        assert a._triggers != b._triggers

    def test_returns_none_on_non_trigger_index(self):
        strategy = RandomStrategy(target_trades=1, seed=1, warmup=10)
        df_1h = _flat_1h(n=100)
        strategy.prepare("BTCUSDT", pd.DataFrame(), df_1h)
        non_trigger = next(i for i in range(10, 99) if i not in strategy._triggers)
        assert strategy.decide(pd.DataFrame(), df_1h.iloc[: non_trigger + 1]) is None

    def test_decision_on_trigger_matches_bot_create_trade_plan(self):
        strategy = RandomStrategy(target_trades=1, seed=1, warmup=10)
        df_1h = _flat_1h(n=100, atr=2.0)
        strategy.prepare("BTCUSDT", pd.DataFrame(), df_1h)
        trigger = next(iter(strategy._triggers))
        decision = strategy.decide(pd.DataFrame(), df_1h.iloc[: trigger + 1])

        assert decision is not None
        assert decision["side"] in ("BUY", "SELL")
        expected_plan = bot.create_trade_plan(df_1h.iloc[: trigger + 1], decision["side"])
        assert decision["stop_loss"] == pytest.approx(expected_plan["stop_loss"])
        assert decision["take_profit"] == pytest.approx(expected_plan["take_profit"])

    def test_no_trend_filter_or_rr_gate(self):
        # Deliberately no get_trend_4h / is_trade_worth_it involvement: an
        # empty df_4h_slice (which would crash EmaRsiMacdStrategy) must not
        # stop RandomStrategy from deciding, and a poor RR ratio must not be
        # rejected either.
        strategy = RandomStrategy(target_trades=1, seed=3, warmup=0)
        n = 5
        df_1h = _flat_1h(n=n, atr=50.0)  # huge ATR → poor rr_ratio, still not gated
        strategy.prepare("BTCUSDT", pd.DataFrame(), df_1h)
        trigger = next(iter(strategy._triggers))
        decision = strategy.decide(pd.DataFrame(), df_1h.iloc[: trigger + 1])
        assert decision is not None

    def test_none_when_atr_is_nan_at_trigger(self):
        strategy = RandomStrategy(target_trades=1, seed=1, warmup=0)
        df_1h = _flat_1h(n=5)
        df_1h.iloc[0, df_1h.columns.get_loc("ATR")] = float("nan")
        strategy.prepare("BTCUSDT", pd.DataFrame(), df_1h)
        strategy._triggers = {0}  # force the NaN-ATR row to be the trigger
        assert strategy.decide(pd.DataFrame(), df_1h.iloc[:1]) is None


# ---------------------------------------------------------------------------
# SmaCrossoverStrategy — the "simplest non-random rule" reference point
# ---------------------------------------------------------------------------

def _closes_1h(closes, atr=2.0):
    n = len(closes)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame(
        {
            "Open": closes, "High": [c * 1.01 for c in closes], "Low": [c * 0.99 for c in closes], "Close": closes,
            "Volume": [1000.0] * n,
            "EMA_20": closes, "EMA_50": closes, "RSI": [50.0] * n,
            "MACD": [0.0] * n, "MACD_SIGNAL": [0.0] * n, "MACD_HIST": [0.0] * n,
            "ATR": [atr] * n, "Volume_MA": [1000.0] * n,
        },
        index=idx,
    )


class TestSmaCrossoverStrategy:
    def test_name(self):
        assert SmaCrossoverStrategy().name == "sma_crossover"

    def test_returns_none_below_sma_period_plus_one_rows(self):
        strategy = SmaCrossoverStrategy()
        df_1h = _closes_1h([100.0] * strategy.SMA_PERIOD)  # exactly SMA_PERIOD, one short
        assert strategy.decide(pd.DataFrame(), df_1h) is None

    def test_returns_none_on_flat_price_no_cross(self):
        strategy = SmaCrossoverStrategy()
        df_1h = _closes_1h([100.0] * (strategy.SMA_PERIOD + 2))
        assert strategy.decide(pd.DataFrame(), df_1h) is None

    def test_buy_on_upward_cross_matches_bot_create_trade_plan(self):
        strategy = SmaCrossoverStrategy()
        closes = [100.0] * strategy.SMA_PERIOD + [95.0, 110.0]
        df_1h = _closes_1h(closes, atr=2.0)

        decision = strategy.decide(pd.DataFrame(), df_1h)

        assert decision is not None
        assert decision["side"] == "BUY"
        expected_plan = bot.create_trade_plan(df_1h, "BUY")
        assert decision["stop_loss"] == pytest.approx(expected_plan["stop_loss"])
        assert decision["take_profit"] == pytest.approx(expected_plan["take_profit"])

    def test_sell_on_downward_cross_matches_bot_create_trade_plan(self):
        strategy = SmaCrossoverStrategy()
        closes = [100.0] * strategy.SMA_PERIOD + [105.0, 90.0]
        df_1h = _closes_1h(closes, atr=2.0)

        decision = strategy.decide(pd.DataFrame(), df_1h)

        assert decision is not None
        assert decision["side"] == "SELL"
        expected_plan = bot.create_trade_plan(df_1h, "SELL")
        assert decision["stop_loss"] == pytest.approx(expected_plan["stop_loss"])
        assert decision["take_profit"] == pytest.approx(expected_plan["take_profit"])

    def test_returns_none_when_atr_is_nan(self):
        strategy = SmaCrossoverStrategy()
        closes = [100.0] * strategy.SMA_PERIOD + [95.0, 110.0]  # would otherwise be a BUY cross
        df_1h = _closes_1h(closes, atr=float("nan"))
        assert strategy.decide(pd.DataFrame(), df_1h) is None

    def test_no_trend_filter_or_rr_gate(self):
        # A poor RR ratio (huge ATR) must not block a real cross — SMA
        # crossover, like RandomStrategy, deliberately has no RR gate.
        strategy = SmaCrossoverStrategy()
        closes = [100.0] * strategy.SMA_PERIOD + [95.0, 110.0]
        df_1h = _closes_1h(closes, atr=50.0)
        assert strategy.decide(pd.DataFrame(), df_1h) is not None
