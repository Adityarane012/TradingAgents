"""Assembles NSE data into the context blocks injected into analyst prompts.

**Why injected rather than exposed as an LLM tool.** Every other market data
source here is a tool the analyst chooses to call. This one is pre-fetched and
pasted into the prompt, like the sentiment analyst's news/StockTwits/Reddit
blocks, for three reasons:

1. Tool-calling is the least reliable part of the free-tier providers this
   project's batch runner targets — Groq rejected the sentiment analyst's
   structured tool call outright, and a 50-ticker run cannot afford a retry
   loop per analyst.
2. The market-wide figures (FII/DII, India VIX, Nifty PCR) are identical for
   every ticker in a batch. Fetched once and cached (nse_india's TTL caches),
   they cost one HTTP call per run instead of one per analyst per ticker.
3. There is nothing for the model to choose: unlike ``get_macro_indicators``,
   which takes a series name, these blocks have no parameters worth varying.

The cost of that choice is that the model cannot ask for a different date, so
each block states its own as-of date and the analyst is told to read it.

Every block degrades to a ``<... unavailable: ...>`` sentinel rather than
raising or going silent, so a blocked NSE never fails an analysis — the
analyst simply sees that the data could not be fetched and says so.
"""

from __future__ import annotations

from datetime import date

from .config import get_config
from .nse_india import (
    announcements_block,
    corporate_actions_block,
    fii_dii_block,
    get_shareholding,
    market_levels_block,
    nifty_pcr_block,
    shareholding_block,
)
from .rbi_rates import policy_rates_block
from .screener_in import ownership_split_block, screener_enabled
from .symbol_utils import is_india_ticker

# Announcement text is the only unbounded part of these blocks (a filing
# summary can run to paragraphs), so the news block takes fewer, shorter ones
# than the raw fetcher's default. Everything else is a line or a small table.
_ANNOUNCEMENT_LIMIT = 6
_ANNOUNCEMENT_LOOKBACK_DAYS = 30
_SHAREHOLDING_QUARTERS = 6


def india_data_enabled(ticker: str) -> bool:
    """Whether to fetch NSE context for ``ticker``.

    False for non-Indian tickers, and for every ticker when the operator has
    set ``india_data_enabled=False`` (or ``--no-india-data``) to skip the
    network calls entirely — the same opt-out shape as ``reddit_enabled``.
    """
    return bool(get_config().get("india_data_enabled", True)) and is_india_ticker(ticker)


def india_market_context(ticker: str, curr_date: str | date | None = None) -> str:
    """Market-wide India context plus the company's recent exchange filings.

    Written for the news analyst: institutional flow direction, the domestic
    fear gauge and options positioning are the macro backdrop for an Indian
    equity, and exchange announcements are company news that Yahoo's feed
    covers thinly. Returns ``""`` when this ticker is not in scope.
    """
    if not india_data_enabled(ticker):
        return ""
    return (
        "\n\n### Live India market data (NSE and RBI)\n"
        "These figures were fetched directly from the exchange and the central "
        "bank and pasted in; there is no tool to re-query them. Each line states "
        "its own as-of date (or says it has none) — cite it, and if a line reads "
        "'<... unavailable ...>' then that fetch failed, which is NOT the same as "
        "the value being zero or the event not happening. Never substitute a "
        "remembered or estimated figure for one marked unavailable.\n"
        # RBI's box has no as-of date, so policy_rates_block refuses a
        # historical run outright — on those, this line is a sentinel saying
        # exactly that, which is the honest answer.
        f"- Policy rates: {policy_rates_block(curr_date)}\n"
        f"- Institutional flows: {fii_dii_block(curr_date)}\n"
        f"- Volatility & index: {market_levels_block(curr_date)}\n"
        f"- Options positioning: {nifty_pcr_block(curr_date)}\n"
        "  (PCR above ~1 means more put than call open interest. It is read as a "
        "contrarian/hedging signal and its interpretation is debated — treat it as "
        "one input, not a directional call.)\n\n"
        + announcements_block(
            ticker,
            curr_date,
            lookback_days=_ANNOUNCEMENT_LOOKBACK_DAYS,
            limit=_ANNOUNCEMENT_LIMIT,
        )
    )


def india_ownership_context(ticker: str, curr_date: str | date | None = None) -> str:
    """Shareholding pattern and corporate actions from NSE filings.

    Written for the fundamentals analyst. Promoter holding and its trend are
    among the most-watched governance signals for an Indian company and are
    absent from yfinance — whose ``heldPercentInsiders`` is not a substitute
    (it reported 51.8% for Reliance against the filed 50.48%). Returns ``""``
    when this ticker is not in scope.
    """
    if not india_data_enabled(ticker):
        return ""
    return (
        _nse_ownership(ticker, curr_date)
        # Opt-in, and empty unless screener_enabled. Appended after the NSE
        # filings so the exchange's own numbers are read first; the block
        # cross-checks its promoter figure against them.
        + ownership_split_block(ticker, curr_date, _nse_promoter_pct(ticker, curr_date))
    )


def _nse_promoter_pct(ticker: str, curr_date: str | date | None) -> float | None:
    """The latest NSE-filed promoter percentage, for cross-checking a second
    source. Best-effort: ``None`` if unavailable, which just skips the check.
    Cheap — nse_india caches the response this shares with the block above."""
    if not screener_enabled():
        return None
    try:
        rows = get_shareholding(ticker, curr_date, quarters=1)
    except Exception:  # noqa: BLE001 — a cross-check must never break the prompt
        return None
    return rows[0].promoter_pct if rows else None


def _nse_ownership(ticker: str, curr_date: str | date | None) -> str:
    return (
        "\n\n### Ownership and corporate actions from NSE filings\n"
        "Filed with the exchange and pasted in below; there is no tool to re-query "
        "them. Only filings public on or before the analysis date are included. "
        "Prefer these figures over any insider/institutional-holding percentage "
        "from the fundamentals tools for this company: those come from a vendor "
        "that models Indian promoter holding inaccurately.\n\n"
        + shareholding_block(ticker, curr_date, quarters=_SHAREHOLDING_QUARTERS)
        + "\n\n"
        + corporate_actions_block(ticker, curr_date)
    )
