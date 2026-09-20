"""screener.in company data: the FII/DII ownership split and headline ratios.

**Why this exists alongside the NSE fetcher.** NSE's shareholding-pattern
endpoint reports promoter vs public as a single split — it does not break the
public half into foreign (FII) and domestic (DII) institutions. That split is
one of the most-watched signals for an Indian company (Reliance's FII holding
fell from 22.60% to 17.19% over twelve quarters while DII rose from 15.99% to
21.10% — a story neither the aggregate nor the price shows), and it is what
the fundamentals prompt otherwise has to declare unavailable. screener.in
publishes it, server-rendered, alongside ROCE/ROE/P-E and the shareholder
count.

**Opt-in, and off by default** (``screener_enabled``). screener.in is a
third-party site with no API, and its terms grant personal, non-commercial
viewing. This module is written for personal/testing use of the project: one
polite request per company, throttled, cached for the session, with an
identified User-Agent rather than a spoofed browser one. It is not switched on
for anyone by default, and NSE remains the primary source for everything both
of them carry.

**Live-only.** The shareholding columns are labelled by quarter end (``Jun
2026``), not by the date the filing became public, and the ratios are current
by definition. Without a publication date there is no way to know what was
public on a past date, so a historical run is refused rather than served
figures that may postdate it. NSE's fetcher, which does carry filing dates,
handles the point-in-time case for promoter holding.

**Cross-checked, not trusted.** Where NSE covers the same number (promoter
holding), the block reports a disagreement rather than silently preferring
one: on 2026-09-20 both reported Reliance at 50.48%.
"""

from __future__ import annotations

import http.client
import logging
from dataclasses import dataclass, field
from datetime import date
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from parsel import Selector

from .config import get_config
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
from .nse_india import nse_symbol

logger = logging.getLogger(__name__)

_SRC = "screener.in"
_URL = "https://www.screener.in/company/{symbol}/consolidated/"
_FALLBACK_URL = "https://www.screener.in/company/{symbol}/"
# Identified rather than spoofed: this is a personal-use fetch and there is no
# reason to pretend otherwise (screener serves it fine, unlike NSE).
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
_TIMEOUT = 20.0
_MAX_BYTES = 8 * 1024 * 1024
_CACHE_TTL = 43200.0  # 12h: this data changes quarterly at most

# One request at a time, spaced — a batch run must not hammer a free site.
_THROTTLE = Throttle(3.0)
_BREAKER = CircuitBreaker(threshold=3, cooldown=600.0)
_CACHE = TTLCache(_CACHE_TTL, max_entries=128)

# Shareholding row labels, as rendered ("Promoters +"), mapped to our keys.
_HOLDER_ROWS = {
    "promoters": "promoters",
    "fiis": "fii",
    "diis": "dii",
    "government": "government",
    "public": "public",
}
# The headline ratios worth carrying. Keyed by screener's own label.
_RATIOS = ("Market Cap", "Stock P/E", "Book Value", "Dividend Yield", "ROCE", "ROE")

_HOLDING_SUM_TOLERANCE = 1.5  # pp; screener rounds each row to 2dp
_QUARTERS = 8

_notice_logged = False


def reset_state() -> None:
    """Forget cached pages, breaker state and the one-time notice."""
    global _notice_logged
    _BREAKER.reset()
    _CACHE.clear()
    _notice_logged = False


def screener_enabled() -> bool:
    """Off unless explicitly switched on (see the module docstring)."""
    return bool(get_config().get("screener_enabled", False))


@dataclass(frozen=True)
class QuarterHolding:
    """One quarter's ownership split, in percent."""

    quarter: str  # as screener labels it, e.g. "Jun 2026"
    promoters: float
    fii: float
    dii: float
    public: float
    government: float = 0.0


@dataclass(frozen=True)
class CompanySnapshot:
    ratios: dict[str, str]
    holdings: list[QuarterHolding] = field(default_factory=list)
    shareholders: str | None = None


