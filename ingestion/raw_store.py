import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from ingestion.base import OHLCV_COLUMNS
from ingestion.time_utils import interval_to_ms, to_ms, to_utc_timestamp

logger = logging.getLogger("ingestion.raw_store")

# A row is uniquely identified by when it happened and where it came from.
# Re-writing the same candle (e.g. because a fetch range overlaps a previous
# one) must not create a second row for the same key.
_DEDUPE_KEYS = ["timestamp", "source", "symbol", "interval"]

# How many missing timestamps to name explicitly in a MissingDataError
# before collapsing the rest into "(+N more)".
_MISSING_PREVIEW_LIMIT = 5


class MissingDataError(Exception):
    """Raised by RawStore.read() when the raw layer doesn't fully cover a requested range."""


class RawStore:
    """Writes OHLCV DataFrames to Parquet, partitioned by day.

    Layout: {base_dir}/{asset_class}/{source}/{symbol}/{interval}/{date}.parquet

    Writing is idempotent: loading the same (or an overlapping) time range
    twice merges into the existing partition file and deduplicates on
    (timestamp, source, symbol, interval) rather than appending duplicate
    rows.
    """

    def __init__(self, base_dir: str | Path = "data/raw"):
        self.base_dir = Path(base_dir)

    def write(self, df: pd.DataFrame, asset_class: str) -> None:
        for path, group in self._partitions(df, asset_class):
            self._write_partition(path, group)

    def read(
        self,
        asset_class: str,
        source: str,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Read all candles for symbol/interval in [start, end] from disk.

        Raises MissingDataError instead of silently returning a partial
        range if any candle expected on the interval's grid is absent —
        whether because a whole day-partition is missing or because an
        existing partition has a gap in it.
        """
        combined = self.load_range(asset_class, source, symbol, interval, start, end)

        missing_ms = self._missing_candles(combined, interval, to_ms(start), to_ms(end))
        if missing_ms:
            raise MissingDataError(
                self._missing_data_message(symbol, interval, start, end, missing_ms)
            )

        return combined[OHLCV_COLUMNS]

    def load_range(
        self,
        asset_class: str,
        source: str,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        dedupe: bool = True,
    ) -> pd.DataFrame:
        """Read whatever is on disk for symbol/interval in [start, end], gaps and all.

        Unlike read(), this never raises on missing candles — it's the
        entry point for callers that need to inspect incomplete or
        duplicate-laden data rather than treat it as an error, e.g. quality
        checks. Pass dedupe=False to see raw partition contents including
        any duplicate rows (read() and dedupe=True both collapse those on
        (timestamp, source, symbol, interval), keeping the last write).
        """
        start_ms, end_ms = to_ms(start), to_ms(end)
        if start_ms > end_ms:
            raise ValueError(f"start ({start}) is after end ({end})")

        frames = [
            pd.read_parquet(path)
            for path in self._partition_paths(asset_class, source, symbol, interval, start, end)
            if path.exists()
        ]
        combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=OHLCV_COLUMNS)

        if not combined.empty:
            start_ts, end_ts = to_utc_timestamp(start), to_utc_timestamp(end)
            combined = combined[(combined["timestamp"] >= start_ts) & (combined["timestamp"] <= end_ts)]

        if dedupe:
            combined = combined.drop_duplicates(subset=_DEDUPE_KEYS, keep="last")
        combined = combined.sort_values("timestamp").reset_index(drop=True)

        return combined[OHLCV_COLUMNS]

    def missing_candles(
        self, df: pd.DataFrame, interval: str, start: datetime, end: datetime
    ) -> list[pd.Timestamp]:
        """Public wrapper around the grid-gap check read() uses internally.

        Returns the UTC timestamps expected on the interval's grid within
        [start, end] that aren't present in df, sorted, empty if none.
        """
        missing_ms = self._missing_candles(df, interval, to_ms(start), to_ms(end))
        return [pd.Timestamp(ms, unit="ms", tz="UTC") for ms in missing_ms]

    def _missing_candles(
        self, combined: pd.DataFrame, interval: str, start_ms: int, end_ms: int
    ) -> list[int]:
        interval_ms = interval_to_ms(interval)
        # Only candles that actually fall on the grid within [start, end] are
        # "expected" — ceil the start up and floor the end down to the grid.
        expected_start_ms = -(-start_ms // interval_ms) * interval_ms
        expected_end_ms = (end_ms // interval_ms) * interval_ms
        if expected_start_ms > expected_end_ms:
            return []

        expected = set(range(expected_start_ms, expected_end_ms + 1, interval_ms))
        actual = {int(ts.timestamp() * 1000) for ts in combined["timestamp"]} if not combined.empty else set()
        return sorted(expected - actual)

    def _missing_data_message(
        self, symbol: str, interval: str, start: datetime, end: datetime, missing_ms: list[int]
    ) -> str:
        preview_ts = [pd.Timestamp(ms, unit="ms", tz="UTC").isoformat() for ms in missing_ms[:_MISSING_PREVIEW_LIMIT]]
        more = len(missing_ms) - len(preview_ts)
        suffix = f" (+{more} more)" if more > 0 else ""
        return (
            f"Raw layer is missing {len(missing_ms)} candle(s) for {symbol} {interval} "
            f"between {start} and {end}: {', '.join(preview_ts)}{suffix}. "
            f"Run `python -m ingestion.load --symbol {symbol} --interval {interval} "
            f"--start {to_utc_timestamp(start).date()} --end {to_utc_timestamp(end).date()}` "
            f"to backfill, or pass --refresh."
        )

    def _partition_paths(
        self, asset_class: str, source: str, symbol: str, interval: str, start: datetime, end: datetime
    ):
        for date in self._dates_between(start, end):
            yield self._partition_path(asset_class, source, symbol, interval, date)

    def _dates_between(self, start: datetime, end: datetime):
        current = to_utc_timestamp(start).date()
        last = to_utc_timestamp(end).date()
        while current <= last:
            yield current.strftime("%Y-%m-%d")
            current += timedelta(days=1)

    def preview(self, df: pd.DataFrame, asset_class: str) -> list[tuple[Path, int]]:
        """Return (partition_path, incoming_row_count) pairs without writing.

        Used for --dry-run: shows what write() would touch and how many
        rows it would add, without merging into or touching disk state.
        """
        return [(path, len(group)) for path, group in self._partitions(df, asset_class)]

    def _partitions(self, df: pd.DataFrame, asset_class: str):
        if df.empty:
            return

        df = df.copy()
        df["_date"] = df["timestamp"].dt.strftime("%Y-%m-%d")

        for (source, symbol, interval, date), group in df.groupby(
            ["source", "symbol", "interval", "_date"], sort=True
        ):
            path = self._partition_path(asset_class, source, symbol, interval, date)
            yield path, group.drop(columns="_date")

    def _partition_path(
        self, asset_class: str, source: str, symbol: str, interval: str, date: str
    ) -> Path:
        return self.base_dir / asset_class / source / symbol / interval / f"{date}.parquet"

    def _write_partition(self, path: Path, new_rows: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():
            existing = pd.read_parquet(path)
            combined = pd.concat([existing, new_rows], ignore_index=True)
        else:
            combined = new_rows

        combined = combined.drop_duplicates(subset=_DEDUPE_KEYS, keep="last")
        combined = combined.sort_values("timestamp").reset_index(drop=True)

        # Write-to-temp + atomic rename so a crash mid-write never leaves a
        # truncated/corrupt partition file behind.
        tmp_path = path.with_suffix(".parquet.tmp")
        combined.to_parquet(tmp_path, index=False)
        tmp_path.replace(path)

        logger.info(
            "Wrote %d rows to %s (%d rows in this batch)",
            len(combined), path, len(new_rows),
        )
