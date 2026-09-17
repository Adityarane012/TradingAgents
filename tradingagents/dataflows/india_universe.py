"""Curated lists of Indian equity tickers for batch/universe analysis.

NOT a live-fetched index constituent feed — NSE's site returns HTTP 403 for
every request from this environment (checked directly, including the
homepage, before any API call), so there is no reliable way to pull the
*official* Nifty 50 membership list here. ``NIFTY_50_APPROX`` is a
best-effort list of major NSE bluechips, checked one by one against live
yfinance data (see ``verify_universe`` below) rather than trusted from
training-data recall alone — that check caught two names that would
otherwise have silently failed every run:

- Tata Motors demerged in 2025 into two separate listings: ``TMCV.NS``
  ("Tata Motors Limited," commercial vehicles — kept here as "Tata Motors")
  and ``TMPV.NS`` ("Tata Motors Passenger Vehicles Limited," the newer
  passenger/EV entity). The old ``TATAMOTORS.NS`` symbol 404s. Which one (if
  either) actually sits in the live Nifty 50 today cannot be confirmed from
  here; ``TMCV.NS`` was chosen as the closer continuation of the original
  entity, but swap in ``TMPV.NS`` if your own source says otherwise.
- LTIMindtree could not be resolved to any working symbol via yfinance's
  search or direct lookup (``LTIM.NS``, ``LTI.NS``, ``LTIMINDTREE.NS``,
  ``MINDTREE.NS`` all failed) — dropped rather than guessed. If you know its
  current ticker, add it back.

Treat this list as "verified to resolve on yfinance as of the date below,"
not as an authoritative, current Nifty 50 constituent list — NSE rebalances
the index semi-annually (typically March/September) and this will drift.
Re-run ``verify_universe`` periodically, and cross-check membership against
a live source (e.g. niftyindices.com) before relying on it for anything
that depends on exact index composition.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Verified live against yfinance on 2026-09-17 (see module docstring). 50
# tickers, NSE-listed, .NS suffix. Format: ticker -> company name (for
# display only; not read by any tool).
NIFTY_50_APPROX: dict[str, str] = {
    "RELIANCE.NS": "Reliance Industries",
    "TCS.NS": "Tata Consultancy Services",
    "HDFCBANK.NS": "HDFC Bank",
    "ICICIBANK.NS": "ICICI Bank",
    "INFY.NS": "Infosys",
    "SBIN.NS": "State Bank of India",
    "BHARTIARTL.NS": "Bharti Airtel",
    "ITC.NS": "ITC",
    "LT.NS": "Larsen & Toubro",
    "KOTAKBANK.NS": "Kotak Mahindra Bank",
    "AXISBANK.NS": "Axis Bank",
    "BAJFINANCE.NS": "Bajaj Finance",
    "HCLTECH.NS": "HCL Technologies",
    "ASIANPAINT.NS": "Asian Paints",
    "MARUTI.NS": "Maruti Suzuki",
    "SUNPHARMA.NS": "Sun Pharmaceutical Industries",
    "TITAN.NS": "Titan Company",
    "ULTRACEMCO.NS": "UltraTech Cement",
    "WIPRO.NS": "Wipro",
    "NESTLEIND.NS": "Nestle India",
    "BAJAJFINSV.NS": "Bajaj Finserv",
    "NTPC.NS": "NTPC",
    "POWERGRID.NS": "Power Grid Corporation of India",
    "M&M.NS": "Mahindra & Mahindra",
    "TMCV.NS": "Tata Motors",  # post-2025-demerger; see module docstring
    "TATASTEEL.NS": "Tata Steel",
    "JSWSTEEL.NS": "JSW Steel",
    "ADANIENT.NS": "Adani Enterprises",
    "ADANIPORTS.NS": "Adani Ports and SEZ",
    "COALINDIA.NS": "Coal India",
    "INDUSINDBK.NS": "IndusInd Bank",
    "GRASIM.NS": "Grasim Industries",
    "HINDALCO.NS": "Hindalco Industries",
    "DRREDDY.NS": "Dr. Reddy's Laboratories",
    "CIPLA.NS": "Cipla",
    "BRITANNIA.NS": "Britannia Industries",
    "EICHERMOT.NS": "Eicher Motors",
    "APOLLOHOSP.NS": "Apollo Hospitals Enterprise",
    "HEROMOTOCO.NS": "Hero MotoCorp",
    "BAJAJ-AUTO.NS": "Bajaj Auto",
    "DIVISLAB.NS": "Divi's Laboratories",
    "SBILIFE.NS": "SBI Life Insurance",
    "HDFCLIFE.NS": "HDFC Life Insurance",
    "TECHM.NS": "Tech Mahindra",
    "SHRIRAMFIN.NS": "Shriram Finance",
    "TRENT.NS": "Trent",
    "HINDUNILVR.NS": "Hindustan Unilever",
    "BEL.NS": "Bharat Electronics",
    "CHOLAFIN.NS": "Cholamandalam Investment and Finance",
    "ETERNAL.NS": "Eternal (formerly Zomato)",
}


def verify_universe(
    tickers: dict[str, str] | list[str],
    timeout: float = 10.0,
) -> tuple[list[str], list[str]]:
    """Live-check tickers against yfinance before a paid batch run.

    Returns ``(good, bad)`` — ``good`` is tickers that returned a real quote,
    ``bad`` is ones that 404'd or errored (delisted, renamed, or typo'd).
    Catches exactly the class of problem that motivated this module: a
    demerger or rename silently turning a "verified" ticker into a 404, and
    therefore a wasted LLM run against a symbol with no data.

    This makes real network calls (one per ticker) — run it before a batch,
    not inside the per-ticker analysis loop.
    """
    import yfinance as yf

    good, bad = [], []
    for ticker in (tickers.keys() if isinstance(tickers, dict) else tickers):
        try:
            info = yf.Ticker(ticker).info
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            if price:
                good.append(ticker)
            else:
                bad.append(ticker)
        except Exception as exc:  # noqa: BLE001 — any failure means "bad"
            logger.warning("verify_universe: %s failed: %s", ticker, exc)
            bad.append(ticker)
    return good, bad
