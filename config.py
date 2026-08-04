import os

API_KEY = os.environ.get("BINANCE_API_KEY", "")
API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"]
HIGHER_INTERVAL = "4h"
LOWER_INTERVAL = "1h"
KLINE_LIMIT = 200  # enough bars for indicator warmup (EMA50 needs ~50, plus buffer)

ACCOUNT_SIZE = 1000
RISK_PER_TRADE = 0.01
MIN_RR = 2.0
MAX_OPEN_POSITIONS = 3
DAILY_LOSS_LIMIT = 0.03  # halt new trades if daily closed PnL drops below -3% of account

DB_PATH = "trading_bot.db"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
