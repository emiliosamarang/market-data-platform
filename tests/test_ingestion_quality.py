from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from config import INGESTION_SOURCE, INGESTION_SYMBOLS
from ingestion.base import OHLCV_COLUMNS
from ingestion.quality import (
    ERROR,
    OUTLIER_WINDOW,
    REPORT_COLUMNS,
    WARNING,
    build_parser,
    check_cross_source,
    check_duplicates,
    check_freshness,
    check_gaps,
    check_ohlc_plausibility,
    check_outliers,
    check_zero_volume,
    run,
    run_checks,
)
from ingestion.raw_store import RawStore


def _rows(timestamps, symbol="BTCUSDT", interval="1h", source="binance", price=100.0, volume=10.0):
    n = len(timestamps)
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "open": [price] * n,
            "high": [price * 1.01] * n,
            "low": [price * 0.99] * n,
            "close": [price + 1] * n,
            "volume": [volume] * n,
            "source": [source] * n,
            "symbol": [symbol] * n,
            "interval": [interval] * n,
        }
    )[OHLCV_COLUMNS]


# ---------------------------------------------------------------------------
# check_gaps
# ---------------------------------------------------------------------------

class TestCheckGaps:
    def test_no_gap_passes(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        df = _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z", "2024-01-01T02:00:00Z"])

        result = check_gaps(
            df, store, "1h",
            datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
        )

        assert result.passed
        assert result.severity == ERROR
        assert result.violation_count == 0

    def test_missing_hour_fails(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        df = _rows(["2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z"])  # 01:00 missing

        result = check_gaps(
            df, store, "1h",
            datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
        )

        assert not result.passed
        assert result.violation_count == 1
        assert "2024-01-01T01:00:00" in result.details


# ---------------------------------------------------------------------------
# check_duplicates
# ---------------------------------------------------------------------------

class TestCheckDuplicates:
    def test_unique_rows_pass(self):
        df = _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"])

        result = check_duplicates(df)

        assert result.passed
        assert result.severity == ERROR
        assert result.violation_count == 0

    def test_repeated_key_fails(self):
        df = pd.concat([_rows(["2024-01-01T00:00:00Z"]), _rows(["2024-01-01T00:00:00Z"])], ignore_index=True)

        result = check_duplicates(df)

        assert not result.passed
        assert result.violation_count == 2  # both copies counted


# ---------------------------------------------------------------------------
# check_ohlc_plausibility
# ---------------------------------------------------------------------------

class TestCheckOhlcPlausibility:
    def test_valid_candles_pass(self):
        df = _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"])

        result = check_ohlc_plausibility(df)

        assert result.passed
        assert result.severity == ERROR

    def test_high_below_low_fails(self):
        df = _rows(["2024-01-01T00:00:00Z"])
        df.loc[0, "high"] = 90.0
        df.loc[0, "low"] = 100.0

        result = check_ohlc_plausibility(df)

        assert not result.passed
        assert result.violation_count == 1

    def test_negative_price_fails(self):
        df = _rows(["2024-01-01T00:00:00Z"])
        df.loc[0, "open"] = -1.0

        result = check_ohlc_plausibility(df)

        assert not result.passed

    def test_close_outside_high_low_band_fails(self):
        df = _rows(["2024-01-01T00:00:00Z"])
        df.loc[0, "close"] = df.loc[0, "high"] + 10  # close above high


        result = check_ohlc_plausibility(df)

        assert not result.passed


# ---------------------------------------------------------------------------
# check_zero_volume
# ---------------------------------------------------------------------------

class TestCheckZeroVolume:
    def test_nonzero_volume_passes(self):
        df = _rows(["2024-01-01T00:00:00Z"], volume=10.0)

        result = check_zero_volume(df)

        assert result.passed
        assert result.severity == WARNING

    def test_zero_volume_fails_but_is_a_warning(self):
        df = _rows(["2024-01-01T00:00:00Z"], volume=0.0)

        result = check_zero_volume(df)

        assert not result.passed
        assert result.severity == WARNING
        assert result.violation_count == 1


# ---------------------------------------------------------------------------
# check_freshness
# ---------------------------------------------------------------------------

class TestCheckFreshness:
    def test_not_applicable_when_end_far_in_past(self):
        df = _rows(["2024-01-01T00:00:00Z"])
        end = datetime(2024, 1, 1, tzinfo=timezone.utc)

        result = check_freshness(df, "1h", end)

        assert result is None

    def test_passes_when_last_candle_is_recent(self):
        now = datetime.now(timezone.utc)
        df = _rows([(now - timedelta(minutes=20)).isoformat()])

        result = check_freshness(df, "1h", now)

        assert result.passed
        assert result.severity == ERROR

    def test_fails_when_last_candle_is_stale(self):
        now = datetime.now(timezone.utc)
        df = _rows([(now - timedelta(hours=5)).isoformat()])

        result = check_freshness(df, "1h", now)

        assert not result.passed

    def test_fails_when_no_candles_at_all(self):
        now = datetime.now(timezone.utc)
        df = _rows([])

        result = check_freshness(df, "1h", now)

        assert not result.passed
        assert "no candles" in result.details


# ---------------------------------------------------------------------------
# check_outliers
# ---------------------------------------------------------------------------

def _close_series_df(closes, start="2024-01-01T00:00:00Z"):
    timestamps = pd.date_range(start, periods=len(closes), freq="1h", tz="UTC")
    df = _rows([ts.isoformat() for ts in timestamps])
    df["close"] = closes
    return df


class TestCheckOutliers:
    def test_calm_market_passes(self):
        n = OUTLIER_WINDOW + 10
        closes = [100.0]
        for i in range(n - 1):
            closes.append(closes[-1] * (1.002 if i % 2 == 0 else 0.998))
        df = _close_series_df(closes)

        result = check_outliers(df)

        assert result.passed
        assert result.severity == WARNING

    def test_single_spike_is_flagged(self):
        n = OUTLIER_WINDOW + 10
        closes = [100.0]
        for i in range(n - 1):
            closes.append(closes[-1] * (1.002 if i % 2 == 0 else 0.998))
        closes.append(closes[-1] * 2.0)  # +100% single-candle jump
        df = _close_series_df(closes)

        result = check_outliers(df)

        assert not result.passed
        assert result.violation_count >= 1

    def test_flat_price_does_not_crash_on_zero_mad(self):
        n = OUTLIER_WINDOW + 10
        closes = [100.0] * n
        df = _close_series_df(closes)

        result = check_outliers(df)

        assert result.passed
        assert result.violation_count == 0

    def test_too_few_rows_for_window_passes(self):
        closes = [100.0, 100.5, 99.8]
        df = _close_series_df(closes)

        result = check_outliers(df)

        assert result.passed
        assert result.violation_count == 0


# ---------------------------------------------------------------------------
# check_cross_source
# ---------------------------------------------------------------------------

class TestCheckCrossSource:
    def test_one_source_empty_passes(self):
        df_a = _rows(["2024-01-01T00:00:00Z"], source="binance")
        df_b = _rows([], source="kraken")

        gaps, price = check_cross_source(df_a, df_b, "binance", "kraken", "1h")

        assert gaps.passed and gaps.violation_count == 0
        assert price.passed and price.violation_count == 0
        assert gaps.severity == WARNING
        assert price.severity == WARNING

    def test_non_overlapping_ranges_pass(self):
        df_a = _rows(["2024-01-01T00:00:00Z"], source="binance")
        df_b = _rows(["2024-02-01T00:00:00Z"], source="kraken")

        gaps, price = check_cross_source(df_a, df_b, "binance", "kraken", "1h")

        assert gaps.passed
        assert price.passed

    def test_matching_prices_within_threshold_pass(self):
        ts = ["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"]
        df_a = _rows(ts, source="binance", price=100.0)
        df_b = _rows(ts, source="kraken", price=100.3)  # ~0.3% off on close

        gaps, price = check_cross_source(df_a, df_b, "binance", "kraken", "1h")

        assert gaps.passed
        assert price.passed

    def test_price_deviation_beyond_threshold_flagged(self):
        ts = ["2024-01-01T00:00:00Z"]
        df_a = _rows(ts, source="binance", price=100.0)  # close = 101.0
        df_b = _rows(ts, source="kraken", price=90.0)    # close = 91.0, ~10% off

        gaps, price = check_cross_source(df_a, df_b, "binance", "kraken", "1h")

        assert gaps.passed  # same timestamp present in both -> not a gap
        assert not price.passed
        assert price.violation_count == 1

    def test_candle_only_in_one_source_within_overlap_is_a_gap(self):
        # kraken's own span reaches 00:00-02:00 (same as binance's), it just
        # has a hole at 01:00 — a genuine gap, not a coverage-boundary
        # difference (see test_gap_check_ignores_candles_outside_overlap_window
        # below for that distinction).
        df_a = _rows(
            ["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z", "2024-01-01T02:00:00Z"],
            source="binance",
        )
        df_b = _rows(
            ["2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z"],
            source="kraken",
        )

        gaps, price = check_cross_source(df_a, df_b, "binance", "kraken", "1h")

        assert not gaps.passed
        assert gaps.violation_count == 1
        assert "binance only" in gaps.details

    def test_gap_check_ignores_candles_outside_overlap_window(self):
        # binance has a long history; kraken only covers the tail. Earlier
        # binance-only candles must not be flagged as gaps — they're
        # outside kraken's reach by design, not missing data.
        df_a = _rows(
            ["2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z", "2024-01-03T00:00:00Z"],
            source="binance",
        )
        df_b = _rows(["2024-01-03T00:00:00Z"], source="kraken")

        gaps, price = check_cross_source(df_a, df_b, "binance", "kraken", "1h")

        assert gaps.passed
        assert gaps.violation_count == 0

    def test_still_forming_candle_excluded_from_comparison(self):
        # Two independent order books mid-candle will legitimately disagree
        # on the running price — comparing it would fire on every run for a
        # known-harmless reason.
        forming_ts = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        df_a = _rows([forming_ts.isoformat()], source="binance", price=100.0)
        df_b = _rows([forming_ts.isoformat()], source="kraken", price=50.0)  # would fail threshold if compared

        gaps, price = check_cross_source(df_a, df_b, "binance", "kraken", "1h")

        assert gaps.passed
        assert price.passed

    def test_forming_candle_excluded_but_closed_candles_still_compared(self):
        # The fix must not mask a real deviation on an already-closed candle.
        closed_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        forming_ts = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        df_a = pd.concat([
            _rows([closed_ts.isoformat()], source="binance", price=100.0),
            _rows([forming_ts.isoformat()], source="binance", price=100.0),
        ], ignore_index=True)
        df_b = pd.concat([
            _rows([closed_ts.isoformat()], source="kraken", price=90.0),   # closed, ~10% off -> real finding
            _rows([forming_ts.isoformat()], source="kraken", price=50.0),  # forming -> must be ignored
        ], ignore_index=True)

        gaps, price = check_cross_source(df_a, df_b, "binance", "kraken", "1h")

        assert not price.passed
        assert price.violation_count == 1


# ---------------------------------------------------------------------------
# run_checks / run — integration through RawStore
# ---------------------------------------------------------------------------

class TestRunChecks:
    def test_clean_historical_data_all_pass_and_freshness_skipped(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(
            _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z", "2024-01-01T02:00:00Z"]),
            asset_class="crypto",
        )

        results = run_checks(
            "BTCUSDT", "1h",
            datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
            store,
        )

        assert {r.check_name for r in results} == {
            "gaps", "duplicates", "ohlc_plausibility", "zero_volume", "outliers",
        }
        assert all(r.passed for r in results)

    def test_gap_in_stored_data_is_detected(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(_rows(["2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z"]), asset_class="crypto")

        results = run_checks(
            "BTCUSDT", "1h",
            datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
            store,
        )

        gaps_result = next(r for r in results if r.check_name == "gaps")
        assert not gaps_result.passed


class TestRun:
    def test_writes_report_file_and_returns_zero_exit_on_clean_data(self, tmp_path):
        store = RawStore(base_dir=tmp_path / "raw")
        store.write(
            _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z", "2024-01-01T02:00:00Z"]),
            asset_class="crypto",
        )
        report_dir = tmp_path / "quality"

        report_df, exit_code = run(
            ["BTCUSDT"], "1h",
            datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
            store=store, report_dir=report_dir,
        )

        assert exit_code == 0
        assert list(report_df.columns) == REPORT_COLUMNS
        written = list(report_dir.glob("*.parquet"))
        assert len(written) == 1
        assert len(pd.read_parquet(written[0])) == len(report_df)

    def test_nonzero_exit_when_hard_check_fails(self, tmp_path):
        store = RawStore(base_dir=tmp_path / "raw")
        store.write(_rows(["2024-01-01T00:00:00Z", "2024-01-01T02:00:00Z"]), asset_class="crypto")

        _, exit_code = run(
            ["BTCUSDT"], "1h",
            datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
            store=store, report_dir=tmp_path / "quality",
        )

        assert exit_code == 1

    def test_soft_check_failure_does_not_set_nonzero_exit(self, tmp_path):
        store = RawStore(base_dir=tmp_path / "raw")
        rows = _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"])
        rows.loc[0, "volume"] = 0.0  # zero_volume is WARNING-severity only

        store.write(rows, asset_class="crypto")

        _, exit_code = run(
            ["BTCUSDT"], "1h",
            datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
            store=store, report_dir=tmp_path / "quality",
        )

        assert exit_code == 0


class TestRunCrossSource:
    def test_compare_source_adds_cross_source_rows(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(
            _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"], source="binance"),
            asset_class="crypto",
        )
        store.write(
            _rows(["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"], source="kraken"),
            asset_class="crypto",
        )

        report_df, exit_code = run(
            ["BTCUSDT"], "1h",
            datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
            store=store, report_dir=tmp_path / "quality",
            source="binance", compare_source="kraken",
        )

        cross_rows = report_df[report_df["source"] == "binance+kraken"]
        assert set(cross_rows["check_name"]) == {"cross_source_gaps", "cross_source_price"}
        assert (cross_rows["severity"] == WARNING).all()
        assert exit_code == 0  # WARNING-severity only, never blocks

    def test_no_cross_source_rows_when_compare_source_not_set(self, tmp_path):
        store = RawStore(base_dir=tmp_path)
        store.write(_rows(["2024-01-01T00:00:00Z"], source="binance"), asset_class="crypto")

        report_df, _ = run(
            ["BTCUSDT"], "1h",
            datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
            store=store, report_dir=tmp_path / "quality",
        )

        assert not any("+" in s for s in report_df["source"].unique())


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

class TestArgParsing:
    def test_default_symbols_from_config(self):
        args = build_parser().parse_args(["--interval", "1h"])
        assert args.symbols == INGESTION_SYMBOLS

    def test_interval_is_required(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--symbol", "BTCUSDT"])

    def test_start_and_end_default_to_none(self):
        args = build_parser().parse_args(["--interval", "1h"])
        assert args.start is None
        assert args.end is None

    def test_start_parses_date_only_as_utc(self):
        args = build_parser().parse_args(["--interval", "1h", "--start", "2024-01-01"])
        assert args.start == datetime(2024, 1, 1, tzinfo=timezone.utc)

    def test_source_defaults_to_ingestion_source(self):
        args = build_parser().parse_args(["--interval", "1h"])
        assert args.source == INGESTION_SOURCE

    def test_source_can_be_overridden(self):
        args = build_parser().parse_args(["--interval", "1h", "--source", "kraken"])
        assert args.source == "kraken"

    def test_compare_source_defaults_to_none(self):
        args = build_parser().parse_args(["--interval", "1h"])
        assert args.compare_source is None

    def test_compare_source_can_be_set(self):
        args = build_parser().parse_args(["--interval", "1h", "--compare-source", "kraken"])
        assert args.compare_source == "kraken"
