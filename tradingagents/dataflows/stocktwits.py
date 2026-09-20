"""StockTwits public symbol-stream fetcher.

StockTwits exposes a per-symbol message stream at
``api.stocktwits.com/api/2/streams/symbol/{ticker}.json`` that requires no
API key, no OAuth, and no registration. Each message includes a
user-labeled sentiment field (``Bullish``/``Bearish``/null), the message
body, timestamp, and posting user.

The function is deliberately self-contained: short timeout, graceful
degradation on any HTTP or parse failure, and a string return type so
the calling agent gets a uniform interface regardless of whether the
network call succeeded.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
from datetime import datetime
from urllib.request import Request, urlopen

from .date_window import coverage_gap, in_window
from .symbol_utils import crypto_base

logger = logging.getLogger(__name__)

_API = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"


def _created_at(message) -> datetime | None:
    """Parse a message's ISO 8601 ``created_at``; None when missing or malformed."""
    raw = message.get("created_at")
    if not raw:
        return None
    with contextlib.suppress(ValueError, TypeError):
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return None


def _within_window(messages, start_date, end_date):
    """Keep only messages published in [start_date, end_date] (look-ahead safe).

    No window (both None) leaves the list untouched for live callers. A message
    whose ``created_at`` (ISO 8601) is unparseable is dropped in a historical
    window, since we can't prove it isn't from after the as-of date (#1220).
    """
    if not (start_date and end_date):
        return messages
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    return [m for m in messages if in_window(_created_at(m), start_dt, end_dt)]


# Indian exchange suffixes that mark a ticker as needing ADR remapping (see
# _INDIA_ADR_ALIASES below) rather than a bare-symbol guess.
_BARE_CASHTAG_SUFFIXES = (".NS", ".BO", ".BSE", ".NSE")

# Verified NSE/BSE root -> StockTwits symbol mapping, checked live against
# api.stocktwits.com on 2026-09-17. Blind "strip the suffix and hope" is NOT
# safe for Indian equities: most NSE roots simply 404 (RELIANCE, HDFCBANK,
# SBIN, WIPRO, MARUTI, SUNPHARMA, AXISBANK, TITAN, ONGC, NTPC all confirmed),
# and several *silently collide* with an unrelated US ticker of the same
# letters and return a confidently-wrong company's sentiment instead of no
# data at all:
#   TCS.NS -> bare "TCS" is Container Store Group Inc (OTC), not Tata
#             Consultancy Services.
#   ITC.NS -> bare "ITC" is ITC Holdings Corp (NYSE), a US utility, not
#             ITC Limited.
# Only the roots below have a confirmed-correct StockTwits listing, via the
# company's own US-listed ADR (or, for INFY/VEDL, a coincidental ticker
# match with the NSE root). Anything else returns no mapping so the caller
# skips the request rather than gamble on a wrong-company result — same
# principle as the SHEL.L / BRK.B guards a few lines up in the test suite.
_INDIA_ADR_ALIASES = {
    "INFY": "INFY",  # Infosys — ADR ticker coincides with the NSE root
    "WIPRO": "WIT",  # Wipro ADR
    "ICICIBANK": "IBN",  # ICICI Bank ADR
    "HDFCBANK": "HDB",  # HDFC Bank ADR
    "DRREDDY": "RDY",  # Dr. Reddy's Laboratories ADR
    "VEDL": "VEDL",  # Vedanta — ADR ticker coincides with the NSE root
    "TATAMOTORS": "TTM",  # Tata Motors ADR; NYSE-delisted 2023, stream likely
    # stale/inactive but still resolves to the correct company, not a
    # different one — safe to keep, weight confidence accordingly.
}


