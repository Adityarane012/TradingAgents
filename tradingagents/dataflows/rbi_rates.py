"""RBI policy rates and reserve ratios, scraped from the RBI homepage.

**Why scraping, and why not FRED.** FRED's India "discount rate" series
(``INTDSRINM193N``) stopped updating in July 2022 — it still serves 5.15%
while the actual policy repo rate is 5.25%, and an analyst told "the RBI rate
is 5.15%" is simply being misinformed. FRED has no live India policy-rate
series at all. RBI's own DBIE portal (``dbie.rbi.org.in``) presents a broken
TLS certificate, and its replacement (``data.rbi.org.in``) is a JavaScript
application with no documented API. What is left, and what actually works, is
the "Current Rates" box on rbi.org.in's homepage: plain server-rendered HTML,
no key, structured as ``<th>label</th><td>: value</td>`` pairs.

**These figures are live-only.** Unlike every NSE endpoint, the policy-rate box
carries no as-of date — the page states a current value with no vintage. RBI
changes rates at MPC meetings roughly every two months, so pasting today's repo
rate into a run dated months back could assert a rate that was not in force
then. So a historical run is refused outright rather than served a plausible
wrong number. This is the same rule ``date_window.withhold_live_profile``
applies to vendor company profiles, for the same reason.

Because it is a scrape, the parser keys off the row labels rather than their
position, and fails loudly (``IndiaDataInvalid``) if an expected label is
missing — a redesigned page must not silently yield a partial rate set.
"""

from __future__ import annotations

import http.client
import logging
from dataclasses import dataclass
from datetime import date
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from parsel import Selector

from .errors import VendorError
from .india_data_common import (
    CircuitBreaker,
    IndiaDataInvalid,
    IndiaSourceUnavailable,
    Throttle,
    TTLCache,
    resolve_curr_date,
    schema_guard,
    sentinel,
    to_float,
    today,
)

logger = logging.getLogger(__name__)

_SRC = "RBI"
_URL = "https://www.rbi.org.in/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT = 15.0
_MAX_BYTES = 5 * 1024 * 1024
# Policy rates move a handful of times a year; one fetch per run is plenty.
_CACHE_TTL = 3600.0

_THROTTLE = Throttle(1.0)
_BREAKER = CircuitBreaker(threshold=3, cooldown=300.0)
_CACHE = TTLCache(_CACHE_TTL, max_entries=4)

# Row labels as they appear on the page, mapped to our field names. Every one
# of these must be present: a page that no longer carries the repo rate is a
# page we do not understand, not a page with a missing rate.
_REQUIRED = {
    "policy repo rate": "repo",
    "standing deposit facility rate": "sdf",
    "marginal standing facility rate": "msf",
    "bank rate": "bank_rate",
    "crr": "crr",
    "slr": "slr",
}
# Present today but not worth failing the whole fetch over.
_OPTIONAL = {"fixed reverse repo rate": "reverse_repo"}

# A policy rate outside this band means we parsed the wrong thing (a share
# price, an index level) rather than that India has 40% rates.
_RATE_BAND = (0.0, 25.0)


def reset_state() -> None:
    """Forget the cached page and breaker state (tests, long-running sessions)."""
    _BREAKER.reset()
    _CACHE.clear()


@dataclass(frozen=True)
class PolicyRates:
    """RBI's current policy rates and reserve ratios, in percent."""

    repo: float
    sdf: float
    msf: float
    bank_rate: float
    crr: float
    slr: float
    reverse_repo: float | None = None