def _fetch(symbol: str) -> str:
    cached = _CACHE.get(symbol)
    if cached is not None:
        return cached

    if not _BREAKER.allow():
        raise IndiaSourceUnavailable(
            _SRC, "circuit breaker open after repeated failures; will retry later"
        )

    global _notice_logged
    if not _notice_logged:
        logger.info(
            "screener.in is enabled. Its terms allow personal, non-commercial use; "
            "requests are throttled and cached. Disable with screener_enabled=False."
        )
        _notice_logged = True

    last_exc: Exception | None = None
    for template in (_URL, _FALLBACK_URL):
        _THROTTLE.wait()
        req = Request(
            template.format(symbol=symbol),
            headers={"User-Agent": _UA, "Accept": "text/html,*/*"},
        )
        try:
            with urlopen(req, timeout=_TIMEOUT) as resp:
                raw = resp.read(_MAX_BYTES + 1)
        except HTTPError as exc:
            last_exc = exc
            if exc.code == 404:
                # Consolidated statements don't exist for every company; the
                # standalone page is the documented fallback. Any other 404
                # means the symbol isn't on screener.
                continue
            _BREAKER.record_failure()
            raise IndiaSourceUnavailable(_SRC, f"HTTP {exc.code}") from exc
        except (OSError, http.client.HTTPException) as exc:
            _BREAKER.record_failure()
            raise IndiaSourceUnavailable(_SRC, f"{type(exc).__name__}: {exc}") from exc

        if len(raw) > _MAX_BYTES:
            _BREAKER.record_failure()
            raise IndiaSourceUnavailable(_SRC, f"page exceeded {_MAX_BYTES} bytes")

        _BREAKER.record_success()
        html = raw.decode("utf-8", "replace")
        _CACHE.set(symbol, html)
        return html

    _BREAKER.record_failure()
    raise IndiaSourceUnavailable(_SRC, f"no page for {symbol} (HTTP 404)") from last_exc


def _parse_ratios(sel: Selector) -> dict[str, str]:
    out: dict[str, str] = {}
    for li in sel.css("#top-ratios li"):
        name = " ".join("".join(li.css(".name ::text").getall()).split())
        value = " ".join("".join(li.css(".value ::text").getall()).split())
        if name in _RATIOS and value:
            out[name] = value
    return out


def _parse_holdings(sel: Selector) -> tuple[list[QuarterHolding], str | None]:
    """The quarterly ownership table (the first one under #shareholding; the
    second is the yearly view of the same data)."""
    tables = sel.css("#shareholding table")
    if not tables:
        return [], None
    table = tables[0]
    quarters = [" ".join(t.split()) for t in table.css("thead th ::text").getall() if t.strip()]

    rows: dict[str, list[float | None]] = {}
    shareholders: str | None = None
    for tr in table.css("tbody tr"):
        cells = [" ".join("".join(td.css("::text").getall()).split()) for td in tr.css("td")]
        if not cells:
            continue
        label = cells[0].rstrip(" +").strip().lower()
        if "shareholder" in label:
            shareholders = cells[-1] or None
            continue
        key = _HOLDER_ROWS.get(label)
        if key:
            rows[key] = [to_float(c.replace("%", "")) for c in cells[1:]]

    # A company with no promoter (HDFC Bank, ITC, L&T and other professionally
    # managed names) has no "Promoters" row at all on screener.in — the row is
    # omitted rather than shown as 0%. Requiring it rejected those companies
    # outright, which is the same mistake the NSE fetcher already had to fix.
    # Only the institutional split is genuinely required here, since that is
    # the reason this source exists.
    if "promoters" not in rows:
        rows["promoters"] = [0.0] * len(quarters)
    missing = {"fii", "dii", "public"} - set(rows)
    if missing:
        raise IndiaDataInvalid(
            _SRC,
            f"shareholding table is missing {', '.join(sorted(missing))}; "
            f"the page layout may have changed",
        )

    holdings: list[QuarterHolding] = []
    for i, quarter in enumerate(quarters):
        try:
            values = {k: rows[k][i] for k in ("promoters", "fii", "dii", "public")}
            values["government"] = (rows.get("government") or [])[i] if rows.get("government") else 0.0
        except IndexError:
            continue  # ragged row: skip this column rather than mis-align it
        if any(v is None for v in values.values()):
            continue
        total = sum(values.values())  # type: ignore[arg-type]
        if abs(total - 100.0) > _HOLDING_SUM_TOLERANCE:
            logger.warning("screener.in: %s holdings sum to %.2f%%, skipping", quarter, total)
            continue
        holdings.append(QuarterHolding(quarter=quarter, **values))  # type: ignore[arg-type]

    if not holdings:
        raise IndiaDataInvalid(_SRC, "no shareholding column passed validation")
    return holdings, shareholders


