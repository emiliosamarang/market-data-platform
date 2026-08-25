import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
from apscheduler.schedulers.blocking import BlockingScheduler

import exchange
import database
import trader
import notify
import sentiment
from config import (
    SYMBOLS, HIGHER_INTERVAL, LOWER_INTERVAL, KLINE_LIMIT,
    ACCOUNT_SIZE, RISK_PER_TRADE, MIN_RR, DAILY_LOSS_LIMIT,
    INGESTION_ASSET_CLASS, INGESTION_SOURCE, RAW_DATA_DIR,
    setup_logging,
)
from ingestion.base import to_ohlc_frame
from ingestion.binance_source import BinanceSource
from ingestion.raw_store import MissingDataError, RawStore
from ingestion.time_utils import interval_to_ms

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Strategy thresholds — named constants, not magic numbers, so a backtest
# run can record exactly what was used (see transform/fact_backtest.py)
# without a second, possibly-drifting copy of these values.
# ---------------------------------------------------------------------------

EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL_PERIOD = 9
ATR_PERIOD = 14
VOLUME_MA_WINDOW = 20

RSI_BULLISH_BAND = (40, 70)
RSI_BEARISH_BAND = (30, 60)
EMA20_DISTANCE_THRESHOLD = 0.03  # entry must be within +/-3% of EMA20

ATR_SL_MULTIPLE = 1.5
ATR_TP_MULTIPLE = 3.0


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def calculate_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_macd(series: pd.Series):
    ema_fast = series.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = series.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=MACD_SIGNAL_PERIOD, adjust=False).mean()
    hist = macd - signal
    return macd, signal, hist


