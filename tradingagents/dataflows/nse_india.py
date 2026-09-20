"""NSE India fetchers: FII/DII flows, India VIX + Nifty level, Nifty put-call
ratio, and per-company shareholding, corporate actions and announcements.

NSE has no official API. This module calls the same JSON endpoints nse
india.com's own pages call. Every one of them was checked live (2026-09-20)
before being used, and the values were cross-checked where a second source
exists (Nifty and India VIX against yfinance; Reliance's promoter holding
against screener.in). What is *not* independently verified is marked below.

Verified behaviour worth knowing before changing anything:

- **User-Agent.** NSE silently drops requests from non-browser User-Agents:
  an identified ``tradingagents/...`` UA timed out on every endpoint while a
  browser UA returned 200 on all of them, with no cookies and no Referer
  needed. A browser UA is therefore required (it is also what every NSE client
  library does). This is a personal/testing tool; revisit before distributing.
- **Endpoints move.** The old ``option-chain-indices`` URL now 404s; the
  current one is a two-step ``option-chain-contract-info`` + ``option-chain-v3``.
  A 404 is reported as "the API may have changed", not retried.
- **Filters can be ignored.** Every per-company row is checked to actually be
  for the requested symbol before it is used, so an endpoint that ignores its
  ``symbol`` filter yields an error instead of another company's data.

Not independently verified: FII/DII figures (only internally consistent —
net equals buy minus sell) and the put-call ratio (computed here from the
chain's open interest; sources define PCR differently, so it is labelled).

Every value carries its as-of date. A snapshot dated after the trade date is
refused (no post-decision data in a historical run), and one too far before it
is refused as stale. Nothing here raises to the analyst: the ``*_block``
functions return a ``<... unavailable: ...>`` sentinel instead (never worded as
an absence of data), following reddit.py / india_news.py.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .errors import VendorError
from .india_data_common import (
    IST,
    CircuitBreaker,
    IndiaDataInvalid,
    IndiaSourceUnavailable,
    Throttle,
    TTLCache,
    check_fresh,
    check_not_after,
    published_by,
    resolve_curr_date,
    schema_guard,
    sentinel,
    to_float,
    today,
)
from .symbol_utils import is_india_ticker

logger = logging.getLogger(__name__)

_SRC = "NSE"
_BASE = "https://www.nseindia.com/api/"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_TIMEOUT = 15.0
# Responses are at most a few hundred KB; cap the read so a misbehaving
# endpoint can't stream an unbounded body into memory (as reddit.py does).
_MAX_BYTES = 5 * 1024 * 1024

# 12 back-to-back calls at 0.5s spacing all succeeded (2026-09-20); NSE client
# libraries cite ~3 requests/second as the ceiling. Stay well under it.
_MIN_INTERVAL = 0.5
_MARKET_TTL = 300.0  # market-wide snapshots: shared by every ticker in a batch
_FILING_TTL = 3600.0  # per-company filings change a few times a day at most

# Sanity thresholds. Deliberately generous: they exist to catch a broken or
# reshaped response, not to second-guess a real market.
_FLOW_TOLERANCE = 0.05  # crore; net must equal buy - sell to the paisa
_PCT_TOLERANCE = 0.25  # percentage points; NSE rounds last/previous close
_MIN_STRIKES = 10
_PCR_RANGE = (0.2, 5.0)
_HOLDING_SUM_TOLERANCE = 0.05  # promoter + public + employee trusts must be ~100

_THROTTLE = Throttle(_MIN_INTERVAL)
_BREAKER = CircuitBreaker(threshold=3, cooldown=300.0)
_MARKET_CACHE = TTLCache(_MARKET_TTL)
_FILING_CACHE = TTLCache(_FILING_TTL)


def reset_state() -> None:
    """Forget cached responses and breaker state (used by tests and by callers
    that want a clean slate after a long-running session)."""
    _BREAKER.reset()
    _MARKET_CACHE.clear()
    _FILING_CACHE.clear()


# --- HTTP -------------------------------------------------------------------


def _read_capped(resp) -> bytes:
    data = resp.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise http.client.HTTPException(
            f"NSE response exceeded {_MAX_BYTES} bytes; refusing to parse"
        )
    return data


def _get_json(path: str, cache: TTLCache):
    """GET ``path`` under the NSE API and return the parsed JSON.

    Successful responses are stored in ``cache``; failures never are.
    403/429/5xx/timeouts count toward the circuit breaker so a blocked run
    stops calling NSE instead of hammering it; a 404 means the endpoint moved
    and does not.
    """
    cached = cache.get(path)
    if cached is not None:
        return cached

    if not _BREAKER.allow():
        raise IndiaSourceUnavailable(
            _SRC, "circuit breaker open after repeated failures; will retry later"
        )

    _THROTTLE.wait()
    req = Request(
        _BASE + path,
        headers={"User-Agent": _UA, "Accept": "application/json, text/plain, */*"},
    )
    try:
        with urlopen(req, timeout=_TIMEOUT) as resp:
            raw = _read_capped(resp)
    except HTTPError as exc:
        if exc.code == 404:
            raise IndiaDataInvalid(
                _SRC,
                f"endpoint {path.split('?')[0]!r} not found (HTTP 404); "
                f"NSE may have changed this API",
            ) from exc
        _BREAKER.record_failure()
        raise IndiaSourceUnavailable(_SRC, f"HTTP {exc.code}") from exc
    except (OSError, http.client.HTTPException) as exc:
        _BREAKER.record_failure()
        raise IndiaSourceUnavailable(_SRC, f"{type(exc).__name__}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _BREAKER.record_failure()
        raise IndiaDataInvalid(
            _SRC, "response was not JSON (likely an HTML block page)"
        ) from exc

    _BREAKER.record_success()
    cache.set(path, data)
    return data


def _query(path: str, **params: str) -> str:
    return f"{path}?{urlencode(params)}"


# --- parsing helpers --------------------------------------------------------

_DATETIME_FORMATS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y")


def parse_nse_datetime(raw: object) -> datetime | None:
    """An NSE timestamp (``18-Sep-2026 15:30``, ``16-JUL-2026 19:24:44``,
    ``18-Sep-2026``) as an IST-aware datetime, or ``None`` if unparseable."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def parse_nse_date(raw: object) -> date | None:
    parsed = parse_nse_datetime(raw)
    return parsed.date() if parsed else None


