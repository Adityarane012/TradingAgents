"""Tests for the shared India-data plumbing (india_data_common)."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch

import pytest

from tradingagents.dataflows import india_data_common as common
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.india_data_common import (
    CircuitBreaker,
    IndiaDataError,
    IndiaDataInvalid,
    IndiaSourceUnavailable,
    Throttle,
    TTLCache,
)


class FakeClock:
    """Deterministic clock whose ``sleep`` advances it, so throttling can be
    asserted without waiting."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class TestErrors:
    def test_both_types_are_vendor_errors(self):
        assert issubclass(IndiaSourceUnavailable, IndiaDataError)
        assert issubclass(IndiaDataInvalid, IndiaDataError)
        assert issubclass(IndiaDataError, VendorError)

    def test_message_carries_source_and_reason(self):
        exc = IndiaSourceUnavailable("NSE", "HTTP 403")
        assert exc.source == "NSE"
        assert exc.reason == "HTTP 403"
        assert str(exc) == "NSE: HTTP 403"

    def test_sentinel_never_claims_absence(self):
        text = common.sentinel("NSE FII/DII flows", "HTTP 403")
        assert text.startswith("<NSE FII/DII flows unavailable: HTTP 403")
        assert "not an absence of data" in text


class TestDates:
    def test_none_means_today(self):
        with patch.object(common, "get_current_date", return_value="2026-09-20"):
            assert common.resolve_curr_date(None) == date(2026, 9, 20)

    def test_string_and_date_inputs(self):
        assert common.resolve_curr_date("2026-09-18") == date(2026, 9, 18)
        assert common.resolve_curr_date(date(2026, 9, 18)) == date(2026, 9, 18)
        assert common.resolve_curr_date(datetime(2026, 9, 18, 15, 30)) == date(2026, 9, 18)

    def test_bad_string_raises(self):
        with pytest.raises(ValueError):
            common.resolve_curr_date("18-09-2026")

    def test_snapshot_dated_after_trade_date_is_refused(self):
        with pytest.raises(IndiaSourceUnavailable, match="leak post-decision data"):
            common.check_not_after("NSE", "FII/DII flows", date(2026, 9, 18), date(2026, 9, 17))

    def test_snapshot_on_trade_date_is_allowed(self):
        common.check_not_after("NSE", "FII/DII flows", date(2026, 9, 18), date(2026, 9, 18))

    def test_stale_snapshot_is_invalid(self):
        with pytest.raises(IndiaDataInvalid, match="stale"):
            common.check_fresh("NSE", "flows", date(2026, 9, 1), date(2026, 9, 18), max_days=5)

    def test_staleness_boundary_is_inclusive(self):
        common.check_fresh("NSE", "flows", date(2026, 9, 13), date(2026, 9, 18), max_days=5)

    def test_staleness_limit_comes_from_config(self):
        config = {"india_max_staleness_days": 1}
        with patch.object(common, "get_config", return_value=config), pytest.raises(
            IndiaDataInvalid
        ):
            common.check_fresh("NSE", "flows", date(2026, 9, 16), date(2026, 9, 18))

    def test_staleness_default_when_config_key_absent(self):
        assert common.max_stale_days() == common.DEFAULT_MAX_STALE_DAYS

    def test_published_by_uses_the_ist_calendar_day(self):
        # 00:30 IST on the 19th is 19:00 UTC on the 18th — but it is the 19th
        # in India, so it must NOT be visible to a run dated the 18th.
        late = datetime(2026, 9, 19, 0, 30, tzinfo=common.IST)
        assert common.published_by(late, date(2026, 9, 18)) is False
        assert common.published_by(late, date(2026, 9, 19)) is True

    def test_published_by_treats_naive_datetimes_as_ist(self):
        assert common.published_by(datetime(2026, 9, 18, 23, 59), date(2026, 9, 18)) is True
        assert common.published_by(datetime(2026, 9, 19, 0, 1), date(2026, 9, 18)) is False

    def test_published_by_accepts_plain_dates(self):
        assert common.published_by(date(2026, 9, 18), date(2026, 9, 18)) is True
        assert common.published_by(date(2026, 9, 19), date(2026, 9, 18)) is False


