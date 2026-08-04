import numpy as np
import pandas as pd
import pytest

from bot import (
    add_indicators,
    calculate_atr,
    calculate_macd,
    calculate_position_size,
    calculate_rsi,
    calculate_score,
    create_trade_plan,
    generate_entry_signal,
    get_trend_4h,
    is_trade_worth_it,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ohlcv(prices, highs=None, lows=None, volumes=None):
    """Minimal OHLCV DataFrame from a price list."""
    n = len(prices)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    h = highs or [p * 1.01 for p in prices]
    lo = lows or [p * 0.99 for p in prices]
    v = volumes or [1000.0] * n
    return pd.DataFrame(
        {"Open": prices, "High": h, "Low": lo, "Close": prices, "Volume": v},
        index=idx,
    )


def _indicator_row(**overrides):
    """Single-row DataFrame with pre-filled indicator columns for strategy tests."""
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


# ---------------------------------------------------------------------------
# calculate_rsi
# ---------------------------------------------------------------------------

class TestCalculateRsi:
    def test_all_gains_gives_rsi_100(self):
        prices = pd.Series([float(i) for i in range(1, 51)])
        rsi = calculate_rsi(prices, period=14)
        # avg_loss = 0 → RS = inf → RSI = 100
        assert rsi.dropna().iloc[-1] >= 99.9

    def test_all_losses_gives_rsi_0(self):
        prices = pd.Series([float(50 - i) for i in range(50)])
        rsi = calculate_rsi(prices, period=14)
        # avg_gain = 0 → RS = 0 → RSI = 0
        assert rsi.dropna().iloc[-1] <= 0.1

    def test_insufficient_data_all_nan(self):
        # 5 values with period=14 → EWM min_periods not satisfied
        rsi = calculate_rsi(pd.Series([100.0, 101.0, 99.0, 102.0, 98.0]), period=14)
        assert rsi.isna().all()

    def test_output_length_matches_input(self):
        prices = pd.Series(np.arange(30, dtype=float))
        assert len(calculate_rsi(prices, period=14)) == 30

    def test_values_bounded_0_to_100(self):
        prices = pd.Series([100.0 + (-1) ** i * 2 for i in range(60)])
        valid = calculate_rsi(prices, period=14).dropna()
        assert (valid >= 0).all() and (valid <= 100).all()

    def test_alternating_prices_near_50(self):
        prices = pd.Series([100.0 + (-1) ** i for i in range(60)])
        rsi = calculate_rsi(prices, period=14).dropna().iloc[-1]
        assert 40 < rsi < 60


# ---------------------------------------------------------------------------
# calculate_macd
# ---------------------------------------------------------------------------

class TestCalculateMacd:
    def test_returns_three_series(self):
        prices = pd.Series(np.arange(1, 51, dtype=float))
        macd, signal, hist = calculate_macd(prices)
        assert len(macd) == len(signal) == len(hist) == 50

    def test_hist_equals_macd_minus_signal(self):
        prices = pd.Series(np.arange(1, 51, dtype=float))
        macd, signal, hist = calculate_macd(prices)
        pd.testing.assert_series_equal(hist, macd - signal, check_names=False)

    def test_rising_prices_positive_macd(self):
        prices = pd.Series([float(i) for i in range(1, 100)])
        macd, _, _ = calculate_macd(prices)
        # Fast EMA > Slow EMA for rising series → MACD > 0
        assert macd.iloc[-1] > 0

    def test_falling_prices_negative_macd(self):
        prices = pd.Series([float(100 - i) for i in range(100)])
        macd, _, _ = calculate_macd(prices)
        assert macd.iloc[-1] < 0

    def test_constant_prices_macd_near_zero(self):
        prices = pd.Series([100.0] * 60)
        macd, signal, hist = calculate_macd(prices)
        assert abs(macd.iloc[-1]) < 1e-9
        assert abs(signal.iloc[-1]) < 1e-9


# ---------------------------------------------------------------------------
# calculate_atr
# ---------------------------------------------------------------------------

class TestCalculateAtr:
    def test_constant_bars_atr_equals_range(self):
        # High=10, Low=8, Close=9 every bar → TR = max(2, 1, 1) = 2 → ATR = 2
        df = pd.DataFrame({
            "High": [10.0] * 20,
            "Low": [8.0] * 20,
            "Close": [9.0] * 20,
        })
        atr = calculate_atr(df, period=14)
        assert atr.dropna().iloc[-1] == pytest.approx(2.0)

    def test_period_1_equals_true_range(self):
        df = pd.DataFrame({
            "High": [110.0, 120.0],
            "Low": [90.0, 80.0],
            "Close": [100.0, 100.0],
        })
        # Row 1: high_low=40, high_close=20, low_close=20 → TR=40
        atr = calculate_atr(df, period=1)
        assert atr.iloc[-1] == pytest.approx(40.0)

    def test_output_length_matches_input(self):
        df = _ohlcv([100.0] * 20)
        assert len(calculate_atr(df, period=14)) == 20

    def test_insufficient_data_produces_nan(self):
        df = _ohlcv([100.0] * 5)
        atr = calculate_atr(df, period=14)
        assert atr.isna().all()


# ---------------------------------------------------------------------------
# add_indicators
# ---------------------------------------------------------------------------

class TestAddIndicators:
    EXPECTED_COLS = ["EMA_20", "EMA_50", "RSI", "MACD", "MACD_SIGNAL", "MACD_HIST", "ATR", "Volume_MA"]

    def test_all_columns_added(self):
        result = add_indicators(_ohlcv([100.0] * 60))
        for col in self.EXPECTED_COLS:
            assert col in result.columns, f"missing column: {col}"

    def test_original_not_mutated(self):
        df = _ohlcv([100.0] * 60)
        original_cols = set(df.columns)
        add_indicators(df)
        assert set(df.columns) == original_cols

    def test_ema_has_no_nan(self):
        # EWM with adjust=False produces a value from row 0
        result = add_indicators(_ohlcv([float(i + 100) for i in range(60)]))
        assert not result["EMA_20"].isna().any()
        assert not result["EMA_50"].isna().any()

    def test_ema_20_above_ema_50_in_strong_uptrend(self):
        # Prices strictly rising → EMA20 reacts faster, stays above EMA50
        result = add_indicators(_ohlcv([float(i) for i in range(1, 101)]))
        assert result["EMA_20"].iloc[-1] > result["EMA_50"].iloc[-1]


# ---------------------------------------------------------------------------
# get_trend_4h
# ---------------------------------------------------------------------------

class TestGetTrend4h:
    def test_bullish(self):
        df = _indicator_row(EMA_20=110.0, EMA_50=100.0, Close=115.0, MACD=1.0, MACD_SIGNAL=0.5)
        assert get_trend_4h(df) == "BULLISH"

    def test_bearish(self):
        df = _indicator_row(EMA_20=90.0, EMA_50=100.0, Close=85.0, MACD=-1.0, MACD_SIGNAL=-0.5)
        assert get_trend_4h(df) == "BEARISH"

    def test_neutral_flat_market(self):
        # EMA20 == EMA50 and MACD == SIGNAL → neither bullish nor bearish
        df = _indicator_row(EMA_20=100.0, EMA_50=100.0, Close=100.0, MACD=0.0, MACD_SIGNAL=0.0)
        assert get_trend_4h(df) == "NEUTRAL"

    def test_neutral_on_nan(self):
        df = _indicator_row(Close=float("nan"))
        assert get_trend_4h(df) == "NEUTRAL"

    def test_neutral_mixed_signals(self):
        # EMA alignment bullish but MACD bearish → no clear trend
        df = _indicator_row(EMA_20=110.0, EMA_50=100.0, Close=115.0, MACD=-0.5, MACD_SIGNAL=0.5)
        assert get_trend_4h(df) == "NEUTRAL"

    def test_neutral_price_below_ema20_despite_ema_alignment(self):
        # EMA20 > EMA50 but price below EMA20 → not BULLISH
        df = _indicator_row(EMA_20=110.0, EMA_50=100.0, Close=105.0, MACD=1.0, MACD_SIGNAL=0.5)
        assert get_trend_4h(df) == "NEUTRAL"


# ---------------------------------------------------------------------------
# generate_entry_signal
# ---------------------------------------------------------------------------

class TestGenerateEntrySignal:
    def _buy_row(self, **kw):
        defaults = dict(
            Close=102.0, EMA_20=100.0, EMA_50=95.0,
            RSI=55.0, MACD=1.0, MACD_SIGNAL=0.5,
            Volume=1100.0, Volume_MA=1000.0,
        )
        defaults.update(kw)
        return _indicator_row(**defaults)

    def _sell_row(self, **kw):
        defaults = dict(
            Close=93.0, EMA_20=95.0, EMA_50=100.0,
            RSI=45.0, MACD=-1.0, MACD_SIGNAL=-0.5,
            Volume=1100.0, Volume_MA=1000.0,
        )
        defaults.update(kw)
        return _indicator_row(**defaults)

    def test_buy_signal_all_conditions_met(self):
        assert generate_entry_signal(self._buy_row(), "BULLISH") == "BUY"

    def test_sell_signal_all_conditions_met(self):
        assert generate_entry_signal(self._sell_row(), "BEARISH") == "SELL"

    def test_hold_wrong_trend_for_buy(self):
        assert generate_entry_signal(self._buy_row(), "BEARISH") == "HOLD"

    def test_hold_wrong_trend_for_sell(self):
        assert generate_entry_signal(self._sell_row(), "BULLISH") == "HOLD"

    def test_hold_neutral_trend(self):
        assert generate_entry_signal(self._buy_row(), "NEUTRAL") == "HOLD"

    def test_not_enough_data_on_nan_rsi(self):
        assert generate_entry_signal(_indicator_row(RSI=float("nan")), "BULLISH") == "NOT ENOUGH DATA"

    def test_not_enough_data_on_nan_volume_ma(self):
        assert generate_entry_signal(_indicator_row(Volume_MA=float("nan")), "BULLISH") == "NOT ENOUGH DATA"

    def test_hold_volume_below_ma(self):
        assert generate_entry_signal(self._buy_row(Volume=900.0, Volume_MA=1000.0), "BULLISH") == "HOLD"

    def test_hold_rsi_too_high_for_buy(self):
        # RSI > 70 → overbought, no BUY
        assert generate_entry_signal(self._buy_row(RSI=72.0), "BULLISH") == "HOLD"

    def test_hold_rsi_too_low_for_buy(self):
        # RSI < 40 → not in the 40-70 window
        assert generate_entry_signal(self._buy_row(RSI=38.0), "BULLISH") == "HOLD"

    def test_hold_rsi_too_low_for_sell(self):
        # RSI < 30 → not in the 30-60 window for SELL
        assert generate_entry_signal(self._sell_row(RSI=28.0), "BEARISH") == "HOLD"

    def test_hold_price_too_far_above_ema20_for_buy(self):
        # distance = (105 - 100) / 100 = 0.05 > 0.03 → extended, no entry
        assert generate_entry_signal(self._buy_row(Close=105.0, EMA_20=100.0), "BULLISH") == "HOLD"

    def test_hold_price_too_far_below_ema20_for_sell(self):
        # distance = (90 - 95) / 95 ≈ -0.053 < -0.03 → extended, no entry
        assert generate_entry_signal(self._sell_row(Close=90.0, EMA_20=95.0), "BEARISH") == "HOLD"


# ---------------------------------------------------------------------------
# create_trade_plan
# ---------------------------------------------------------------------------

class TestCreateTradePlan:
    def test_buy_plan_levels(self):
        # entry=100, ATR=2 → SL=97, TP=106, risk=3, reward=6, RR=2.0
        plan = create_trade_plan(_indicator_row(Close=100.0, ATR=2.0), "BUY")
        assert plan["entry"] == pytest.approx(100.0)
        assert plan["stop_loss"] == pytest.approx(97.0)
        assert plan["take_profit"] == pytest.approx(106.0)
        assert plan["rr_ratio"] == pytest.approx(2.0)

    def test_sell_plan_levels(self):
        # entry=100, ATR=2 → SL=103, TP=94, risk=3, reward=6, RR=2.0
        plan = create_trade_plan(_indicator_row(Close=100.0, ATR=2.0), "SELL")
        assert plan["entry"] == pytest.approx(100.0)
        assert plan["stop_loss"] == pytest.approx(103.0)
        assert plan["take_profit"] == pytest.approx(94.0)
        assert plan["rr_ratio"] == pytest.approx(2.0)

    def test_risk_and_reward_populated(self):
        plan = create_trade_plan(_indicator_row(Close=100.0, ATR=4.0), "BUY")
        assert plan["risk"] == pytest.approx(6.0)    # 1.5 * 4
        assert plan["reward"] == pytest.approx(12.0) # 3.0 * 4

    def test_hold_returns_empty_dict(self):
        assert create_trade_plan(_indicator_row(Close=100.0, ATR=2.0), "HOLD") == {}

    def test_nan_entry_returns_empty_dict(self):
        assert create_trade_plan(_indicator_row(Close=float("nan"), ATR=2.0), "BUY") == {}

    def test_nan_atr_returns_empty_dict(self):
        assert create_trade_plan(_indicator_row(Close=100.0, ATR=float("nan")), "BUY") == {}


# ---------------------------------------------------------------------------
# is_trade_worth_it
# ---------------------------------------------------------------------------

class TestIsTradeWorthIt:
    def test_good_rr_accepted(self):
        assert is_trade_worth_it({"rr_ratio": 2.5}) is True

    def test_exact_minimum_rr_accepted(self):
        assert is_trade_worth_it({"rr_ratio": 2.0}) is True

    def test_below_minimum_rr_rejected(self):
        assert is_trade_worth_it({"rr_ratio": 1.9}) is False

    def test_empty_plan_rejected(self):
        assert is_trade_worth_it({}) is False

    def test_custom_min_rr(self):
        assert is_trade_worth_it({"rr_ratio": 3.0}, min_rr=3.0) is True
        assert is_trade_worth_it({"rr_ratio": 2.9}, min_rr=3.0) is False


# ---------------------------------------------------------------------------
# calculate_position_size
# ---------------------------------------------------------------------------

class TestCalculatePositionSize:
    def test_known_values(self):
        # risk_amount = 1000 * 0.01 = 10; risk_per_unit = |100 - 90| = 10 → size = 1.0
        assert calculate_position_size(1000.0, 0.01, 100.0, 90.0) == pytest.approx(1.0)

    def test_zero_distance_returns_zero(self):
        assert calculate_position_size(1000.0, 0.01, 100.0, 100.0) == 0

    def test_proportional_to_risk_percent(self):
        s1 = calculate_position_size(1000.0, 0.01, 100.0, 90.0)
        s2 = calculate_position_size(1000.0, 0.02, 100.0, 90.0)
        assert s2 == pytest.approx(s1 * 2)

    def test_proportional_to_account_size(self):
        s1 = calculate_position_size(1000.0, 0.01, 100.0, 90.0)
        s2 = calculate_position_size(2000.0, 0.01, 100.0, 90.0)
        assert s2 == pytest.approx(s1 * 2)

    def test_sell_stop_above_entry(self):
        # For a short, SL is above entry — abs() ensures same result
        s_buy = calculate_position_size(1000.0, 0.01, 100.0, 90.0)
        s_sell = calculate_position_size(1000.0, 0.01, 100.0, 110.0)
        assert s_buy == pytest.approx(s_sell)


# ---------------------------------------------------------------------------
# calculate_score
# ---------------------------------------------------------------------------

def _score_row(**overrides):
    row = {
        "Close": 100.0, "EMA_20": 105.0, "EMA_50": 100.0,
        "MACD_HIST": 0.5, "RSI": 55.0, "ATR": 1.0,
    }
    row.update(overrides)
    return pd.DataFrame([row], index=pd.date_range("2024-01-01", periods=1, freq="1h"))


class TestCalculateScore:
    def test_valid_input_returns_finite_float(self):
        score = calculate_score(_score_row(), _score_row())
        assert np.isfinite(score)

    def test_nan_in_4h_returns_sentinel(self):
        assert calculate_score(_score_row(Close=float("nan")), _score_row()) == -999

    def test_nan_in_1h_returns_sentinel(self):
        assert calculate_score(_score_row(), _score_row(RSI=float("nan"))) == -999

    def test_wider_ema_spread_gives_higher_score(self):
        strong = _score_row(EMA_20=120.0, EMA_50=100.0)
        weak = _score_row(EMA_20=101.0, EMA_50=100.0)
        assert calculate_score(strong, _score_row()) > calculate_score(weak, _score_row())

    def test_higher_volatility_lowers_score(self):
        low_vol = _score_row(ATR=1.0)
        high_vol = _score_row(ATR=10.0)
        assert calculate_score(_score_row(), low_vol) > calculate_score(_score_row(), high_vol)
