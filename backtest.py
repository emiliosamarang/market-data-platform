#!/usr/bin/env python3
"""
Backtest the trading strategy on Binance historical data.

Usage:
    python backtest.py                          # 1 year, all symbols from config
    python backtest.py --days 730               # 2 years
    python backtest.py --symbols BTCUSDT ETHUSDT
"""
import argparse
import logging
from datetime import datetime, timedelta, timezone

import exchange
from bot import (
    add_indicators,
    get_trend_4h,
    generate_entry_signal,
    create_trade_plan,
    is_trade_worth_it,
    calculate_position_size,
)
from config import SYMBOLS, ACCOUNT_SIZE, RISK_PER_TRADE

logging.basicConfig(level=logging.WARNING)

WARMUP = 100  # skip first N candles while indicators stabilise
FEE_RATE = 0.001  # 0.1% per side — Binance standard spot taker fee


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_history(symbol: str, interval: str, days: int):
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d %b %Y")
    raw = exchange.client.get_historical_klines(symbol, interval, start)
    if not raw:
        raise ValueError(f"No data returned for {symbol} {interval}")

    import pandas as pd
    cols = [
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    df = df[["open_time", "Open", "High", "Low", "Close", "Volume"]].copy()
    df[["Open", "High", "Low", "Close", "Volume"]] = df[
        ["Open", "High", "Low", "Close", "Volume"]
    ].astype(float)
    df.index = __import__("pandas").to_datetime(df["open_time"], unit="ms", utc=True)
    df.index.name = "timestamp"
    return df.drop(columns="open_time").dropna()


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def run_backtest(symbol: str, df_4h, df_1h) -> list[dict]:
    df_4h = add_indicators(df_4h)
    df_1h = add_indicators(df_1h)

    trades = []
    open_trade = None

    for i in range(WARMUP, len(df_1h) - 1):
        current_time = df_1h.index[i]
        candle = df_1h.iloc[i]

        # ---- Manage open trade ----
        if open_trade:
            side = open_trade["side"]
            sl = open_trade["sl"]
            tp = open_trade["tp"]

            hit_sl = candle["Low"] <= sl if side == "BUY" else candle["High"] >= sl
            hit_tp = candle["High"] >= tp if side == "BUY" else candle["Low"] <= tp

            exit_price = None
            exit_reason = None
            if hit_sl:
                # Assume worst case: SL hit before TP when both on same candle
                exit_price = sl
                exit_reason = "SL"
            elif hit_tp:
                exit_price = tp
                exit_reason = "TP"

            if exit_price is not None:
                entry = open_trade["entry"]
                size = open_trade["size"]
                gross_pnl = (
                    (exit_price - entry) * size
                    if side == "BUY"
                    else (entry - exit_price) * size
                )
                fee = (entry + exit_price) * size * FEE_RATE
                pnl = gross_pnl - fee
                open_trade.update(
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    exit_time=current_time,
                    pnl=pnl,
                    fee=fee,
                )
                trades.append(open_trade)
                open_trade = None

            continue  # never open a new trade on the same candle we check exits

        # ---- Look for new signal ----
        df_4h_slice = df_4h[df_4h.index <= current_time]
        if len(df_4h_slice) < 50:
            continue

        df_1h_slice = df_1h.iloc[: i + 1]
        higher_trend = get_trend_4h(df_4h_slice)
        signal = generate_entry_signal(df_1h_slice, higher_trend)

        if signal not in ("BUY", "SELL"):
            continue

        trade_plan = create_trade_plan(df_1h_slice, signal)
        if not is_trade_worth_it(trade_plan):
            continue

        # Enter at next candle's open — avoids lookahead bias on entry price
        next_open = df_1h.iloc[i + 1]["Open"]
        size = calculate_position_size(
            ACCOUNT_SIZE, RISK_PER_TRADE, next_open, trade_plan["stop_loss"]
        )
        if size <= 0:
            continue

        open_trade = {
            "symbol": symbol,
            "side": signal,
            "entry": next_open,
            "sl": trade_plan["stop_loss"],
            "tp": trade_plan["take_profit"],
            "size": size,
            "entry_time": df_1h.index[i + 1],
            "exit_price": None,
            "exit_reason": None,
            "exit_time": None,
            "pnl": None,
        }

    return trades


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_report(label: str, trades: list[dict], account: float) -> None:
    closed = [t for t in trades if t.get("pnl") is not None]
    print(f"\n{'=' * 52}")
    print(f"  {label}")
    print(f"{'=' * 52}")

    if not closed:
        print("  No closed trades.")
        return

    pnls = [t["pnl"] for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    win_rate = len(wins) / len(closed) * 100
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    profit_factor = (
        sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else float("inf")
    )

    # Equity curve + max drawdown
    equity = account
    peak = account
    max_dd_pct = 0.0
    equity_curve = [account]
    for p in pnls:
        equity += p
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd_pct:
            max_dd_pct = dd

    # Consecutive losses
    max_consec_losses = cur = 0
    for p in pnls:
        if p <= 0:
            cur += 1
            max_consec_losses = max(max_consec_losses, cur)
        else:
            cur = 0

    sl_count = sum(1 for t in closed if t["exit_reason"] == "SL")
    tp_count = sum(1 for t in closed if t["exit_reason"] == "TP")
    total_fees = sum(t.get("fee", 0) for t in closed)

    print(f"  Trades:              {len(closed)}")
    print(f"  Win rate:            {win_rate:.1f}%   ({len(wins)}W / {len(losses)}L)")
    print(f"  TP hits / SL hits:   {tp_count} / {sl_count}")
    print(f"  Profit factor:       {profit_factor:.2f}")
    print(f"  Avg win:             ${avg_win:+.2f}")
    print(f"  Avg loss:            ${avg_loss:+.2f}")
    print(f"  Total fees paid:     ${total_fees:.2f}")
    print(f"  Total PnL (net):     ${total_pnl:+.2f}")
    print(f"  Return on account:   {total_pnl / account * 100:+.1f}%")
    print(f"  Max drawdown:        {max_dd_pct:.1f}%")
    print(f"  Max consec. losses:  {max_consec_losses}")
    print(f"  Final equity:        ${equity_curve[-1]:.2f}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(symbols: list[str], days: int) -> None:
    all_trades: list[dict] = []

    for symbol in symbols:
        print(f"Fetching {symbol} ({days}d of 1h + 4h)...", end=" ", flush=True)
        try:
            df_4h = fetch_history(symbol, "4h", days + 30)   # extra buffer for 4h warmup
            df_1h = fetch_history(symbol, "1h", days)
            print(f"{len(df_1h)} candles")
        except Exception as exc:
            print(f"FAILED — {exc}")
            continue

        trades = run_backtest(symbol, df_4h, df_1h)
        print_report(symbol, trades, ACCOUNT_SIZE)
        all_trades.extend(trades)

    if len(symbols) > 1:
        print_report("ALL SYMBOLS COMBINED", all_trades, ACCOUNT_SIZE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the trading strategy")
    parser.add_argument("--days", type=int, default=365, help="How many days to backtest")
    parser.add_argument("--symbols", nargs="+", default=SYMBOLS, help="Symbols to test")
    args = parser.parse_args()
    main(args.symbols, args.days)
