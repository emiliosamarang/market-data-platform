import sqlite3
from contextlib import contextmanager
from config import DB_PATH

_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    symbol         TEXT NOT NULL,
    signal         TEXT NOT NULL,
    trend_4h       TEXT,
    price          REAL,
    rsi_1h         REAL,
    macd_hist_1h   REAL,
    atr_1h         REAL,
    score          REAL,
    entry          REAL,
    stop_loss      REAL,
    take_profit    REAL,
    rr_ratio       REAL,
    position_size  REAL
)
"""

_CREATE_TRADES = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at       TIMESTAMP,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    entry_price     REAL,
    stop_loss       REAL,
    take_profit     REAL,
    position_size   REAL,
    entry_order_id  TEXT,
    exit_order_id   TEXT,
    close_price     REAL,
    pnl             REAL
)
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_CREATE_SIGNALS)
        conn.execute(_CREATE_TRADES)


def log_signal(record: dict) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO signals (
                symbol, signal, trend_4h, price, rsi_1h, macd_hist_1h,
                atr_1h, score, entry, stop_loss, take_profit, rr_ratio, position_size
            ) VALUES (
                :symbol, :signal, :trend_4h, :price, :rsi_1h, :macd_hist_1h,
                :atr_1h, :score, :entry, :stop_loss, :take_profit, :rr_ratio, :position_size
            )
            """,
            record,
        )


def open_trade(record: dict) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO trades (
                symbol, side, entry_price, stop_loss, take_profit,
                position_size, entry_order_id, exit_order_id
            ) VALUES (
                :symbol, :side, :entry_price, :stop_loss, :take_profit,
                :position_size, :entry_order_id, :exit_order_id
            )
            """,
            record,
        )
        return cur.lastrowid


def get_open_trade(symbol: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE symbol = ? AND status = 'open'",
            (symbol,),
        ).fetchone()
        return dict(row) if row else None


def count_open_trades() -> int:
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'open'"
        ).fetchone()[0]


def get_all_open_trades() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'open'"
        ).fetchall()
        return [dict(row) for row in rows]


def get_daily_pnl() -> float:
    with _connect() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE status = 'closed' AND date(closed_at) = date('now')"
        ).fetchone()[0]


def close_trade(trade_id: int, close_price: float, pnl: float) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE trades
            SET status = 'closed', closed_at = CURRENT_TIMESTAMP,
                close_price = ?, pnl = ?
            WHERE id = ?
            """,
            (close_price, pnl, trade_id),
        )