def _fetch_page() -> str:
    cached = _CACHE.get(_URL)
    if cached is not None:
        return cached

    if not _BREAKER.allow():
        raise IndiaSourceUnavailable(
            _SRC, "circuit breaker open after repeated failures; will retry later"
        )

    _THROTTLE.wait()
    req = Request(_URL, headers={"User-Agent": _UA, "Accept": "text/html,*/*"})
    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read(_MAX_BYTES + 1)
    except HTTPError as exc:
        _BREAKER.record_failure()
        raise IndiaSourceUnavailable(_SRC, f"HTTP {exc.code}") from exc
    except (OSError, http.client.HTTPException) as exc:
        _BREAKER.record_failure()
        raise IndiaSourceUnavailable(_SRC, f"{type(exc).__name__}: {exc}") from exc

    if len(raw) > _MAX_BYTES:
        _BREAKER.record_failure()
        raise IndiaSourceUnavailable(_SRC, f"page exceeded {_MAX_BYTES} bytes; refusing to parse")

    _BREAKER.record_success()
    html = raw.decode("utf-8", "replace")
    _CACHE.set(_URL, html)
    return html


def _parse_percent(raw: str) -> float | None:
    """``': 5.25%'`` -> ``5.25``. Ranges (``'8.40% - 10.00%'``) and footnote
    markers are rejected: a policy rate is a single number, and anything else
    means we matched a row that is not one."""
    text = raw.replace(":", " ").replace("%", " ").strip()
    if "-" in text or "*" in text or "#" in text:
        return None
    return to_float(text)


def get_policy_rates(curr_date: str | date | None = None) -> PolicyRates:
    """Current RBI policy rates. Refuses any run dated before today.

    Validated: every required label present, each value a plain percentage in
    a plausible band, and the LAF corridor ordered SDF <= repo <= MSF — which
    is how the corridor is constructed, so a violation means a misparse.
    """
    curr = resolve_curr_date(curr_date)
    if curr < today():
        raise IndiaSourceUnavailable(
            _SRC,
            f"the RBI rates box states current values with no as-of date, so it "
            f"cannot be served into a run dated {curr.isoformat()}; rates change "
            f"at MPC meetings and today's value may not have been in force then",
        )

    html = _fetch_page()
    with schema_guard(_SRC, "policy rates"):
        rows: dict[str, float] = {}
        for tr in Selector(html).css("#wrapper tr"):
            label = " ".join("".join(tr.css("th ::text").getall()).split()).lower()
            key = _REQUIRED.get(label) or _OPTIONAL.get(label)
            if key is None or key in rows:
                continue
            value = _parse_percent("".join(tr.css("td ::text").getall()))
            if value is not None:
                rows[key] = value

    missing = sorted(set(_REQUIRED.values()) - set(rows))
    if missing:
        raise IndiaDataInvalid(
            _SRC,
            f"the homepage rates box is missing {', '.join(missing)}; "
            f"the page layout may have changed",
        )
    out_of_band = {k: v for k, v in rows.items() if not _RATE_BAND[0] <= v <= _RATE_BAND[1]}
    if out_of_band:
        raise IndiaDataInvalid(_SRC, f"implausible rate value(s): {out_of_band}")
    if not rows["sdf"] <= rows["repo"] <= rows["msf"]:
        raise IndiaDataInvalid(
            _SRC,
            f"LAF corridor is inverted (SDF {rows['sdf']}, repo {rows['repo']}, "
            f"MSF {rows['msf']}); the labels were probably misread",
        )
    return PolicyRates(**rows)


def policy_rates_block(curr_date: str | date | None = None) -> str:
    """The prompt block. Never raises: a failure becomes a sentinel."""
    try:
        r = get_policy_rates(curr_date)
    except VendorError as exc:
        reason = getattr(exc, "reason", exc)
        logger.warning("RBI policy rates unavailable: %s", exc)
        return sentinel("RBI policy rates", reason)

    reverse = f", fixed reverse repo {r.reverse_repo:.2f}%" if r.reverse_repo is not None else ""
    return (
        f"RBI policy rates (current, from rbi.org.in — this source states today's "
        f"values without an as-of date, so treat them as current and do not infer "
        f"when they were last changed): repo {r.repo:.2f}%, standing deposit "
        f"facility {r.sdf:.2f}%, marginal standing facility {r.msf:.2f}%, bank rate "
        f"{r.bank_rate:.2f}%{reverse}; CRR {r.crr:.2f}%, SLR {r.slr:.2f}%."
    )
