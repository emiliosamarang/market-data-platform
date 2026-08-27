import bot
import config
from strategies.base import Strategy


class EmaRsiMacdStrategy(Strategy):
    """The original bot.py strategy, unchanged, behind the Strategy
    interface — a pure wrapper, not a reimplementation. Calls bot.py's own
    functions so it always reflects bot.py's current constants, including
    ones monkeypatched at runtime (e.g. scripts/parameter_sweep.py), the
    same way run_backtest's old inline logic did.
    """

    name = "ema_rsi_macd"

    def decide(self, df_4h_slice, df_1h_slice) -> dict | None:
        if len(df_4h_slice) < 50:
            return None
        higher_trend = bot.get_trend_4h(df_4h_slice)
        signal = bot.generate_entry_signal(df_1h_slice, higher_trend)
        if signal not in ("BUY", "SELL"):
            return None
        trade_plan = bot.create_trade_plan(df_1h_slice, signal)
        if not bot.is_trade_worth_it(trade_plan, min_rr=config.MIN_RR):
            return None
        return {
            "side": signal,
            "stop_loss": trade_plan["stop_loss"],
            "take_profit": trade_plan["take_profit"],
        }