def _require_datetime(raw: object, what: str) -> datetime:
    parsed = parse_nse_datetime(raw)
    if parsed is None:
        raise IndiaDataInvalid(_SRC, f"unparseable {what}: {raw!r}")
    return parsed


def nse_symbol(ticker: str) -> str:
    """The NSE trading symbol for a Yahoo-style ticker (``RELIANCE.NS`` ->
    ``RELIANCE``, ``M&M.NS`` -> ``M&M``).

    Only NSE tickers are mapped. A ``.BO`` (BSE) root is *not* assumed to be an
    NSE symbol: the same string can name a different company on the other
    exchange, which is exactly how a bare-root lookup once resolved TCS and ITC
    to unrelated US companies on StockTwits.
    """
    text = (ticker or "").strip()
    upper = text.upper()
    for suffix in (".NS", ".NSE"):
        if upper.endswith(suffix) and len(upper) > len(suffix):
            return upper[: -len(suffix)]
    if is_india_ticker(text):
        raise IndiaSourceUnavailable(
            _SRC,
            f"{ticker} is BSE-listed and has no verified NSE symbol mapping "
            f"(a guessed root can name a different company)",
        )
    raise IndiaSourceUnavailable(_SRC, f"{ticker} is not an NSE (.NS) ticker")


def _rows_for_symbol(rows: object, symbol: str, what: str) -> list[dict]:
    """The rows of a per-company response that are actually for ``symbol``.

    A non-empty response with no matching row means the endpoint ignored its
    filter (or returned something else entirely): fail rather than use it.
    """
    if not isinstance(rows, list):
        raise IndiaDataInvalid(_SRC, f"{what} response was not a list")
    dicts = [r for r in rows if isinstance(r, dict)]
    mine = [r for r in dicts if str(r.get("symbol", "")).strip().upper() == symbol]
    if dicts and not mine:
        raise IndiaDataInvalid(
            _SRC, f"{what} response contained no rows for {symbol} (symbol filter ignored?)"
        )
    if len(mine) != len(dicts):
        logger.warning("NSE %s: dropped %d row(s) not for %s", what, len(dicts) - len(mine), symbol)
    return mine


