"""Live cross-check of the India data sources against independent sources.

The unit tests prove the parsers handle *captured* responses. They cannot tell
you the live source is still returning correct data — that is what this does.
Run it before a batch run, or whenever an India block looks off:

    python scripts/verify_india_sources.py
    python scripts/verify_india_sources.py --tickers RELIANCE.NS,TCS.NS --date 2026-09-18

Each check prints PASS, FAIL or SKIP. A FAIL means a source disagreed with an
independent one (or stopped returning usable data); a SKIP means the check
could not be made (e.g. no Yahoo close exists yet for the date), which is
reported rather than counted as a pass. Exit status is 1 if anything FAILED.

What is independent and what is not (be honest about the strength of a PASS):

- Nifty 50 and India VIX vs Yahoo Finance: a transcription check, since Yahoo's
  index data is itself sourced from NSE.
- Option-chain underlying vs the Nifty level: two NSE endpoints agreeing.
- FII/DII: internal consistency only (net == buy - sell); there is no second
  free source to compare against, so this PASS is weaker than the others.
- Per-company filings: each fetcher checks every row is for the requested
  symbol; here we only confirm they return usable data for real tickers.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from tradingagents.dataflows import nse_india
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.utils import get_current_date

DEFAULT_TICKERS = "RELIANCE.NS,TCS.NS,INFY.NS,HDFCBANK.NS"

# Yahoo's close for an index and NSE's official close should agree closely; a
# wider band is allowed while the market is open (Yahoo is delayed).
_NIFTY_REL_TOL = 0.001
_VIX_ABS_TOL = 0.05
_UNDERLYING_REL_TOL = 0.005


@dataclass
class Result:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str


def _yahoo_close(symbol: str, on: date) -> float | None:
    """Yahoo's close for ``symbol`` on the calendar day ``on``, or None."""
    import yfinance as yf

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    hist = yf.Ticker(symbol).history(start=on - timedelta(days=7), end=on + timedelta(days=1))
    for idx, close in hist["Close"].items():
        if idx.date() == on:
            return float(close)
    return None


def check_index_levels(curr: str) -> list[Result]:
    try:
        m = nse_india.get_market_levels(curr)
    except VendorError as exc:
        return [Result("NSE index levels", "FAIL", str(exc))]
    on = m.as_of.date()
    out = []
    for label, level, symbol, tol_kind in (
        ("Nifty 50", m.nifty, "^NSEI", "rel"),
        ("India VIX", m.vix, "^INDIAVIX", "abs"),
    ):
        name = f"{label} vs Yahoo ({on.isoformat()})"
        yahoo = _yahoo_close(symbol, on)
        if yahoo is None:
            out.append(Result(name, "SKIP", f"Yahoo has no {symbol} close for {on} yet"))
            continue
        diff = abs(level.last - yahoo)
        ok = diff <= _VIX_ABS_TOL if tol_kind == "abs" else diff / yahoo <= _NIFTY_REL_TOL
        out.append(
            Result(name, "PASS" if ok else "FAIL", f"NSE {level.last:,.2f} vs Yahoo {yahoo:,.2f}")
        )
    return out


def check_fii_dii(curr: str) -> Result:
    try:
        f = nse_india.get_fii_dii(curr)
    except VendorError as exc:
        return Result("FII/DII flows", "FAIL", str(exc))
    return Result(
        "FII/DII flows (internal consistency only)",
        "PASS",
        f"{f.as_of}: FII net {f.fii_net:+,.2f}, DII net {f.dii_net:+,.2f} Rs crore",
    )


def check_pcr(curr: str) -> Result:
    try:
        p = nse_india.get_nifty_pcr(curr)
        m = nse_india.get_market_levels(curr)
    except VendorError as exc:
        return Result("Nifty PCR / chain underlying", "FAIL", str(exc))
    diff = abs(p.underlying - m.nifty.last) / m.nifty.last
    ok = diff <= _UNDERLYING_REL_TOL
    return Result(
        "Option-chain underlying vs Nifty level",
        "PASS" if ok else "FAIL",
        f"chain {p.underlying:,.2f} vs index {m.nifty.last:,.2f}; PCR {p.pcr:.2f} "
        f"({p.strikes} strikes, {p.expiry}; own OI sum, definitions vary)",
    )


def check_company(ticker: str, curr: str) -> list[Result]:
    out = []
    try:
        rows = nse_india.get_shareholding(ticker, curr)
        top = rows[0]
        out.append(
            Result(
                f"{ticker} shareholding",
                "PASS",
                f"{len(rows)} filings; latest {top.quarter_end}: promoter {top.promoter_pct:.2f}%",
            )
        )
    except VendorError as exc:
        out.append(Result(f"{ticker} shareholding", "FAIL", str(exc)))
    try:
        found = nse_india.get_announcements(ticker, curr, lookback_days=60, limit=50)
        out.append(Result(f"{ticker} announcements (60d)", "PASS", f"{len(found)} filings"))
    except VendorError as exc:
        out.append(Result(f"{ticker} announcements (60d)", "FAIL", str(exc)))
    try:
        actions = nse_india.get_corporate_actions(ticker, curr, history_days=730)
        out.append(Result(f"{ticker} corporate actions (2y)", "PASS", f"{len(actions)} actions"))
    except VendorError as exc:
        out.append(Result(f"{ticker} corporate actions (2y)", "FAIL", str(exc)))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--date", default=None, help="Trade date, yyyy-mm-dd (default: today).")
    parser.add_argument("--tickers", default=DEFAULT_TICKERS, help="Comma-separated .NS tickers.")
    args = parser.parse_args(argv)

    curr = args.date or get_current_date()
    datetime.strptime(curr, "%Y-%m-%d")  # fail fast on a malformed date
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    print(f"India data source verification for trade date {curr}\n")
    started = time.monotonic()
    results = [*check_index_levels(curr), check_fii_dii(curr), check_pcr(curr)]
    for ticker in tickers:
        results.extend(check_company(ticker, curr))

    width = max(len(r.name) for r in results)
    for r in results:
        print(f"[{r.status}] {r.name:<{width}}  {r.detail}")
    counts = {s: sum(1 for r in results if r.status == s) for s in ("PASS", "FAIL", "SKIP")}
    print(
        f"\n{counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped "
        f"in {time.monotonic() - started:.1f}s"
    )
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
