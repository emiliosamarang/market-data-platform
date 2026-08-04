import logging
import logging.handlers
import os
import sys

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

# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

INGESTION_SOURCE = "binance"
INGESTION_ASSET_CLASS = "crypto"
INGESTION_SYMBOLS = SYMBOLS
INGESTION_INTERVALS = [LOWER_INTERVAL, HIGHER_INTERVAL]
RAW_DATA_DIR = "data/raw"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
# Set LOG_FILE="" to disable the rotating file handler (e.g. in tests/CI).
LOG_FILE = os.environ.get("LOG_FILE", "trading_bot.log")

_LOG_FMT = "%(asctime)s  %(name)-20s  %(levelname)-8s  %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

# Dedicated logger names so trade events and API errors can be filtered
# independently in the log stream.
TRADE_LOGGER = "trade"
API_LOGGER = "api"


def setup_logging() -> None:
    """Configure root logger with stdout + optional rotating file handler.

    Safe to call multiple times; subsequent calls are no-ops.
    """
    root = logging.getLogger()
    if root.handlers:
        return

    level = getattr(logging, LOG_LEVEL, logging.INFO)
    root.setLevel(level)

    fmt = logging.Formatter(_LOG_FMT, datefmt=_DATE_FMT)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    root.addHandler(stdout_handler)

    if LOG_FILE:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    # Suppress chatty third-party loggers.
    for _name in ("urllib3", "binance", "apscheduler", "anthropic", "httpx"):
        logging.getLogger(_name).setLevel(logging.WARNING)