# --- market-wide snapshots ---------------------------------------------------


@dataclass(frozen=True)
class FiiDiiFlows:
    """One trading session of institutional cash-market flows, Rs crore."""

    as_of: date
    fii_buy: float
    fii_sell: float
    fii_net: float
    dii_buy: float
    dii_sell: float
    dii_net: float


def get_fii_dii(curr_date: str | date | None = None) -> FiiDiiFlows:
    """Latest FII/FPI and DII net flows. Validated: both categories present,
    same session, and net == buy - sell for each."""
    curr = resolve_curr_date(curr_date)
    data = _get_json("fiidiiTradeReact", _MARKET_CACHE)

    with schema_guard(_SRC, "FII/DII"):
        by_kind: dict[str, dict] = {}
        for row in data:
            category = str(row["category"]).upper()
            if category == "DII":
                by_kind["dii"] = row
            elif "FII" in category or "FPI" in category:
                by_kind["fii"] = row
        if set(by_kind) != {"fii", "dii"}:
            raise IndiaDataInvalid(
                _SRC, f"expected FII and DII rows, found {sorted(by_kind) or 'neither'}"
            )

        parsed = {}
        for kind, row in by_kind.items():
            buy, sell, net = (to_float(row[k]) for k in ("buyValue", "sellValue", "netValue"))
            if None in (buy, sell, net):
                raise IndiaDataInvalid(_SRC, f"{kind.upper()} row has a non-numeric value")
            if abs((buy - sell) - net) > _FLOW_TOLERANCE:
                raise IndiaDataInvalid(
                    _SRC,
                    f"{kind.upper()} net {net:,.2f} != buy {buy:,.2f} - sell {sell:,.2f}",
                )
            parsed[kind] = (buy, sell, net, _require_datetime(row["date"], "FII/DII date").date())

        if parsed["fii"][3] != parsed["dii"][3]:
            raise IndiaDataInvalid(_SRC, "FII and DII rows are for different sessions")
        as_of = parsed["fii"][3]

    check_not_after(_SRC, "FII/DII flows", as_of, curr)
    check_fresh(_SRC, "FII/DII flows", as_of, curr)
    return FiiDiiFlows(as_of, *parsed["fii"][:3], *parsed["dii"][:3])


@dataclass(frozen=True)
class IndexLevel:
    name: str
    last: float
    previous_close: float
    change_pct: float


@dataclass(frozen=True)
class MarketLevels:
    """Nifty 50 and India VIX from one NSE index snapshot."""

    as_of: datetime
    nifty: IndexLevel
    vix: IndexLevel


def _index_level(row: dict, name: str) -> IndexLevel:
    last, prev, pct = (to_float(row[k]) for k in ("last", "previousClose", "percentChange"))
    if None in (last, prev, pct) or last <= 0 or prev <= 0:
        raise IndiaDataInvalid(_SRC, f"{name} has a missing or non-positive value")
    implied = (last / prev - 1.0) * 100.0
    if abs(implied - pct) > _PCT_TOLERANCE:
        raise IndiaDataInvalid(
            _SRC,
            f"{name} change {pct:+.2f}% disagrees with last/previous close ({implied:+.2f}%)",
        )
    return IndexLevel(name, last, prev, pct)


def get_market_levels(curr_date: str | date | None = None) -> MarketLevels:
    """Nifty 50 and India VIX. Validated: positive levels, VIX in (0, 100), and
    each day-change consistent with its own last and previous close."""
    curr = resolve_curr_date(curr_date)
    data = _get_json("allIndices", _MARKET_CACHE)

    with schema_guard(_SRC, "index levels"):
        as_of = _require_datetime(data["timestamp"], "index snapshot timestamp")
        rows = {str(r["indexSymbol"]).upper(): r for r in data["data"]}
        nifty = _index_level(rows["NIFTY 50"], "Nifty 50")
        vix = _index_level(rows["INDIA VIX"], "India VIX")
        if not 0 < vix.last < 100:
            raise IndiaDataInvalid(_SRC, f"India VIX {vix.last} is outside (0, 100)")

    check_not_after(_SRC, "index snapshot", as_of.date(), curr)
    check_fresh(_SRC, "index snapshot", as_of.date(), curr)
    return MarketLevels(as_of, nifty, vix)


