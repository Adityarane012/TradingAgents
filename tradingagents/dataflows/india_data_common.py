"""Shared plumbing for the India data sources (NSE, and the RBI/screener.in
fetchers that follow it).

None of these sources has an official, versioned API: NSE serves the JSON its
own website uses, RBI and screener.in are HTML pages. They can change shape,
rate-limit, or block without notice, and a number that was fetched but is
wrong is worse than a number that is missing. So this module holds the pieces
that make each fetcher fail *loudly and specifically* instead of quietly
returning something plausible:

- two error types that separate "could not obtain it" from "obtained it but it
  failed a sanity check", both ``VendorError`` so they slot into the existing
  routing taxonomy (``errors.py``);
- look-ahead and freshness checks against the analysis date, so a snapshot
  dated after ``trade_date`` can never leak into a historical run (#1220);
- a per-host throttle, a TTL cache and a circuit breaker, so a 50-ticker batch
  neither hammers a host nor keeps retrying one that is blocking us.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from .config import get_config
from .errors import VendorError
from .utils import get_current_date

# NSE/RBI publish in Indian Standard Time; comparing on the IST calendar day
# (not UTC) is what makes "published on or before the trade date" exact.
IST = timezone(timedelta(hours=5, minutes=30))

# A live snapshot older than this (relative to the trade date) is treated as
# failed rather than served: 5 days spans a weekend plus a market holiday.
DEFAULT_MAX_STALE_DAYS = 5


class IndiaDataError(VendorError):
    """Base for India-source failures; carries the source label and a reason."""

    def __init__(self, source: str, reason: str):
        self.source = source
        self.reason = reason
        super().__init__(f"{source}: {reason}")


class IndiaSourceUnavailable(IndiaDataError):
    """The data could not be obtained or may not be served (network error, HTTP
    block, open circuit breaker, a snapshot dated after the trade date)."""


class IndiaDataInvalid(IndiaDataError):
    """The data was fetched but failed a sanity check or has an unexpected
    shape — treated as a failed fetch, never passed on as a value."""


def sentinel(label: str, reason: object) -> str:
    """The placeholder string served in place of data that could not be
    produced. Same convention as the Reddit/India-news fetchers: a failure is
    never worded as an absence of data."""
    return f"<{label} unavailable: {reason}; this is not an absence of data>"


# --- dates -----------------------------------------------------------------


def today() -> date:
    return datetime.strptime(get_current_date(), "%Y-%m-%d").date()


def resolve_curr_date(curr_date: str | date | None) -> date:
    """The analysis date as a ``date``; ``None`` means a live run (today)."""
    if curr_date is None:
        return today()
    if isinstance(curr_date, datetime):
        return curr_date.date()
    if isinstance(curr_date, date):
        return curr_date
    return datetime.strptime(curr_date, "%Y-%m-%d").date()


def max_stale_days() -> int:
    return int(get_config().get("india_max_staleness_days", DEFAULT_MAX_STALE_DAYS))


def check_not_after(source: str, what: str, as_of: date, curr: date) -> None:
    """Refuse a snapshot dated after the trade date: these endpoints only serve
    'now', and serving a later value into a past run leaks post-decision data."""
    if as_of > curr:
        raise IndiaSourceUnavailable(
            source,
            f"{what} is dated {as_of.isoformat()}, after the trade date "
            f"{curr.isoformat()}; serving it would leak post-decision data",
        )


def check_fresh(
    source: str, what: str, as_of: date, curr: date, max_days: int | None = None
) -> None:
    """Refuse a snapshot too far *before* the trade date to describe it."""
    limit = max_stale_days() if max_days is None else max_days
    age = (curr - as_of).days
    if age > limit:
        raise IndiaDataInvalid(
            source,
            f"{what} is stale: dated {as_of.isoformat()}, {age} days before the "
            f"trade date {curr.isoformat()} (limit {limit})",
        )


def published_by(moment: datetime | date, curr: date) -> bool:
    """Whether something published at ``moment`` was public on ``curr`` (IST
    calendar day). A naive datetime is taken to be IST."""
    if isinstance(moment, datetime):
        moment = (moment.replace(tzinfo=IST) if moment.tzinfo is None else moment).astimezone(IST)
        return moment.date() <= curr
    return moment <= curr


# --- parsing / validation ---------------------------------------------------


def to_float(value: object) -> float | None:
    """A number from an API string (``"1,234.50"``), or ``None`` for blank /
    dash / non-numeric input."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "--", "NA", "N/A", "null"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


@contextmanager
def schema_guard(source: str, what: str) -> Iterator[None]:
    """Turn a parse blow-up (missing key, wrong type, bad number) into
    ``IndiaDataInvalid`` with a message naming the likely cause: the upstream
    API changed shape. Our own ``IndiaDataError`` passes through untouched."""
    try:
        yield
    except IndiaDataError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise IndiaDataInvalid(
            source,
            f"unexpected {what} response shape ({type(exc).__name__}: {exc}); "
            f"the upstream API may have changed",
        ) from exc


# --- throttle / cache / breaker ---------------------------------------------


class Throttle:
    """Minimum spacing between calls to one host, shared across threads."""

    def __init__(
        self,
        min_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._min = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            delay = self._next - self._clock()
            if delay > 0:
                self._sleep(delay)
            self._next = self._clock() + self._min


class TTLCache:
    """Small in-memory cache with per-cache TTL and a size cap. Only ever fed
    successful results, so a failure is retried rather than remembered."""

    def __init__(
        self,
        ttl_seconds: float,
        *,
        max_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[float, object]] = OrderedDict()

    def get(self, key: str, default: object = None) -> object:
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                return default
            stored_at, value = entry
            if self._clock() - stored_at >= self._ttl:
                del self._items[key]
                return default
            return value

    def set(self, key: str, value: object) -> None:
        with self._lock:
            self._items[key] = (self._clock(), value)
            self._items.move_to_end(key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class CircuitBreaker:
    """Stop calling a host that keeps failing.

    After ``threshold`` consecutive failures the breaker opens and ``allow()``
    is False for ``cooldown`` seconds; then one trial call is let through
    (half-open) — a success closes it, another failure reopens it at once.
    """

    def __init__(
        self,
        threshold: int = 3,
        cooldown: float = 300.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._threshold = threshold
        self._cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None

    def allow(self) -> bool:
        with self._lock:
            if self._opened_at is None:
                return True
            return self._clock() - self._opened_at >= self._cooldown

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._opened_at = self._clock()

    def reset(self) -> None:
        self.record_success()
