import logging

import database
import exchange
import notify
from config import MAX_OPEN_POSITIONS, TRADE_LOGGER, API_LOGGER

log = logging.getLogger(__name__)
trade_log = logging.getLogger(TRADE_LOGGER)
api_log = logging.getLogger(API_LOGGER)


def open_trade(record: dict) -> None:
    symbol = record["symbol"]
    signal = record["signal"]
    size = record["position_size"]
    sl = record["stop_loss"]
    tp = record["take_profit"]

    if database.get_open_trade(symbol):
        log.info("%s | skipped — already have open position", symbol)
        return

    open_count = database.count_open_trades()
    if open_count >= MAX_OPEN_POSITIONS:
        log.info("%s | skipped — max %d positions reached", symbol, MAX_OPEN_POSITIONS)
        return

    side = "BUY" if signal == "BUY" else "SELL"

    try:
        order = exchange.place_market_order(symbol, side, size)
    except Exception:
        api_log.exception("%s | entry order failed", symbol)
        return

    fills = order.get("fills", [])
    filled_price = (
        sum(float(f["price"]) * float(f["qty"]) for f in fills)
        / sum(float(f["qty"]) for f in fills)
        if fills else record["entry"]
    )
    entry_order_id = str(order["orderId"])
    trade_log.info("%s | %s filled: id=%s price=%.4f qty=%.6f",
                   symbol, side, entry_order_id, filled_price, size)

    close_side = "SELL" if side == "BUY" else "BUY"
    oco_id = None
    try:
        oco = exchange.place_oco_order(symbol, close_side, size, tp, sl)
        oco_id = str(oco.get("orderListId", ""))
        trade_log.info("%s | OCO placed: id=%s tp=%.4f sl=%.4f", symbol, oco_id, tp, sl)
    except Exception:
        api_log.exception("%s | OCO failed — attempting emergency market close", symbol)
        try:
            exchange.place_market_order(symbol, close_side, size)
            trade_log.warning("%s | emergency close executed — position flat, no DB record", symbol)
            notify.send(
                f"[WARNING] {symbol} — OCO failed, emergency market close executed. Position is flat."
            )
            return
        except Exception:
            api_log.exception("%s | emergency close also failed", symbol)
            trade_log.critical(
                "%s | MANUAL INTERVENTION REQUIRED — open %s qty=%.6f entry=%.4f",
                symbol, side, size, filled_price,
            )
            notify.send(
                f"[CRITICAL] {symbol} — OCO failed AND emergency close failed.\n"
                f"MANUAL ACTION REQUIRED: close {side} {size:.6f} @ entry {filled_price:.4f}"
            )

    database.open_trade({
        "symbol": symbol,
        "side": side,
        "entry_price": filled_price,
        "stop_loss": sl,
        "take_profit": tp,
        "position_size": size,
        "entry_order_id": entry_order_id,
        "exit_order_id": oco_id,
    })
    trade_log.info(
        "[OPEN] %s %s  entry=%.4f  size=%.6f  sl=%.4f  tp=%.4f  rr=%.1f",
        side, symbol, filled_price, size, sl, tp,
        abs(tp - filled_price) / abs(filled_price - sl),
    )
    notify.send(
        f"[TRADE OPEN] {side} {symbol}\n"
        f"Entry: {filled_price:.4f}  Size: {size:.6f}\n"
        f"SL: {sl:.4f}  TP: {tp:.4f}  R/R: {abs(tp - filled_price) / abs(filled_price - sl):.1f}"
    )


def sync_open_trades() -> None:
    open_trades = database.get_all_open_trades()
    if not open_trades:
        return

    for trade in open_trades:
        symbol = trade["symbol"]
        oco_id = trade.get("exit_order_id")

        if not oco_id:
            trade_log.warning("%s | trade id=%d has no OCO — position is unmanaged", symbol, trade["id"])
            continue

        try:
            oco = exchange.client.get_order_list(orderListId=int(oco_id))
        except Exception:
            api_log.exception("%s | failed to fetch OCO %s", symbol, oco_id)
            continue

        if oco.get("listOrderStatus") != "ALL_DONE":
            log.debug("%s | OCO %s still active", symbol, oco_id)
            continue

        filled_order = None
        for order_ref in oco.get("orders", []):
            try:
                order = exchange.client.get_order(symbol=symbol, orderId=order_ref["orderId"])
                if order["status"] == "FILLED":
                    filled_order = order
                    break
            except Exception:
                api_log.exception("%s | failed to fetch order %s", symbol, order_ref["orderId"])

        if not filled_order:
            trade_log.warning("%s | OCO %s ALL_DONE but no FILLED leg found", symbol, oco_id)
            continue

        exec_qty = float(filled_order["executedQty"])
        if exec_qty == 0:
            trade_log.warning("%s | filled leg has zero executedQty", symbol)
            continue

        close_price = float(filled_order["cummulativeQuoteQty"]) / exec_qty
        exit_reason = "TP" if filled_order["type"] == "LIMIT_MAKER" else "SL"

        entry = trade["entry_price"]
        size = trade["position_size"]
        pnl = (
            (close_price - entry) * size
            if trade["side"] == "BUY"
            else (entry - close_price) * size
        )

        database.close_trade(trade["id"], close_price, pnl)
        trade_log.info(
            "[CLOSE] %s %s via %s — close=%.4f  entry=%.4f  pnl=$%+.2f",
            trade["side"], symbol, exit_reason, close_price, entry, pnl,
        )
        notify.send(
            f"[{exit_reason}] {symbol} {trade['side']} closed\n"
            f"Exit: {close_price:.4f}  Entry: {entry:.4f}  PnL: ${pnl:+.2f}"
        )