@dataclass(frozen=True)
class NiftyPcr:
    """Nifty put-call ratio by open interest for one expiry."""

    as_of: datetime
    expiry: date
    underlying: float
    pcr: float
    put_oi: int
    call_oi: int
    strikes: int


def get_nifty_pcr(curr_date: str | date | None = None) -> NiftyPcr:
    """PCR (total put OI / total call OI) for the nearest Nifty expiry on or
    after the trade date. Validated: enough strikes, non-zero OI on both sides,
    and a PCR inside a generous plausibility band."""
    curr = resolve_curr_date(curr_date)
    info = _get_json(_query("option-chain-contract-info", symbol="NIFTY"), _MARKET_CACHE)

    with schema_guard(_SRC, "option-chain expiries"):
        pairs = sorted(
            (d, raw) for raw in info["expiryDates"] if (d := parse_nse_date(raw)) is not None
        )
        if not pairs:
            raise IndiaDataInvalid(_SRC, "no parseable Nifty expiry dates")
        pick = next((p for p in pairs if p[0] >= curr), None)
    if pick is None:
        raise IndiaSourceUnavailable(_SRC, f"no listed Nifty expiry on or after {curr}")
    expiry, expiry_raw = pick

    chain = _get_json(
        _query("option-chain-v3", type="Indices", symbol="NIFTY", expiry=expiry_raw),
        _MARKET_CACHE,
    )

    with schema_guard(_SRC, "Nifty option chain"):
        records = chain["records"]
        as_of = _require_datetime(records["timestamp"], "option-chain timestamp")
        underlying = to_float(records["underlyingValue"])
        call_oi = put_oi = strikes = 0
        for row in records["data"]:
            if parse_nse_date(row.get("expiryDates")) != expiry:
                continue
            call = to_float((row.get("CE") or {}).get("openInterest"))
            put = to_float((row.get("PE") or {}).get("openInterest"))
            if call is None and put is None:
                continue
            call_oi += int(call or 0)
            put_oi += int(put or 0)
            strikes += 1

    if underlying is None or underlying <= 0:
        raise IndiaDataInvalid(_SRC, "option chain has no valid underlying value")
    if strikes < _MIN_STRIKES:
        raise IndiaDataInvalid(
            _SRC, f"option chain has only {strikes} strikes for {expiry_raw} (< {_MIN_STRIKES})"
        )
    if call_oi <= 0 or put_oi <= 0:
        raise IndiaDataInvalid(
            _SRC, f"option chain has no open interest on one side (calls {call_oi}, puts {put_oi})"
        )
    pcr = put_oi / call_oi
    if not _PCR_RANGE[0] <= pcr <= _PCR_RANGE[1]:
        raise IndiaDataInvalid(_SRC, f"PCR {pcr:.2f} is outside the plausible band {_PCR_RANGE}")

    check_not_after(_SRC, "option chain", as_of.date(), curr)
    check_fresh(_SRC, "option chain", as_of.date(), curr)
    return NiftyPcr(as_of, expiry, underlying, pcr, put_oi, call_oi, strikes)


# --- per-company filings -----------------------------------------------------


@dataclass(frozen=True)
class ShareholdingRow:
    quarter_end: date
    promoter_pct: float
    public_pct: float
    filed: datetime
    # Shares held by employee benefit trusts (and DR custodians): neither
    # promoter nor public, but part of the 100% (Infosys: ~0.2%).
    other_pct: float = 0.0