def get_snapshot(ticker: str, curr_date: str | date | None = None) -> CompanySnapshot:
    """Ratios and the ownership split for ``ticker``. Live runs only."""
    if not screener_enabled():
        raise IndiaSourceUnavailable(_SRC, "disabled (screener_enabled=False)")
    symbol = nse_symbol(ticker)  # .BO is refused here, as in nse_india
    curr = resolve_curr_date(curr_date)
    if curr < today():
        raise IndiaSourceUnavailable(
            _SRC,
            f"screener.in shows current values and labels shareholding by quarter "
            f"end rather than filing date, so it cannot be served into a run dated "
            f"{curr.isoformat()} without risking figures that were not yet public",
        )

    sel = Selector(_fetch(symbol))
    with schema_guard(_SRC, "company page"):
        ratios = _parse_ratios(sel)
        holdings, shareholders = _parse_holdings(sel)
    if not ratios:
        raise IndiaDataInvalid(_SRC, "no headline ratios found; the page layout may have changed")
    return CompanySnapshot(ratios=ratios, holdings=holdings[-_QUARTERS:], shareholders=shareholders)


def ownership_split_block(
    ticker: str, curr_date: str | date | None = None, nse_promoter_pct: float | None = None
) -> str:
    """The prompt block. Returns ``""`` when disabled so callers can append it
    unconditionally; any other failure becomes a sentinel."""
    if not screener_enabled():
        return ""
    try:
        snap = get_snapshot(ticker, curr_date)
    except VendorError as exc:
        reason = getattr(exc, "reason", exc)
        logger.warning("screener.in unavailable for %s: %s", ticker, exc)
        return "\n\n" + sentinel(f"screener.in data for {ticker}", reason)

    lines = [
        "\n\n### Institutional ownership split (screener.in)",
        "NSE's filings give only promoter vs public; this breaks the public half "
        "into foreign (FII) and domestic (DII) institutions. Current values, "
        "labelled by quarter end — not point-in-time filing dates.",
        "",
        "| Quarter | Promoters % | FII % | DII % | Public % |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {h.quarter} | {h.promoters:.2f} | {h.fii:.2f} | {h.dii:.2f} | {h.public:.2f} |"
        for h in snap.holdings
    ]

    if len(snap.holdings) >= 2:
        first, last = snap.holdings[0], snap.holdings[-1]
        lines.append(
            f"\nOver these {len(snap.holdings)} quarters: FII {first.fii:.2f}% -> "
            f"{last.fii:.2f}% ({last.fii - first.fii:+.2f} pp), DII {first.dii:.2f}% -> "
            f"{last.dii:.2f}% ({last.dii - first.dii:+.2f} pp)."
        )
    if snap.shareholders:
        lines.append(f"Retail shareholder count (latest): {snap.shareholders}.")

    # Cross-check against the primary source rather than quietly preferring one.
    if nse_promoter_pct is not None and snap.holdings:
        gap = abs(snap.holdings[-1].promoters - nse_promoter_pct)
        if gap > 0.5:
            lines.append(
                f"\n**Sources disagree**: screener.in reports promoter holding "
                f"{snap.holdings[-1].promoters:.2f}% for the latest quarter, NSE's "
                f"filing says {nse_promoter_pct:.2f}%. Prefer the NSE figure (it is "
                f"the exchange filing) and note the discrepancy."
            )

    if snap.ratios:
        lines.append(
            "\nHeadline ratios (screener.in, current): "
            + ", ".join(f"{k} {v}" for k, v in snap.ratios.items())
            + "."
        )
    return "\n".join(lines)
