import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger("ingestion.raw_store")

# A row is uniquely identified by when it happened and where it came from.
# Re-writing the same candle (e.g. because a fetch range overlaps a previous
# one) must not create a second row for the same key.
_DEDUPE_KEYS = ["timestamp", "source", "symbol", "interval"]


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