def get_shareholding(
    ticker: str, curr_date: str | date | None = None, quarters: int = 8
) -> list[ShareholdingRow]:
    """Promoter/public holding for the last ``quarters`` filings, newest first.

    Only filings public on or before the trade date are used. Each row is
    checked to be for the requested symbol and for promoter + public + employee
    trusts to sum to ~100%; a bad row is skipped, and if nothing usable remains
    the call fails.
    """
    symbol = nse_symbol(ticker)
    curr = resolve_curr_date(curr_date)
    raw = _get_json(
        _query("corporate-share-holdings-master", index="equities", symbol=symbol), _FILING_CACHE
    )

    with schema_guard(_SRC, "shareholding"):
        rows = _rows_for_symbol(raw, symbol, "shareholding")
        if not rows:
            raise IndiaSourceUnavailable(_SRC, f"no shareholding filings listed for {symbol}")

        latest: dict[date, ShareholdingRow] = {}
        invalid = future = 0
        for row in rows:
            quarter_end = parse_nse_date(row["date"])
            filed = parse_nse_datetime(row.get("broadcastDate") or row.get("submissionDate"))
            promoter = to_float(row["pr_and_prgrp"])
            public = to_float(row["public_val"])
            # Employee trusts / DR custodians are a third slice of the 100%. A
            # promoter-plus-public-only check wrongly rejects every Infosys
            # filing (13.82 + 85.97 + 0.21 trusts).
            other = (to_float(row.get("employeeTrusts")) or 0.0) + (
                to_float(row.get("underlyingDrs")) or 0.0
            )
            if (
                quarter_end is None
                or filed is None
                or promoter is None
                or public is None
                or not (0 <= promoter <= 100 and 0 <= public <= 100 and 0 <= other <= 100)
                or abs(promoter + public + other - 100) > _HOLDING_SUM_TOLERANCE
            ):
                invalid += 1
                continue
            if not published_by(filed, curr):
                future += 1
                continue
            current = latest.get(quarter_end)
            if current is None or filed > current.filed:
                latest[quarter_end] = ShareholdingRow(quarter_end, promoter, public, filed, other)
        if invalid:
            logger.warning(
                "NSE shareholding for %s: skipped %d row(s) that failed validation", symbol, invalid
            )

    if not latest:
        if invalid and not future:
            raise IndiaDataInvalid(_SRC, f"every shareholding row for {symbol} failed validation")
        raise IndiaSourceUnavailable(
            _SRC, f"no shareholding filing for {symbol} was public by {curr.isoformat()}"
        )
    ordered = sorted(latest.values(), key=lambda r: r.quarter_end, reverse=True)
    return ordered[:quarters]


@dataclass(frozen=True)
class CorporateAction:
    subject: str
    ex_date: date
    record_date: date | None
    upcoming: bool


def get_corporate_actions(
    ticker: str,
    curr_date: str | date | None = None,
    limit: int = 5,
    history_days: int = 365,
    upcoming_days: int = 90,
) -> list[CorporateAction]:
    """Recent dividends, bonuses, splits — newest first.

    An action whose ex-date is on or before the trade date was necessarily
    announced before it, so it is safe in a historical run. An upcoming one
    (ex-date after the trade date) has no announcement date on this endpoint,
    so it is shown only for a live run (trade date today or later).
    """
    symbol = nse_symbol(ticker)
    curr = resolve_curr_date(curr_date)
    raw = _get_json(
        _query("corporates-corporateActions", index="equities", symbol=symbol), _FILING_CACHE
    )
    live = curr >= today()

    with schema_guard(_SRC, "corporate actions"):
        actions = []
        for row in _rows_for_symbol(raw, symbol, "corporate actions"):
            ex_date = parse_nse_date(row.get("exDate"))
            subject = " ".join(str(row.get("subject", "")).split())
            if ex_date is None or not subject:
                continue  # no ex-date means no way to prove it was public yet
            if ex_date <= curr:
                if (curr - ex_date).days > history_days:
                    continue
                upcoming = False
            else:
                if not live or (ex_date - curr).days > upcoming_days:
                    continue
                upcoming = True
            actions.append(
                CorporateAction(subject, ex_date, parse_nse_date(row.get("recDate")), upcoming)
            )
    actions.sort(key=lambda a: a.ex_date, reverse=True)
    return actions[:limit]


# Announcement categories that are procedural by construction: a statutory
# newspaper advertisement, a notice that executives will attend a conference,
# the opening/closing of the insider trading window, a routine depository
# certificate. Surveyed across 709 real filings from 8 companies over 6.5
# months (2026-09-20); these four accounted for 27% of the volume and none of
# them carries a number an analyst would act on.
#
# This list is deliberately SHORT and is a deny-list, not an allow-list: an
# unrecognised or newly-introduced category counts as material. The survey
# showed why that matters — the generic "Updates" bucket (25% of all filings)
# carried Reliance's Jio Platforms IPO observation letter and a Rolls-Royce
# partnership announcement alongside the noise, so classifying by category
# alone would have discarded the most market-moving news in the window.
#
# Nothing is ever dropped: routine items are rolled up into a one-line count
# instead of being rendered in full, so the model still sees that they were
# filed and can say so.
_ROUTINE_CATEGORIES = frozenset(
    {
        "analysts/institutional investor meet/con. call updates",
        "copy of newspaper publication",
        "trading window",
        "certificate under sebi (depositories and participants) regulations, 2018",
    }
)