def _stocktwits_symbol(ticker: str) -> str | None:
    """Map to a StockTwits cashtag, or ``None`` when there's no safe mapping.

    StockTwits lists crypto as ``BTC.X`` (Yahoo's ``BTC-USD`` form 404s), so any
    crypto symbol resolves to its base plus ``.X``. For Indian exchange-suffixed
    tickers (``.NS``/``.BO``/``.NSE``/``.BSE``), only the verified entries in
    ``_INDIA_ADR_ALIASES`` are resolved; every other Indian root returns
    ``None`` rather than a bare-symbol guess (see the module comment above for
    why that guess is unsafe). Other symbols pass through upper-cased.
    """
    base = crypto_base(ticker)
    if base:
        return f"{base}.X"
    clean = ticker.strip().upper()
    for suffix in _BARE_CASHTAG_SUFFIXES:
        if clean.endswith(suffix):
            root = clean[: -len(suffix)]
            return _INDIA_ADR_ALIASES.get(root)
    return clean



def fetch_stocktwits_messages(
    ticker: str,
    limit: int = 30,
    timeout: float = 10.0,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Fetch recent StockTwits messages for ``ticker`` and return them as a
    formatted plaintext block ready for prompt injection.

    When ``start_date``/``end_date`` (yyyy-mm-dd) are given, messages are trimmed
    to that window, so a historical run never sees today's chatter (#1220). The
    public stream only serves recent messages, so a window it cannot reach is
    reported as unavailable rather than as silence.

    Returns a placeholder string when the endpoint is unreachable, the
    symbol has no messages, the response shape is unexpected, or (for
    Indian exchange-suffixed tickers) there is no verified StockTwits
    mapping — the caller never has to special-case None or exceptions.
    """
    symbol = _stocktwits_symbol(ticker)
    if symbol is None:
        return (
            f"<no StockTwits mapping for ${ticker.upper()}: only Indian "
            "tickers with a verified US-listed ADR have StockTwits coverage>"
        )
    url = _API.format(ticker=symbol)
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        # OSError covers URLError/TimeoutError/connection resets; HTTPException
        # covers chunked-transfer errors (IncompleteRead/BadStatusLine, #1024).
        logger.warning("StockTwits fetch failed for %s: %s", ticker, exc)
        # Same wording as every other source's failure (Reddit, India news,
        # NSE, RBI, screener): name the source, give the reason, and say
        # explicitly that this is not silence. Without that last clause the
        # analyst can read a failed fetch as "nobody is discussing this",
        # which is the exact misreading #1295 was filed for.
        return (
            f"<StockTwits unavailable: {type(exc).__name__}; "
            f"this is not an absence of discussion>"
        )

    fetched = data.get("messages", []) if isinstance(data, dict) else []
    messages = _within_window(fetched, start_date, end_date)
    if not messages:
        if start_date and end_date:
            gap = coverage_gap(
                (_created_at(m) for m in fetched), start_date, end_date,
                "StockTwits", f"messages about ${ticker.upper()}",
            )
            return gap or (
                f"<no StockTwits messages for ${ticker.upper()} within "
                f"{start_date}..{end_date}>"
            )
        return f"<no StockTwits messages found for ${ticker.upper()}>"

    lines = []
    bullish = bearish = unlabeled = 0
    for m in messages[:limit]:
        created = m.get("created_at", "")
        user = (m.get("user") or {}).get("username", "?")
        entities = m.get("entities") or {}
        sentiment_obj = entities.get("sentiment") or {}
        sentiment = sentiment_obj.get("basic") if isinstance(sentiment_obj, dict) else None
        body = (m.get("body") or "").replace("\n", " ").strip()
        if len(body) > 280:
            body = body[:280] + "…"

        if sentiment == "Bullish":
            bullish += 1
            tag = "Bullish"
        elif sentiment == "Bearish":
            bearish += 1
            tag = "Bearish"
        else:
            unlabeled += 1
            tag = "no-label"
        lines.append(f"[{created} · @{user} · {tag}] {body}")

    total = bullish + bearish + unlabeled
    bull_pct = round(100 * bullish / total) if total else 0
    bear_pct = round(100 * bearish / total) if total else 0
    summary = (
        f"Bullish: {bullish} ({bull_pct}%) · "
        f"Bearish: {bearish} ({bear_pct}%) · "
        f"Unlabeled: {unlabeled} · "
        f"Total: {total} most-recent messages"
    )
    return summary + "\n\n" + "\n".join(lines)