def calculate_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(window=period).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["EMA_20"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["EMA_50"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["RSI"] = calculate_rsi(df["Close"], period=RSI_PERIOD)
    df["MACD"], df["MACD_SIGNAL"], df["MACD_HIST"] = calculate_macd(df["Close"])
    df["ATR"] = calculate_atr(df, period=ATR_PERIOD)
    df["Volume_MA"] = df["Volume"].rolling(window=VOLUME_MA_WINDOW).mean()
    return df


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

def get_trend_4h(df_4h: pd.DataFrame) -> str:
    latest = df_4h.iloc[-1]
    needed = [latest["Close"], latest["EMA_20"], latest["EMA_50"],
              latest["MACD"], latest["MACD_SIGNAL"]]
    if any(pd.isna(x) for x in needed):
        return "NEUTRAL"
    bullish = (latest["EMA_20"] > latest["EMA_50"]
               and latest["Close"] > latest["EMA_20"]
               and latest["MACD"] > latest["MACD_SIGNAL"])
    bearish = (latest["EMA_20"] < latest["EMA_50"]
               and latest["Close"] < latest["EMA_20"]
               and latest["MACD"] < latest["MACD_SIGNAL"])
    if bullish:
        return "BULLISH"
    if bearish:
        return "BEARISH"
    return "NEUTRAL"


def generate_entry_signal(df_1h: pd.DataFrame, higher_trend: str) -> str:
    latest = df_1h.iloc[-1]
    price = latest["Close"]
    ema_20 = latest["EMA_20"]
    ema_50 = latest["EMA_50"]
    rsi = latest["RSI"]
    macd = latest["MACD"]
    macd_signal = latest["MACD_SIGNAL"]

    volume = latest["Volume"]
    volume_ma = latest["Volume_MA"]

    if any(pd.isna(x) for x in [price, ema_20, ema_50, rsi, macd, macd_signal, volume_ma]):
        return "NOT ENOUGH DATA"

    distance_from_ema20 = (price - ema_20) / ema_20

    bullish_entry = (
        higher_trend == "BULLISH"
        and ema_20 > ema_50
        and price > ema_20
        and macd > macd_signal
        and RSI_BULLISH_BAND[0] < rsi < RSI_BULLISH_BAND[1]
        and distance_from_ema20 < EMA20_DISTANCE_THRESHOLD
        and volume > volume_ma
    )
    bearish_entry = (
        higher_trend == "BEARISH"
        and ema_20 < ema_50
        and price < ema_20
        and macd < macd_signal
        and RSI_BEARISH_BAND[0] < rsi < RSI_BEARISH_BAND[1]
        and distance_from_ema20 > -EMA20_DISTANCE_THRESHOLD
        and volume > volume_ma
    )

    if bullish_entry:
        return "BUY"
    if bearish_entry:
        return "SELL"
    return "HOLD"


def create_trade_plan(df_1h: pd.DataFrame, signal: str) -> dict:
    latest = df_1h.iloc[-1]
    entry = latest["Close"]
    atr = latest["ATR"]

    if pd.isna(entry) or pd.isna(atr):
        return {}

    if signal == "BUY":
        stop_loss = entry - (ATR_SL_MULTIPLE * atr)
        take_profit = entry + (ATR_TP_MULTIPLE * atr)
    elif signal == "SELL":
        stop_loss = entry + (ATR_SL_MULTIPLE * atr)
        take_profit = entry - (ATR_TP_MULTIPLE * atr)
    else:
        return {}

    risk = abs(entry - stop_loss)
    reward = abs(take_profit - entry)
    rr_ratio = reward / risk if risk != 0 else 0

    return {
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk": risk,
        "reward": reward,
        "rr_ratio": rr_ratio,
    }


def is_trade_worth_it(trade_plan: dict, min_rr: float = MIN_RR) -> bool:
    if not trade_plan:
        return False
    return trade_plan["rr_ratio"] >= min_rr


def calculate_position_size(
    account_size: float,
    risk_per_trade: float,
    entry: float,
    stop_loss: float,
) -> float:
    risk_amount = account_size * risk_per_trade
    risk_per_unit = abs(entry - stop_loss)
    if risk_per_unit == 0:
        return 0
    return risk_amount / risk_per_unit


def calculate_score(df_4h: pd.DataFrame, df_1h: pd.DataFrame) -> float:
    latest_4h = df_4h.iloc[-1]
    latest_1h = df_1h.iloc[-1]

    needed = [
        latest_4h["Close"], latest_4h["EMA_20"], latest_4h["EMA_50"], latest_4h["MACD_HIST"],
        latest_1h["Close"], latest_1h["EMA_20"], latest_1h["EMA_50"],
        latest_1h["RSI"], latest_1h["MACD_HIST"], latest_1h["ATR"],
    ]
    if any(pd.isna(x) for x in needed):
        return -999

    trend_score_4h = ((latest_4h["EMA_20"] - latest_4h["EMA_50"]) / latest_4h["Close"]) * 100
    trend_score_1h = ((latest_1h["EMA_20"] - latest_1h["EMA_50"]) / latest_1h["Close"]) * 100
    rsi_score = 1 - abs(latest_1h["RSI"] - 55) / 55
    momentum_score = (
        (latest_4h["MACD_HIST"] / latest_4h["Close"]) * 500
        + (latest_1h["MACD_HIST"] / latest_1h["Close"]) * 1000
    )
    volatility_penalty = (latest_1h["ATR"] / latest_1h["Close"]) * 10

    return trend_score_4h + trend_score_1h + rsi_score + momentum_score - volatility_penalty


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

_store = RawStore(base_dir=RAW_DATA_DIR)
_source = BinanceSource(client=exchange.client)

_INCREMENTAL_CANDLES = 10  # lookback window (in candles) for the pre-scan refresh


class DataUnavailable(Exception):
    """Fresh market data for a symbol/interval couldn't be obtained.

    Tells scan() to skip the symbol for this cycle rather than trade on
    stale cached data.
    """


def _load_recent(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    """Incrementally refresh the raw layer, then read the most recent
    `limit` candles from it.

    The scan always needs the freshest — possibly still-forming — candle,
    so this refreshes *before* reading, unlike backtest.py's
    read-then-refresh-on-miss (fine there with a slightly stale cache, wrong
    here). Only a small trailing window is re-fetched from Binance each
    call, not the full `limit`-candle range — the rest is already on disk
    from the previous scan.

    Raises DataUnavailable if the incremental fetch itself fails, or if the
    raw layer still doesn't fully cover the requested window afterwards.
    """
    interval_ms = interval_to_ms(interval)
    end = datetime.now(timezone.utc)

    incremental_start = end - timedelta(milliseconds=interval_ms * _INCREMENTAL_CANDLES)
    try:
        fresh = _source.fetch_ohlcv(symbol, interval, incremental_start, end)
        _store.write(fresh, INGESTION_ASSET_CLASS)
    except Exception as exc:
        raise DataUnavailable(f"{symbol} {interval} | incremental reload failed: {exc}") from exc

    window_start = end - timedelta(milliseconds=interval_ms * (limit + _INCREMENTAL_CANDLES))
    try:
        df = _store.read(INGESTION_ASSET_CLASS, INGESTION_SOURCE, symbol, interval, window_start, end)
    except MissingDataError as exc:
        raise DataUnavailable(f"{symbol} {interval} | raw layer incomplete after reload: {exc}") from exc

    return to_ohlc_frame(df).tail(limit)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def scan() -> None:
    trader.sync_open_trades()

    daily_pnl = database.get_daily_pnl()
    loss_limit = -(ACCOUNT_SIZE * DAILY_LOSS_LIMIT)
    if daily_pnl < loss_limit:
        log.warning("Circuit breaker active — daily PnL $%.2f below limit $%.2f, skipping scan", daily_pnl, loss_limit)
        notify.send(
            f"[CIRCUIT BREAKER] Daily loss limit hit (${daily_pnl:.2f}). "
            f"No new trades until midnight UTC."
        )
        return

    fear_greed = sentiment.get_fear_greed()
    log.info("Scan started")
    results = []

    for symbol in SYMBOLS:
        try:
            df_4h = _load_recent(symbol, HIGHER_INTERVAL, KLINE_LIMIT)
            df_1h = _load_recent(symbol, LOWER_INTERVAL, KLINE_LIMIT)

            df_4h = add_indicators(df_4h)
            df_1h = add_indicators(df_1h)

            higher_trend = get_trend_4h(df_4h)
            signal = generate_entry_signal(df_1h, higher_trend)

            latest_1h = df_1h.iloc[-1]
            price = latest_1h["Close"]
            rsi = latest_1h["RSI"]
            macd_hist = latest_1h["MACD_HIST"]
            atr = latest_1h["ATR"]

            log.info(
                "%s | trend=%s price=%.4f rsi=%.2f macd_hist=%.4f signal=%s",
                symbol, higher_trend, price, rsi, macd_hist, signal,
            )

            if signal in ("HOLD", "NOT ENOUGH DATA"):
                continue

            # --- Macro sentiment filter ---
            if signal == "BUY" and fear_greed < 25:
                log.info("%s | BUY skipped — extreme market fear (%d)", symbol, fear_greed)
                continue
            if signal == "SELL" and fear_greed > 75:
                log.info("%s | SELL skipped — extreme market greed (%d)", symbol, fear_greed)
                continue

            # --- News sentiment filter ---
            news_score = sentiment.get_news_sentiment(symbol)
            if signal == "BUY" and news_score < -0.5:
                log.info("%s | BUY skipped — bearish news sentiment (%.2f)", symbol, news_score)
                continue
            if signal == "SELL" and news_score > 0.5:
                log.info("%s | SELL skipped — bullish news sentiment (%.2f)", symbol, news_score)
                continue

            trade_plan = create_trade_plan(df_1h, signal)
            if not is_trade_worth_it(trade_plan):
                log.info("%s | rejected — R/R %.2f < %.1f", symbol, trade_plan.get("rr_ratio", 0), MIN_RR)
                continue

            position_size = calculate_position_size(
                ACCOUNT_SIZE, RISK_PER_TRADE,
                trade_plan["entry"], trade_plan["stop_loss"],
            )
            score = calculate_score(df_4h, df_1h)

            record = {
                "symbol": symbol,
                "signal": signal,
                "trend_4h": higher_trend,
                "price": price,
                "rsi_1h": rsi,
                "macd_hist_1h": macd_hist,
                "atr_1h": atr,
                "score": score,
                "entry": trade_plan["entry"],
                "stop_loss": trade_plan["stop_loss"],
                "take_profit": trade_plan["take_profit"],
                "rr_ratio": trade_plan["rr_ratio"],
                "position_size": position_size,
            }
            database.log_signal(record)
            results.append(record)

        except DataUnavailable as exc:
            log.warning("%s | skipped — %s", symbol, exc)
        except Exception:
            log.exception("Error processing %s", symbol)

    if not results:
        log.info("Scan complete — no actionable signals")
        return

    results.sort(key=lambda x: x["score"], reverse=True)
    log.info("Scan complete — %d signal(s) logged, opening in score order", len(results))

    for r in results:
        log.info(
            "%s | %s score=%.4f entry=%.4f sl=%.4f tp=%.4f rr=%.2f pos=%.6f",
            r["symbol"], r["signal"], r["score"],
            r["entry"], r["stop_loss"], r["take_profit"],
            r["rr_ratio"], r["position_size"],
        )
        trader.open_trade(r)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    setup_logging()
    database.init_db()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(scan, trigger="cron", minute=2, id="market_scan")
    scheduler.add_job(trader.sync_open_trades, trigger="interval", minutes=5, id="trade_sync")

    log.info("Bot starting — running initial scan, then every hour")
    notify.send(f"[START] Trading bot online — watching {', '.join(SYMBOLS)}")
    scan()  # run immediately on startup

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