# A filing whose own text certifies that nothing price-sensitive was shared.
# That is the company's assertion, not our guess, so it is safe to compress
# regardless of category. Low yield (~1% of filings) but free and principled.
_NO_UPSI = re.compile(r"no\s+unpublished\s+price[\s-]*sensitive\s+information", re.I)


@dataclass(frozen=True)
class Announcement:
    at: datetime
    category: str
    text: str
    link: str
    # True when the filing is procedural (see _ROUTINE_CATEGORIES). Routine
    # items are summarised rather than quoted; they are never discarded.
    routine: bool = False


def _is_routine(category: str, text: str) -> bool:
    return category.strip().lower() in _ROUTINE_CATEGORIES or bool(_NO_UPSI.search(text))


def get_announcements(
    ticker: str,
    curr_date: str | date | None = None,
    lookback_days: int = 30,
    limit: int = 10,
) -> list[Announcement]:
    """Exchange announcements filed in the ``lookback_days`` up to the trade
    date, newest first. The window is asked of NSE (an unbounded call returns
    the company's whole history, thousands of rows) and re-applied here on the
    IST calendar day."""
    symbol = nse_symbol(ticker)
    curr = resolve_curr_date(curr_date)
    start = curr - timedelta(days=lookback_days)
    raw = _get_json(
        _query(
            "corporate-announcements",
            index="equities",
            symbol=symbol,
            from_date=start.strftime("%d-%m-%Y"),
            to_date=curr.strftime("%d-%m-%Y"),
        ),
        _FILING_CACHE,
    )

    with schema_guard(_SRC, "announcements"):
        found = []
        for row in _rows_for_symbol(raw, symbol, "announcements"):
            at = parse_nse_datetime(row.get("an_dt"))
            if at is None or not (start <= at.date() <= curr):
                continue
            category = " ".join(str(row.get("desc", "")).split())
            text = " ".join(str(row.get("attchmntText", "")).split())
            found.append(
                Announcement(
                    at,
                    category,
                    text,
                    str(row.get("attchmntFile", "") or ""),
                    routine=_is_routine(category, text),
                )
            )
    found.sort(key=lambda a: a.at, reverse=True)
    # ``limit`` caps the filings quoted in full. Routine ones are all kept and
    # summarised by the caller, so a burst of newspaper notices can never push
    # a material filing out of the window.
    material = [a for a in found if not a.routine][:limit]
    return material + [a for a in found if a.routine]


# --- formatted blocks (never raise) ------------------------------------------


def _render(label: str, produce) -> str:
    """Run ``produce()``; a data failure becomes a sentinel, a bug still raises."""
    try:
        return produce()
    except VendorError as exc:
        reason = getattr(exc, "reason", exc)
        logger.warning("%s unavailable: %s", label, exc)
        return sentinel(label, reason)


def _stamp(as_of: datetime | date) -> str:
    if isinstance(as_of, datetime):
        return as_of.strftime("%d-%b-%Y %H:%M IST")
    return as_of.strftime("%d-%b-%Y")


def fii_dii_block(curr_date: str | date | None = None) -> str:
    def produce() -> str:
        f = get_fii_dii(curr_date)
        return (
            f"FII/FPI net {f.fii_net:+,.2f} Rs crore (bought {f.fii_buy:,.2f}, sold "
            f"{f.fii_sell:,.2f}); DII net {f.dii_net:+,.2f} Rs crore (bought {f.dii_buy:,.2f}, "
            f"sold {f.dii_sell:,.2f}) — NSE, session of {_stamp(f.as_of)}"
        )

    return _render("NSE FII/DII flows", produce)


def market_levels_block(curr_date: str | date | None = None) -> str:
    def produce() -> str:
        m = get_market_levels(curr_date)
        return (
            f"India VIX {m.vix.last:.2f} ({m.vix.change_pct:+.2f}% vs previous close "
            f"{m.vix.previous_close:.2f}); Nifty 50 {m.nifty.last:,.2f} "
            f"({m.nifty.change_pct:+.2f}%) — NSE, as of {_stamp(m.as_of)}"
        )

    return _render("NSE index levels", produce)


