"""CLI entry point for the ingestion layer.

Usage:
    python -m ingestion.load --symbol BTCUSDT ETHUSDT --interval 1h
    python -m ingestion.load --symbol BTCUSDT --interval 4h --start 2024-01-01 --end 2024-06-01
    python -m ingestion.load --symbol BTCUSDT --interval 1h --dry-run
"""
import argparse
import logging
from datetime import datetime, timedelta, timezone

from config import INGESTION_ASSET_CLASS, INGESTION_SYMBOLS, RAW_DATA_DIR, setup_logging
from ingestion.base import MarketDataSource
from ingestion.binance_source import BinanceSource
from ingestion.raw_store import RawStore

log = logging.getLogger("ingestion.load")


def _default_start() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=30)


def _parse_date(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion.load",
        description="Fetch OHLCV candles and store them in the raw Parquet layer.",
    )
    parser.add_argument(
        "--symbol", nargs="+", dest="symbols", default=INGESTION_SYMBOLS,
        help="One or more symbols to load (default: symbols from config.py)",
    )
    parser.add_argument(
        "--interval", required=True,
        help="Kline interval, e.g. 1h, 4h",
    )
    parser.add_argument(
        "--start", type=_parse_date, default=None,
        help="Start date/time (UTC, ISO format, e.g. 2024-01-01). Default: 30 days ago.",
    )
    parser.add_argument(
        "--end", type=_parse_date, default=None,
        help="End date/time (UTC, ISO format). Default: now.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and show what would be written, without writing anything.",
    )
    return parser


def run(
    symbols: list[str],
    interval: str,
    start: datetime | None,
    end: datetime | None,
    dry_run: bool,
    source: MarketDataSource | None = None,
    store: RawStore | None = None,
) -> None:
    start = start or _default_start()
    end = end or datetime.now(timezone.utc)
    source = source if source is not None else BinanceSource()
    store = store if store is not None else RawStore(base_dir=RAW_DATA_DIR)

    total = len(symbols)
    for i, symbol in enumerate(symbols, start=1):
        log.info(
            "[%d/%d] Fetching %s %s from %s to %s",
            i, total, symbol, interval, start.isoformat(), end.isoformat(),
        )
        df = source.fetch_ohlcv(symbol, interval, start, end)

        if df.empty:
            log.info("[%d/%d] %s %s | no candles returned for this range", i, total, symbol, interval)
            continue

        log.info("[%d/%d] %s %s | fetched %d candles", i, total, symbol, interval, len(df))

        if dry_run:
            for path, count in store.preview(df, INGESTION_ASSET_CLASS):
                log.info("[DRY RUN] %s %s | would write %d rows to %s", symbol, interval, count, path)
            continue

        store.write(df, INGESTION_ASSET_CLASS)


def main() -> None:
    setup_logging()
    args = build_parser().parse_args()
    run(args.symbols, args.interval, args.start, args.end, args.dry_run)


if __name__ == "__main__":
    main()
