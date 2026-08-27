import random

import bot
from strategies.base import Strategy


class RandomStrategy(Strategy):
    """Baseline to answer: does the real strategy's signal timing add
    anything beyond its own stop-loss/take-profit mechanics? Same ATR-based
    stop plan as EmaRsiMacdStrategy (via bot.create_trade_plan — reused, not
    reimplemented), same number of trades as the real strategy produced for
    this symbol/window, but WHEN to enter and BUY-vs-SELL are drawn at
    random instead of from get_trend_4h/generate_entry_signal. Deliberately
    has no trend filter and no is_trade_worth_it gate — those are exactly
    the things being isolated out.

    One instance covers one symbol/window: target_trades is that symbol's
    real-strategy trade count for the window being compared against, not a
    global constant. warmup is a constructor param (not imported from
    backtest.py) to avoid backtest.py <-> strategies circular imports.
    """

    name = "random"

    def __init__(self, target_trades: int, seed: int, warmup: int):
        self.target_trades = target_trades
        self.seed = seed
        self.warmup = warmup
        self._triggers: set[int] = set()
        self._rng: random.Random | None = None

    def prepare(self, symbol: str, df_4h, df_1h) -> None:
        self._rng = random.Random(self.seed)
        population = range(self.warmup, len(df_1h) - 1)
        k = min(self.target_trades, len(population))
        self._triggers = set(self._rng.sample(population, k))

    def decide(self, df_4h_slice, df_1h_slice) -> dict | None:
        idx = len(df_1h_slice) - 1
        if idx not in self._triggers:
            return None
        side = self._rng.choice(["BUY", "SELL"])
        trade_plan = bot.create_trade_plan(df_1h_slice, side)
        if not trade_plan:
            return None
        return {
            "side": side,
            "stop_loss": trade_plan["stop_loss"],
            "take_profit": trade_plan["take_profit"],
        }
