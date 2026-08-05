from decimal import Decimal, ROUND_DOWN

from binance.client import Client
from config import API_KEY, API_SECRET

client = Client(API_KEY, API_SECRET)

_symbol_filters: dict = {}


def _get_filters(symbol: str) -> dict:
    if symbol not in _symbol_filters:
        info = client.get_symbol_info(symbol)
        _symbol_filters[symbol] = {f["filterType"]: f for f in info["filters"]}
    return _symbol_filters[symbol]


def _round_step(value: float, step: str) -> float:
    return float(Decimal(str(value)).quantize(Decimal(step), rounding=ROUND_DOWN))


def round_quantity(symbol: str, qty: float) -> float:
    step = _get_filters(symbol)["LOT_SIZE"]["stepSize"]
    return _round_step(qty, step)


def round_price(symbol: str, price: float) -> float:
    tick = _get_filters(symbol)["PRICE_FILTER"]["tickSize"]
    return _round_step(price, tick)


def place_market_order(symbol: str, side: str, quantity: float) -> dict:
    qty = round_quantity(symbol, quantity)
    return client.create_order(
        symbol=symbol,
        side=side,
        type=Client.ORDER_TYPE_MARKET,
        quantity=qty,
    )


def place_oco_order(
    symbol: str,
    side: str,
    quantity: float,
    take_profit: float,
    stop_loss: float,
) -> dict:
    qty = round_quantity(symbol, quantity)
    tp = round_price(symbol, take_profit)
    sl = round_price(symbol, stop_loss)
    # stopLimitPrice slightly worse than stop to ensure fill
    sl_limit = round_price(symbol, sl * 0.999 if side == "SELL" else sl * 1.001)
    return client.create_oco_order(
        symbol=symbol,
        side=side,
        quantity=qty,
        price=str(tp),
        stopPrice=str(sl),
        stopLimitPrice=str(sl_limit),
        stopLimitTimeInForce="GTC",
    )
