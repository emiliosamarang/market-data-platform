from abc import ABC, abstractmethod

import pandas as pd


class Strategy(ABC):
    """Common interface for backtest.py's run_backtest to drive any strategy
    without knowing its internals. Mirrors ingestion.base.MarketDataSource's
    pattern: a small ABC, real logic lives in the implementations.

    decide() is called once per 1h candle with growing, look-ahead-safe
    slices (df_1h.iloc[:i+1] and the 4h slice up to the same timestamp) —
    the same discipline run_backtest already applies to bot.py's functions.
    It must return None (no trade this candle) or a dict with the keys
    "side" ("BUY"/"SELL"), "stop_loss" and "take_profit" (absolute prices).
    Position sizing and entry-at-next-open stay in run_backtest — a
    strategy decides direction and risk, not sizing.
    """

    name: str

    def prepare(self, symbol: str, df_4h: pd.DataFrame, df_1h: pd.DataFrame) -> None:
        """Optional hook, called once before the backtest loop starts, with
        the full-window data already indicator-enriched. Default no-op —
        only strategies that need to precompute something (e.g. RandomStrategy
        sampling its trigger indices) need to override this."""

    @abstractmethod
    def decide(self, df_4h_slice: pd.DataFrame, df_1h_slice: pd.DataFrame) -> dict | None:
        raise NotImplementedError