def nifty_pcr_block(curr_date: str | date | None = None) -> str:
    def produce() -> str:
        p = get_nifty_pcr(curr_date)
        return (
            f"Nifty put-call ratio (open interest, {_stamp(p.expiry)} expiry): {p.pcr:.2f} — "
            f"put OI {p.put_oi:,} / call OI {p.call_oi:,} across {p.strikes} strikes — "
            f"NSE, as of {_stamp(p.as_of)}"
        )

    return _render("NSE Nifty put-call ratio", produce)


def shareholding_block(
    ticker: str, curr_date: str | date | None = None, quarters: int = 8
) -> str:
    def produce() -> str:
        rows = get_shareholding(ticker, curr_date, quarters)
        show_other = any(r.other_pct > 0 for r in rows)
        lines = [
            "Shareholding pattern from NSE filings (promoter & promoter group / public; "
            "FII/DII split not in this source):",
            "| Quarter end | Promoter % | Public % |"
            + (" Employee trusts % |" if show_other else "")
            + " Filed |",
            "|---|---|---|" + ("---|" if show_other else "") + "---|",
        ]
        for r in rows:
            other = f" {r.other_pct:.2f} |" if show_other else ""
            lines.append(
                f"| {_stamp(r.quarter_end)} | {r.promoter_pct:.2f} | {r.public_pct:.2f} |"
                f"{other} {_stamp(r.filed.date())} |"
            )
        if all(r.promoter_pct == 0 for r in rows):
            lines.append(
                "NSE reports no promoter holding in any of these filings: the company has no "
                "identified promoter (widely held / professionally managed), so 0% is the "
                "reported figure, not missing data."
            )
        elif len(rows) >= 2:
            delta = rows[0].promoter_pct - rows[1].promoter_pct
            lines.append(f"Promoter holding change vs previous filing: {delta:+.2f} pp")
        return "\n".join(lines)

    return _render(f"NSE shareholding for {ticker}", produce)


def corporate_actions_block(ticker: str, curr_date: str | date | None = None) -> str:
    def produce() -> str:
        actions = get_corporate_actions(ticker, curr_date)
        if not actions:
            return f"NSE lists no corporate actions for {ticker} in the last 12 months."
        lines = ["Corporate actions from NSE:"]
        for a in actions:
            record = f", record date {_stamp(a.record_date)}" if a.record_date else ""
            tag = " (upcoming)" if a.upcoming else ""
            lines.append(f"- ex-date {_stamp(a.ex_date)}{tag}: {a.subject}{record}")
        return "\n".join(lines)

    return _render(f"NSE corporate actions for {ticker}", produce)


def announcements_block(
    ticker: str,
    curr_date: str | date | None = None,
    lookback_days: int = 30,
    limit: int = 10,
) -> str:
    def produce() -> str:
        items = get_announcements(ticker, curr_date, lookback_days, limit)
        if not items:
            return f"No NSE announcements filed for {ticker} in the last {lookback_days} days."
        material = [a for a in items if not a.routine]
        routine = [a for a in items if a.routine]

        lines = []
        if material:
            lines.append(f"Recent NSE announcements for {ticker} (newest first):")
            for a in material:
                text = a.text if len(a.text) <= 220 else a.text[:220].rstrip() + "…"
                label = f"{a.category}: " if a.category else ""
                lines.append(f"- {_stamp(a.at)} | {label}{text}")
        else:
            lines.append(
                f"No substantive NSE announcements for {ticker} in the last "
                f"{lookback_days} days."
            )
        if routine:
            # Summarised, not hidden: the model is told these exist and that
            # their detail was withheld, so it cannot read the gap as silence.
            counts = Counter(a.category or "Uncategorised" for a in routine)
            summary = ", ".join(f"{n}x {cat}" for cat, n in counts.most_common())
            lines.append(
                f"Also filed in this window, detail omitted as procedural "
                f"(statutory notices, conference-attendance intimations, trading-window "
                f"and depository certificates): {summary}."
            )
        return "\n".join(lines)

    return _render(f"NSE announcements for {ticker}", produce)
