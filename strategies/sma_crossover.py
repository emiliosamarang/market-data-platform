import bot
from strategies.base import Strategy


class SmaCrossoverStrategy(Strategy):
    """The other end of the complexity question RandomStrategy raised: not
    "no entry logic at all" but "the simplest entry logic that isn't
    random" — does EmaRsiMacdStrategy's combination of trend filter, RR
    gate, and EMA/RSI/MACD confluence beat a single moving average, or is
    roughly the same edge available from one line of arithmetic? Same
    ATR-based stop as the other strategies (via bot.create_trade_plan) —
    isolates entry-rule complexity, not risk management.

    Entry: Close crosses the SMA — BUY on an upward cross, SELL on a
    downward cross. Self-contained (a plain rolling mean over Close, not
    one of bot.py's EMA columns) since this is a deliberately independent
    baseline, not a variant of the real strategy's own indicators.
    """

    name = "sma_crossover"

    SMA_PERIOD = 50

    def decide(self, df_4h_slice, df_1h_slice) -> dict | None:
        closes = df_1h_slice["Close"]
        if len(closes) < self.SMA_PERIOD + 1:
            return None

        curr_sma = closes.iloc[-self.SMA_PERIOD:].mean()
        prev_sma = closes.iloc[-self.SMA_PERIOD - 1 : -1].mean()
        curr_close = closes.iloc[-1]
        prev_close = closes.iloc[-2]

        crossed_up = prev_close <= prev_sma and curr_close > curr_sma
        crossed_down = prev_close >= prev_sma and curr_close < curr_sma

        if crossed_up:
            side = "BUY"
        elif crossed_down:
            side = "SELL"
        else:
            return None

        trade_plan = bot.create_trade_plan(df_1h_slice, side)
        if not trade_plan:
            return None
        return {
            "side": side,
            "stop_loss": trade_plan["stop_loss"],
            "take_profit": trade_plan["take_profit"],
        }
