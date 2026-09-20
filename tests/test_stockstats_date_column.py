"""Tests for tolerating a non-`Date` index column in stockstats_utils (#890).

Guards against a download frame whose date column is `index` or `Datetime`
instead of `Date`, which would otherwise silently drop every indicator.
"""

from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.dataflows import stockstats_utils as su


def _ohlcv(date_col: str) -> pd.DataFrame:
    """OHLCV frame whose date column is named `date_col`."""
    dates = pd.bdate_range("2026-04-01", periods=10)
    return pd.DataFrame({
        date_col: dates,
        "Open": [100.0 + i for i in range(10)],
        "High": [101.0 + i for i in range(10)],
        "Low": [99.0 + i for i in range(10)],
        "Close": [100.5 + i for i in range(10)],
        "Volume": [1_000_000 + i for i in range(10)],
    })


@pytest.mark.unit
class TestEnsureDateColumn:
    def test_renames_index_column(self):
        out = su._ensure_date_column(_ohlcv("index"))
        assert "Date" in out.columns and "index" not in out.columns

    def test_renames_datetime_and_date_variants(self):
        assert "Date" in su._ensure_date_column(_ohlcv("Datetime")).columns
        assert "Date" in su._ensure_date_column(_ohlcv("date")).columns

    def test_leaves_existing_date_untouched(self):
        df = _ohlcv("Date")
        assert su._ensure_date_column(df) is df  # no-op short-circuit

    def test_no_datelike_column_is_left_alone(self):
        df = pd.DataFrame({"Close": [1, 2, 3]})
        out = su._ensure_date_column(df)
        assert "Date" not in out.columns  # nothing to rename; caller handles


@pytest.mark.unit
class TestCleanDataframeAcrossVersions:
    def test_clean_handles_index_column(self):
        """A frame with `index` instead of `Date` must still clean to a
        usable, date-parsed frame (was KeyError: 'Date')."""
        cleaned = su._clean_dataframe(_ohlcv("index"))
        assert "Date" in cleaned.columns
        assert pd.api.types.is_datetime64_any_dtype(cleaned["Date"])
        assert len(cleaned) == 10

    def test_clean_handles_legacy_date_column(self):
        cleaned = su._clean_dataframe(_ohlcv("Date"))
        assert len(cleaned) == 10

    def test_indicators_compute_after_index_rename(self):
        """stockstats must compute indicators on a frame whose date column
        arrived as `index`, instead of erroring per indicator."""
        from stockstats import wrap
        cleaned = su._clean_dataframe(_ohlcv("index"))
        df = wrap(cleaned)
        df["close_5_sma"]  # triggers calculation
        assert "close_5_sma" in df.columns
        assert df["close_5_sma"].notna().any()


class TestDamagedDateRowsAreDropped:
    """A truncated date must cost one bar, not the whole ticker.

    Found in a live batch run: a cache CSV row for TCS.NS had lost the leading
    "202" of its date, leaving "9-17". pandas parses that without complaint as
    year 1, and the failure only surfaces later when the column is cast to
    datetime64[ns] — an OutOfBoundsDatetime that aborted the entire ticker.
    The damage came from an interrupted write, so any crash can reproduce it.
    """

    @pytest.mark.parametrize("raw", ["9-17", "0001-09-17", "1-1"])
    def test_implausible_years_become_nat(self, raw):
        assert pd.isna(su._local_midnight(raw))

    @pytest.mark.parametrize("raw", ["2026-09-17", "1999-01-04"])
    def test_real_dates_still_parse(self, raw):
        assert su._local_midnight(raw) == pd.Timestamp(raw)

    def test_unparseable_values_still_become_nat(self):
        assert pd.isna(su._local_midnight("not-a-date"))
        assert pd.isna(su._local_midnight(None))

    def test_a_damaged_row_is_dropped_and_the_rest_survive(self):
        frame = pd.DataFrame(
            {
                "Date": ["2026-09-15", "9-17", "2026-09-18"],
                "Open": [1.0, 2.0, 3.0],
                "High": [1.0, 2.0, 3.0],
                "Low": [1.0, 2.0, 3.0],
                "Close": [1.0, 2.0, 3.0],
                "Volume": [10, 20, 30],
            }
        )
        cleaned = su._clean_dataframe(frame)
        assert len(cleaned) == 2
        assert list(cleaned["Date"].dt.strftime("%Y-%m-%d")) == ["2026-09-15", "2026-09-18"]

    def test_the_whole_column_casts_without_overflow(self):
        """The actual failure mode: the cast, not the parse."""
        series = su._normalize_dates(["2026-09-15", "9-17", "2026-09-18"])
        assert str(series.dtype).startswith("datetime64")
        assert series.isna().sum() == 1