class TestToFloat:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("1,234.50", 1234.5),
            ("  50.48 ", 50.48),
            (12, 12.0),
            (11.39, 11.39),
            ("-17.5", -17.5),
        ],
    )
    def test_parses_numbers(self, raw, expected):
        assert common.to_float(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "-", "--", "NA", "N/A", "null", "abc", True])
    def test_blank_and_junk_are_none(self, raw):
        assert common.to_float(raw) is None


class TestSchemaGuard:
    def test_missing_key_becomes_invalid_with_hint(self):
        row: dict = {}
        with pytest.raises(
            IndiaDataInvalid, match="upstream API may have changed"
        ), common.schema_guard("NSE", "FII/DII"):
            row["category"]

    @pytest.mark.parametrize(
        "exc", [IndexError("x"), TypeError("x"), ValueError("x"), AttributeError("x")]
    )
    def test_other_parse_errors_are_wrapped(self, exc):
        with pytest.raises(IndiaDataInvalid), common.schema_guard("NSE", "thing"):
            raise exc

    def test_our_own_errors_pass_through_unchanged(self):
        with pytest.raises(IndiaSourceUnavailable), common.schema_guard("NSE", "thing"):
            raise IndiaSourceUnavailable("NSE", "blocked")

    def test_unrelated_errors_are_not_swallowed(self):
        with pytest.raises(RuntimeError), common.schema_guard("NSE", "thing"):
            raise RuntimeError("bug")


class TestThrottle:
    def test_first_call_does_not_wait(self):
        fc = FakeClock()
        Throttle(0.5, clock=fc.clock, sleep=fc.sleep).wait()
        assert fc.slept == []

    def test_back_to_back_calls_are_spaced(self):
        fc = FakeClock()
        t = Throttle(0.5, clock=fc.clock, sleep=fc.sleep)
        t.wait()
        t.wait()
        assert fc.slept == [pytest.approx(0.5)]

    def test_no_wait_once_interval_has_passed(self):
        fc = FakeClock()
        t = Throttle(0.5, clock=fc.clock, sleep=fc.sleep)
        t.wait()
        fc.now += 2.0
        t.wait()
        assert fc.slept == []

    def test_zero_interval_never_sleeps(self):
        fc = FakeClock()
        t = Throttle(0.0, clock=fc.clock, sleep=fc.sleep)
        for _ in range(5):
            t.wait()
        assert fc.slept == []


class TestTTLCache:
    def test_hit_then_expiry(self):
        fc = FakeClock()
        cache = TTLCache(60, clock=fc.clock)
        cache.set("k", "v")
        assert cache.get("k") == "v"
        fc.now += 59
        assert cache.get("k") == "v"
        fc.now += 2
        assert cache.get("k") is None

    def test_default_for_missing(self):
        assert TTLCache(60).get("nope", "fallback") == "fallback"

    def test_oldest_entry_evicted_past_the_cap(self):
        cache = TTLCache(60, max_entries=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)
        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.get("c") == 3

    def test_clear(self):
        cache = TTLCache(60)
        cache.set("a", 1)
        cache.clear()
        assert cache.get("a") is None


class TestCircuitBreaker:
    def test_closed_until_threshold(self):
        fc = FakeClock()
        cb = CircuitBreaker(3, 300, clock=fc.clock)
        cb.record_failure()
        cb.record_failure()
        assert cb.allow() is True
        cb.record_failure()
        assert cb.allow() is False

    def test_success_resets_the_count(self):
        cb = CircuitBreaker(3, 300)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        assert cb.allow() is True

    def test_half_open_after_cooldown(self):
        fc = FakeClock()
        cb = CircuitBreaker(2, 300, clock=fc.clock)
        cb.record_failure()
        cb.record_failure()
        assert cb.allow() is False
        fc.now += 301
        assert cb.allow() is True

    def test_failure_in_half_open_reopens_immediately(self):
        fc = FakeClock()
        cb = CircuitBreaker(2, 300, clock=fc.clock)
        cb.record_failure()
        cb.record_failure()
        fc.now += 301
        assert cb.allow() is True
        cb.record_failure()
        assert cb.allow() is False

    def test_success_in_half_open_closes(self):
        fc = FakeClock()
        cb = CircuitBreaker(2, 300, clock=fc.clock)
        cb.record_failure()
        cb.record_failure()
        fc.now += 301
        cb.record_success()
        cb.record_failure()
        assert cb.allow() is True

    def test_reset(self):
        cb = CircuitBreaker(1, 300)
        cb.record_failure()
        assert cb.allow() is False
        cb.reset()
        assert cb.allow() is True
