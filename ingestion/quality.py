"""CLI entry point for data quality checks on the raw layer.

Usage:
    python -m ingestion.quality --symbol BTCUSDT --interval 1h
    python -m ingestion.quality --symbol BTCUSDT ETHUSDT --interval 1h --start 2024-01-01 --end 2024-06-01

Runs six checks per symbol against the raw Parquet layer (see RawStore):
gaps in the time grid, duplicate rows, OHLC plausibility, zero-volume
candles, staleness of the most recent candle, and statistical outliers in
returns. ERROR-severity checks fail the run (non-zero exit code), matching
backtest.py's INCOMPLETE-result convention; WARNING-severity checks are
reported but never block. Every run writes its results to a report file
under data/quality/, one row per check, shaped so it can later be loaded
straight into a fact_quality_check table.
"""
import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    INGESTION_ASSET_CLASS, INGESTION_SOURCE, INGESTION_SYMBOLS,
    QUALITY_REPORT_DIR, RAW_DATA_DIR, setup_logging,
)
from ingestion.raw_store import RawStore
from ingestion.time_utils import interval_to_ms

log = logging.getLogger("ingestion.quality")

ERROR = "ERROR"
WARNING = "WARNING"

# Outlier detection: robust z-score on log-returns using a rolling
# median/MAD window rather than a fixed percentage threshold, so it adapts
# to each symbol's own volatility instead of false-flagging every ordinary
# crypto swing. A high threshold (10) is deliberate — this check should
# only catch feed glitches (fat-fingered prints, bad ticks), never
# genuinely volatile-but-real candles.
OUTLIER_WINDOW = 30
OUTLIER_Z_THRESHOLD = 10.0
OUTLIER_MAD_SCALE = 0.6745  # scales MAD to be comparable to a normal std-dev

# A candle is "stale" if the newest one on disk is older than this many
# interval-lengths. Only checked when the requested range reaches close to
# wall-clock now — a historical backtest range isn't supposed to be fresh.
FRESHNESS_INTERVAL_MULTIPLE = 2

REPORT_COLUMNS = [
    "run_ts", "symbol", "interval", "check_name",
    "severity", "passed", "violation_count", "details",
]

_DETAIL_PREVIEW_LIMIT = 5


@dataclass
class CheckResult:
    check_name: str
    severity: str
    passed: bool
    violation_count: int
    details: str


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_gaps(df: pd.DataFrame, store: RawStore, interval: str, start: datetime, end: datetime) -> CheckResult:
    missing = store.missing_candles(df, interval, start, end)
    return CheckResult(
        "gaps", ERROR, passed=not missing, violation_count=len(missing),
        details=_preview(ts.isoformat() for ts in missing),
    )


def check_duplicates(raw_df: pd.DataFrame) -> CheckResult:
    dupe_mask = raw_df.duplicated(subset=["timestamp", "source", "symbol", "interval"], keep=False)
    violations = raw_df.loc[dupe_mask, "timestamp"]
    return CheckResult(
        "duplicates", ERROR, passed=not dupe_mask.any(), violation_count=int(dupe_mask.sum()),
        details=_preview(ts.isoformat() for ts in violations),
    )


def check_ohlc_plausibility(df: pd.DataFrame) -> CheckResult:
    bad = (
        (df["high"] < df["low"])
        | (df["high"] < df["open"]) | (df["high"] < df["close"])
        | (df["low"] > df["open"]) | (df["low"] > df["close"])
        | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
    )
    violations = df.loc[bad, "timestamp"]
    return CheckResult(
        "ohlc_plausibility", ERROR, passed=not bad.any(), violation_count=int(bad.sum()),
        details=_preview(ts.isoformat() for ts in violations),
    )


def check_zero_volume(df: pd.DataFrame) -> CheckResult:
    zero = df["volume"] == 0
    violations = df.loc[zero, "timestamp"]
    return CheckResult(
        "zero_volume", WARNING, passed=not zero.any(), violation_count=int(zero.sum()),
        details=_preview(ts.isoformat() for ts in violations),
    )


def check_freshness(df: pd.DataFrame, interval: str, end: datetime) -> CheckResult | None:
    """None means "not applicable" — end isn't close enough to now for
    staleness-vs-wall-clock to mean anything (e.g. a historical backtest range)."""
    now = datetime.now(timezone.utc)
    if (now - end) > timedelta(milliseconds=interval_to_ms(interval)):
        return None

    if df.empty:
        return CheckResult("freshness", ERROR, passed=False, violation_count=1, details="no candles in range")

    last_ts = df["timestamp"].max()
    max_age_ms = FRESHNESS_INTERVAL_MULTIPLE * interval_to_ms(interval)
    age_ms = (now - last_ts).total_seconds() * 1000
    stale = age_ms > max_age_ms
    return CheckResult(
        "freshness", ERROR, passed=not stale, violation_count=1 if stale else 0,
        details=f"last candle {last_ts.isoformat()}, age {age_ms / 3_600_000:.1f}h (max {max_age_ms / 3_600_000:.1f}h)",
    )


def check_outliers(df: pd.DataFrame) -> CheckResult:
    close = df["close"].astype(float)
    log_returns = np.log(close / close.shift(1))

    median_r = log_returns.rolling(OUTLIER_WINDOW, min_periods=OUTLIER_WINDOW).median()
    mad = log_returns.rolling(OUTLIER_WINDOW, min_periods=OUTLIER_WINDOW).apply(_mad, raw=True)

    with np.errstate(divide="ignore", invalid="ignore"):
        z = OUTLIER_MAD_SCALE * (log_returns - median_r) / mad
    z = z.mask(mad == 0)  # price flat over the whole window — nothing to score, skip rather than divide by zero

    flagged = (z.abs() > OUTLIER_Z_THRESHOLD).fillna(False)
    violations = df.loc[flagged, "timestamp"]
    return CheckResult(
        "outliers", WARNING, passed=not flagged.any(), violation_count=int(flagged.sum()),
        details=_preview(ts.isoformat() for ts in violations),
    )


def _mad(x: np.ndarray) -> float:
    return float(np.median(np.abs(x - np.median(x))))


def _preview(items) -> str:
    items = list(items)
    if not items:
        return "none"
    shown = items[:_DETAIL_PREVIEW_LIMIT]
    more = len(items) - len(shown)
    suffix = f" (+{more} more)" if more > 0 else ""
    return ", ".join(shown) + suffix


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_checks(symbol: str, interval: str, start: datetime, end: datetime, store: RawStore) -> list[CheckResult]:
    raw_df = store.load_range(INGESTION_ASSET_CLASS, INGESTION_SOURCE, symbol, interval, start, end, dedupe=False)
    df = store.load_range(INGESTION_ASSET_CLASS, INGESTION_SOURCE, symbol, interval, start, end, dedupe=True)

    results = [
        check_gaps(df, store, interval, start, end),
        check_duplicates(raw_df),
        check_ohlc_plausibility(df),
        check_zero_volume(df),
        check_freshness(df, interval, end),
        check_outliers(df),
    ]
    return [r for r in results if r is not None]


def _default_start() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=30)


def _log_result(symbol: str, interval: str, result: CheckResult) -> None:
    log_fn = log.info if result.passed else (log.error if result.severity == ERROR else log.warning)
    status = "OK" if result.passed else f"FAILED ({result.violation_count})"
    log_fn("%s %s | %-18s %-6s | %s", symbol, interval, result.check_name, status, result.details)


def run(
    symbols: list[str],
    interval: str,
    start: datetime | None,
    end: datetime | None,
    store: RawStore | None = None,
    report_dir: str | Path | None = None,
) -> tuple[pd.DataFrame, int]:
    start = start or _default_start()
    end = end or datetime.now(timezone.utc)
    store = store if store is not None else RawStore(base_dir=RAW_DATA_DIR)
    report_dir = Path(report_dir if report_dir is not None else QUALITY_REPORT_DIR)
    run_ts = datetime.now(timezone.utc)

    rows = []
    for symbol in symbols:
        for result in run_checks(symbol, interval, start, end, store):
            _log_result(symbol, interval, result)
            rows.append({
                "run_ts": run_ts,
                "symbol": symbol,
                "interval": interval,
                "check_name": result.check_name,
                "severity": result.severity,
                "passed": result.passed,
                "violation_count": result.violation_count,
                "details": result.details,
            })

    report_df = pd.DataFrame(rows, columns=REPORT_COLUMNS)
    path = write_report(report_df, report_dir, run_ts)
    log.info("Wrote quality report to %s", path)

    failed = report_df[(report_df["severity"] == ERROR) & (~report_df["passed"])]
    exit_code = 1 if not failed.empty else 0
    if exit_code:
        log.error("Quality check FAILED — %d hard violation(s) across %d symbol(s)",
                   len(failed), failed["symbol"].nunique())

    return report_df, exit_code


def write_report(report_df: pd.DataFrame, report_dir: Path, run_ts: datetime) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{run_ts.strftime('%Y-%m-%dT%H%M%SZ')}.parquet"
    report_df.to_parquet(path, index=False)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion.quality",
        description="Run data quality checks against the raw Parquet layer.",
    )
    parser.add_argument(
        "--symbol", nargs="+", dest="symbols", default=INGESTION_SYMBOLS,
        help="One or more symbols to check (default: symbols from config.py)",
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
    return parser


def _parse_date(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> int:
    setup_logging()
    args = build_parser().parse_args()
    _, exit_code = run(args.symbols, args.interval, args.start, args.end)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
