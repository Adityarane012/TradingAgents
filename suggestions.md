# TradingAgents — India-First Optimization Guide

> **Philosophy**: Indian markets have distinct structural characteristics — two major exchanges (NSE/BSE), SEBI regulations, FII/DII flow dynamics, derivatives-heavy retail participation, and a rich bilingual social media ecosystem. This guide rewires every layer of TradingAgents to treat India as the primary market.

---

## Implementation status (2026-09-17)

Most of the actionable items below were implemented on
`fix/stocktwits-symbol-exchange-suffix` (local, not pushed) — see `issues.md`
for the item-by-item status, since I-001 through I-010 there map to specific
sections here. Summary of what changed after live-testing this document's
claims:

- **§4.2/4.3 (StockTwits, Reddit subreddits):** both suggested fixes were
  tested live and corrected — see issues.md I-001/I-003. The 1-line
  DEFAULT_SUBREDDITS swap this doc suggests would have made US-ticker
  sentiment worse; implemented as region-routing instead.
- **§3.1 (FRED aliases), §6 (agent prompts), §7.2 (India news RSS), §9
  (India VIX):** implemented, with corrections noted in issues.md.
- **§2.2/2.3/2.4 (FRED disable, benchmark_ticker, output_language):** these
  already worked — verified the env vars and config keys exist and do what
  this doc says. No code change needed, they're just not obvious from the
  README (now they're mentioned there).
- **§3.3 (NSE India API) and §3.4 (RBI DBIE):** ⚠️ the 2026-09-17 note here
  said both were unreachable. **That was wrong, and both are now built** — see
  the Correction section in `issues.md`. In short: only nseindia.com's
  *homepage* 403s, its JSON API answers fine with a browser User-Agent, so
  §3.3's FII/DII, India VIX and PCR are live in `nse_india.py`; and while
  `dbie.rbi.org.in` does have a broken certificate, rbi.org.in's homepage
  carries a parseable rates box, so §3.4's repo/CRR/SLR are live in
  `rbi_rates.py`. The code sketch in §3.3 is still not a working example — it
  uses the retired `option-chain-indices` endpoint (now 404) and a session
  cookie handshake that turns out to be unnecessary.
- **§7.2 (RSS feeds):** of the four feeds listed, only Economic Times and
  Mint actually serve RSS. Business Standard's markets feed returns HTTP
  403; the Financial Express URL serves an HTML page, not RSS. Implemented
  with just the two that work (`tradingagents/dataflows/india_news.py`).
- **§4.4 (X/Twitter), §7.3/§7.4 (NSE corporate actions, SEBI filings):** not
  built — paid API or the same NSE/scraping reliability problem as §3.3.
- **Numbers not independently verified and possibly stale:** the "Nifty-VIX
  correlation ~0.7", "<10 StockTwits posts for Indian stocks", and
  "800K+ members" membership figures in this doc are asserted without a
  source; treat them as unverified color, not facts to cite.
- **NSE F&O expiry day:** this doc says "Every Thursday" in a couple of
  places (§5.2, §6.4). NSE has changed its weekly expiry day more than once
  in recent years — the market-analyst prompt guidance shipped here
  deliberately does NOT name a specific weekday for this reason.

---

## Table of Contents

1. [Quick Start — What Works Today](#1-quick-start--what-works-today)
2. [Priority 1 — Configuration Changes (Zero Code)](#2-priority-1--configuration-changes-zero-code)
3. [Priority 2 — Indian Macro Data](#3-priority-2--indian-macro-data)
4. [Priority 3 — Sentiment and Social Data](#4-priority-3--sentiment-and-social-data)
5. [Priority 4 — Market Structure Awareness](#5-priority-4--market-structure-awareness)
6. [Priority 5 — Agent Prompt Improvements](#6-priority-5--agent-prompt-improvements)
7. [Priority 6 — New Data Sources to Add](#7-priority-6--new-data-sources-to-add)
8. [Known Gaps and Honest Limitations](#8-known-gaps-and-honest-limitations)
9. [Quick Reference — Indian Ticker Conventions](#9-quick-reference--indian-ticker-conventions)

---

## 1. Quick Start — What Works Today

These things work **right now, out of the box** for Indian tickers:

| Feature | Status | Notes |
|---|---|---|
| OHLCV price data | Works fully | Use `.NS` (NSE) or `.BO` (BSE) suffix |
| Technical indicators (MACD, RSI, BB) | Works fully | Via stockstats on yfinance data |
| Fundamental data (P/E, EPS, revenue) | Works fully | yfinance covers Nifty 500 well |
| Balance sheet / income statement | Works fully | INR-denominated, quarterly/annual |
| Cash flow statement | Works fully | — |
| Insider transactions | Partial | yfinance has limited India insider data |
| Benchmark auto-selection | Built-in | .NS maps to ^NSEI, .BO maps to ^BSESN |
| INR forex pair (USDINR) | Built-in | Resolves to USDINR=X |
| News (yfinance) | Works | English-language, Reuters/ET/Mint articles |

**To run an analysis on an Indian stock right now:**

```bash
# NSE stock
python main.py --ticker RELIANCE.NS --date 2025-06-15

# BSE stock
python main.py --ticker HDFCBANK.BO --date 2025-06-15
```

---

## 2. Priority 1 — Configuration Changes (Zero Code)

> These are **immediate wins** achievable by editing `.env` or passing config — no code changes required.

### 2.1 Set India-First Macro News Queries

**File:** `tradingagents/default_config.py` — `global_news_queries` key

Replace the default US-centric macro queries with India-first equivalents:

```python
# Pass this config when creating TradingAgentsGraph
config = {
    "global_news_queries": [
        # India macro — primary
        "RBI repo rate monetary policy India inflation",
        "Nifty 50 BSE Sensex earnings GDP India economic outlook",
        "SEBI regulation FII DII institutional flows India",
        "India Union Budget fiscal deficit tax policy",
        "rupee USD INR exchange rate RBI intervention",
        # Global macro — secondary (keep for FII context)
        "Federal Reserve interest rates emerging markets",
        "oil crude Brent commodity prices India",
        "China slowdown global trade India exports",
    ],
    "global_news_lookback_days": 7,
    "global_news_article_limit": 15,
}
```

### 2.2 Disable or Repurpose FRED (US Macro)

FRED covers US indicators only. For Indian stocks, it is **tertiary context at best**.

**Option A — Disable FRED entirely** (saves API calls and token budget):
```python
config = {
    "data_vendors": {
        "macro_data": "",  # Agent gets DATA_UNAVAILABLE sentinel
    }
}
```

**Option B — Keep FRED but use India-relevant global series** (recommended):
```
# Fed/DXY still affects FII flows into India — keep FRED, but instruct
# the news analyst to pull India FRED series (see Section 3).
```

### 2.3 Set Benchmark Explicitly

```python
config = {
    "benchmark_ticker": "^NSEI",   # Nifty 50 for NSE stocks
    # or "^BSESN" for BSE / Sensex comparison
}
```

Or via `.env`:
```env
TRADINGAGENTS_BENCHMARK_TICKER=^NSEI
```

### 2.4 Output Language

```env
TRADINGAGENTS_OUTPUT_LANGUAGE=English
# For Hindi output (experimental):
# TRADINGAGENTS_OUTPUT_LANGUAGE=Hindi
```

---

## 3. Priority 2 — Indian Macro Data

> **The biggest gap**: FRED has zero India-specific indicators by default. But FRED actually hosts several India series via IMF/World Bank data that you can use immediately.

### 3.1 FRED India Series (Use Right Now — No New Code)

These are valid raw FRED series IDs — pass them directly to the existing `get_macro_indicators` tool:

| Series ID | Description | Relevance |
|---|---|---|
| `INDCPIALLMINMEI` | India CPI All Items (monthly) | RBI reaction function |
| `INTDSRINM193N` | India RBI Lending Rate | Direct policy rate |
| `INDIRLTLT01STM` | India 10Y Government Bond Yield | EM bond market |
| `DEXINUS` | USD/INR exchange rate (daily) | INR strength |
| `INDGDPRPCPPPT` | India GDP per capita (PPP) | Structural growth |

**Add these as friendly aliases to `tradingagents/dataflows/fred.py` `MACRO_SERIES` dict:**

```python
# India-specific additions — paste into MACRO_SERIES in fred.py
"india_cpi":         "INDCPIALLMINMEI",
"india_inflation":   "INDCPIALLMINMEI",
"rbi_lending_rate":  "INTDSRINM193N",   # DEAD: last updated July 2022 - do NOT use
"india_10y_yield":   "INDIRLTLT01STM",
"usdinr":            "DEXINUS",
```

### 3.2 FRED Global Series That Affect Indian Markets

| Alias | Why it Matters for India |
|---|---|
| `fed_funds_rate` | US rate hikes cause DXY strength and FII outflows from India |
| `dollar_index` | Strong dollar puts INR depreciation pressure |
| `10y_treasury` | US bond yield is the EM capital flight benchmark |
| `vix` | Global risk-off causes India selloffs (Nifty-VIX correlation ~0.7) |
| `real_gdp` | US GDP health drives IT/pharma export demand (TCS, Infosys, Sun Pharma) |

Crude oil note: India imports ~85% of crude — fetch `CL=F` via yfinance for oil price data.

### 3.3 NSE India API (No Key — Requires New Module)

NSE provides free public API data. A new `tradingagents/dataflows/nse_india.py` module can fetch:
- FII/DII daily net buy/sell figures
- India VIX
- Nifty option chain PCR (Put-Call Ratio — contrarian indicator)
- NSE corporate announcements

```python
# Base NSE API endpoints
NSE_BASE = "https://www.nseindia.com/api"
FII_DII_URL = f"{NSE_BASE}/fiidiiTradeReact"
INDIA_VIX_URL = f"{NSE_BASE}/allIndices"
OPTION_CHAIN_URL = f"{NSE_BASE}/option-chain-indices?symbol=NIFTY"
# Note: NSE API requires browser-like session cookies. Use requests.Session()
# with a prior GET to https://www.nseindia.com to obtain the session.
```

### 3.4 RBI Data Warehouse (DBIE)

The RBI DBIE portal (`dbie.rbi.org.in`) exposes free REST endpoints for:
- Repo rate history, CRR/SLR
- WPI / CPI
- Forex reserves, external debt

---

## 4. Priority 3 — Sentiment and Social Data

> **Second biggest gap**: Reddit (r/wallstreetbets, r/stocks, r/investing) and StockTwits have very thin India coverage. Indian retail is on completely different platforms.

### 4.1 Where Indian Retail Discusses Stocks

| Platform | India Relevance | Access |
|---|---|---|
| r/IndiaInvestments | High — value investing, 800K+ members | Reddit RSS (free) |
| r/DalalStreet | High — NSE/BSE focused | Reddit RSS (free) |
| r/IndianStreetBets | High — WSB-style, high-energy | Reddit RSS (free) |
| Twitter/X cashtags | Very high — retail + institutional | X API v2 (paid) |
| Telegram channels | Very high retail volume | Telegram Bot API (free) |
| Moneycontrol forums | High — India's largest finance site | Scraping only |
| StockTwits | Low — Indian stocks have less than 10 posts | Already integrated |

### 4.2 Quick Win — Add Indian Subreddits (1-Line Change)

**File:** `tradingagents/dataflows/reddit.py`, line 74

```python
# BEFORE (US-focused):
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")

# AFTER (India-first):
DEFAULT_SUBREDDITS = (
    "IndiaInvestments",    # 800K+ members, high quality long-term discussion
    "DalalStreet",         # NSE/BSE specific trading
    "IndianStreetBets",    # High-energy retail sentiment
    "stocks",              # Keep for cross-listed ADRs (Infosys, WIT, etc.)
    "investing",           # Keep for global macro context
)
```

> This is the **single highest-impact code change** for Indian sentiment quality.

### 4.3 Quick Win — Fix StockTwits Indian Ticker Symbols

Indian tickers on StockTwits do not include the exchange suffix. `RELIANCE.NS` must be searched as `RELIANCE`.

**File:** `tradingagents/dataflows/stocktwits.py`, `_stocktwits_symbol()` function

```python
def _stocktwits_symbol(ticker: str) -> str:
    """Map to StockTwits convention.
    StockTwits: crypto as BTC.X, Indian stocks without .NS/.BO suffix.
    """
    base = crypto_base(ticker)
    if base:
        return f"{base}.X"
    # Strip Indian exchange suffixes
    clean = ticker.strip().upper()
    for suffix in (".NS", ".BO", ".BSE", ".NSE"):
        if clean.endswith(suffix):
            return clean[: -len(suffix)]
    return clean
```

### 4.4 Medium-Term — Twitter/X Cashtag Monitoring

Indian retail sentiment on X is extremely high-signal, especially around:
- RBI policy dates (quarterly)
- Budget day  
- Nifty option expiry (weekly Thursdays)
- FII buying/selling days

The X API v2 Basic tier ($100/month) supports cashtag search. Build `tradingagents/dataflows/twitter_india.py`.

---

## 5. Priority 4 — Market Structure Awareness

> Indian markets have structural rules that agents are currently unaware of.

### 5.1 NSE/BSE Trading Hours (IST = UTC+5:30)

| Session | Time (IST) |
|---|---|
| Pre-open | 9:00 AM – 9:15 AM |
| Regular trading | 9:15 AM – 3:30 PM |
| Post-close | 3:40 PM – 4:00 PM |
| Closed | Saturdays, Sundays, NSE holidays |

### 5.2 Key India Calendar Events

| Event | Frequency | Market Impact |
|---|---|---|
| RBI Monetary Policy Committee (MPC) | Bi-monthly (6x/year) | Very High — rate decisions |
| Union Budget | Annual (Feb 1) | Very High — sector allocations |
| NSE F&O Weekly Expiry | Every Thursday | High — Nifty/BankNifty volatility |
| NSE Monthly Expiry | Last Thursday | Very High — rollover activity |
| TCS/Infosys/HCL Earnings | Quarterly (Apr/Jul/Oct/Jan) | High — IT bellwether |
| Advance Tax installment dates | Mar 15, Jun 15, Sep 15, Dec 15 | Medium — institutional selling |
| FII/DII data release | Daily EOD | High — flow direction |
| India CPI release | Monthly (~12th) | High — RBI reaction |
| India IIP data | Monthly (~12th) | Medium — industrial output |
| GST collections | Monthly (1st of next month) | Medium — consumption proxy |
| Monsoon progress | June–September | High — agri/FMCG/rural demand |

### 5.3 SEBI Market Microstructure Rules

- **Index circuit breakers**: 10%, 15%, 20% drops trigger 45-min, 1h45m, and full-day halts
- **Stock circuit breakers**: 5%, 10%, 20% daily limits (varies by circuit category)
- **T+1 settlement**: Since Jan 2023 — important for liquidity/cash management
- **F&O position limits**: SEBI caps are much tighter than US options; relevant for volatility analysis

### 5.4 NSE Sector Indices for Peer Comparison

| NSE Sector Index | Key Stocks | Yahoo Symbol |
|---|---|---|
| Nifty Bank | HDFCBANK, ICICIBANK, KOTAKBANK | ^NSEBANK |
| Nifty IT | TCS, INFY, WIPRO, HCL | ^CNXIT |
| Nifty Pharma | SUNPHARMA, DRREDDY, CIPLA | ^CNXPHARMA |
| Nifty Auto | MARUTI, TATAMOTORS, BAJAJ-AUTO | ^CNXAUTO |
| Nifty FMCG | HINDUNILVR, ITC, NESTLEIND | ^CNXFMCG |
| Nifty Metal | TATASTEEL, JSWSTEEL, HINDALCO | ^CNXMETAL |
| Nifty Energy | RELIANCE, ONGC, POWERGRID | ^CNXENERGY |

---

## 6. Priority 5 — Agent Prompt Improvements

### 6.1 News Analyst — India-Aware Macro Instructions

**File:** `tradingagents/agents/analysts/news_analyst.py`

Detect the Indian ticker and override the macro guidance in `system_message`:

```python
is_india = ticker.upper().endswith((".NS", ".BO"))

if is_india:
    macro_guidance = (
        "For India macro context, call get_macro_indicators with: "
        "'INDCPIALLMINMEI' (India CPI), 'DEXINUS' (USD/INR rate), "
        "'INTDSRINM193N' (RBI lending rate), 'fed_funds_rate' (US Fed — "
        "affects FII flows into India), 'vix' (global risk-off proxy), "
        "'dollar_index' (DXY strength impacts INR and FII inflows into India). "
        "Key India catalysts: RBI MPC decisions, Union Budget announcements, "
        "NSE F&O expiry weeks, FII/DII flow direction, India CPI/IIP releases, "
        "GST collection data, monsoon progress for agri/FMCG/rural demand."
    )
```

### 6.2 Fundamentals Analyst — INR and India-Specific Context

**File:** `tradingagents/agents/analysts/fundamentals_analyst.py`

Add this to the system message for Indian tickers:

```
When analyzing Indian companies:
- Financials are in INR. Crores (1 Cr = 10M INR) and Lakhs (1 L = 100K INR) are standard.
- Benchmark P/E against Nifty 50 median (~22x historically), not S&P 500.
- Promoter holding percentage is critical. Promoter share pledging is a major bearish signal.
- FII ownership changes quarter-over-quarter signal institutional confidence direction.
- Debt-to-EBITDA matters more than in the US due to India's higher interest rate environment.
- Watch for related-party transactions, common in Indian conglomerates.
- For PSU (Public Sector Undertaking) stocks, factor in government capex policy and disinvestment risk.
- Government policy changes (PLI schemes, Make in India) are material for manufacturing stocks.
```

### 6.3 Sentiment Analyst — India Social Media Context

**File:** `tradingagents/agents/analysts/sentiment_analyst.py`

Add to `_build_system_message()` for Indian tickers:

```
For Indian stocks, r/IndiaInvestments, r/DalalStreet, and r/IndianStreetBets are
the primary retail sentiment signals — not r/wallstreetbets. StockTwits coverage
for Indian stocks is typically thin (under 20 messages); weight it accordingly and
lower confidence if the sample is small. Yahoo Finance news for Indian tickers
surfaces Economic Times, Mint, Business Standard, and Reuters India — these are
high-quality institutional sources. Be explicit when data is sparse.
```

### 6.4 Market Analyst — India Technical Context

**File:** `tradingagents/agents/analysts/market_analyst.py`

Add awareness of:
- India VIX (`^INDIAVIX`) as the domestic fear gauge (distinct from CBOE VIX)
- Weekly Thursday F&O expiry creates artificial intraday and weekly volatility
- Nifty 50 key psychological levels (round numbers like 22000, 24000, 25000 are frequently cited)

---

## 7. Priority 6 — New Data Sources to Add

> These require new code but would deliver the largest coverage improvements.

### 7.1 NSE India Module — FII/DII and Option Chain (Very High Impact)

Create `tradingagents/dataflows/nse_india.py`:

```python
"""NSE India public API data fetcher.

Provides FII/DII daily flows, India VIX, Nifty option chain PCR,
and corporate action announcements — all without an API key.

Note: NSE requires a browser-like session. Initialize with a GET
to https://www.nseindia.com/ before any API call to obtain cookies.
"""
import requests

NSE_BASE = "https://www.nseindia.com/api"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

def _get_session() -> requests.Session:
    session = requests.Session()
    session.get("https://www.nseindia.com/", headers=NSE_HEADERS, timeout=10)
    return session

def get_fii_dii_data(curr_date: str) -> str:
    """Fetch FII and DII daily net buying/selling figures."""
    # Returns markdown table of FII/DII activity
    ...

def get_india_vix(curr_date: str) -> str:
    """Fetch India VIX value. VIX>20=elevated fear, VIX>30=extreme fear."""
    ...

def get_nifty_pcr(curr_date: str) -> str:
    """Fetch Nifty Put-Call Ratio. PCR>1.2 is bullish, PCR<0.8 is bearish."""
    ...
```

### 7.2 Indian Financial News RSS Feeds (High Impact)

Create `tradingagents/dataflows/india_news.py` using free RSS feeds:

```
Economic Times Markets RSS:
  https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms

Mint Markets RSS:
  https://www.livemint.com/rss/markets

Business Standard Markets RSS:
  https://www.business-standard.com/rss/markets-106.rss

Financial Express Markets RSS:
  https://www.financialexpress.com/market/feed/
```

Integrate via the existing `VENDOR_METHODS` / `interface.py` pattern as a new `india_news` vendor.

### 7.3 NSE Corporate Announcements (High Impact)

SEBI mandates all filings to NSE/BSE. Fetch via:
```
https://www.nseindia.com/api/corporates-corporateActions?index=equities
```

Data includes: board meeting dates, dividend/bonus announcements, insider disclosures, block/bulk deals, and credit rating changes. Far more useful for Indian stocks than yfinance's generic insider transactions.

### 7.4 SEBI Filings Scraper (Medium Impact)

SEBI's public portal (`efts.sebi.gov.in`) has all regulatory filings for:
- Promoter shareholding changes and pledging disclosures
- Creeping acquisition notices
- DRHP filings (for IPO peer context)

### 7.5 India VIX Integration (Medium Impact)

Add `^INDIAVIX` as a standard market-context indicator:

```python
# In market analyst or news analyst
india_vix_data = get_YFin_data_online("^INDIAVIX", start_date, end_date)
# Reference levels: <12 = complacency, 12-20 = normal, 20-30 = fear, >30 = extreme fear
```

---

## 8. Known Gaps and Honest Limitations

| Gap | Severity | Workaround Available |
|---|---|---|
| No RBI repo rate as a friendly FRED alias | High | Use raw series ID INTDSRINM193N directly |
| Reddit India subreddits not in defaults | High | 1-line fix in reddit.py (see Section 4.2) |
| StockTwits has thin India coverage | Medium | Fix suffix stripping; weight confidence low |
| No FII/DII flow data | High | Requires new NSE India module |
| No India VIX integration | Medium | Fetch ^INDIAVIX via existing yfinance tool |
| No Moneycontrol or ET dedicated news vendor | Medium | yfinance news has decent India coverage |
| No India F&O/derivative awareness in prompts | Medium | Update agent prompts (see Section 6) |
| FRED still fetches US macro by default | Low | Still useful: DXY/VIX/Fed impacts FII flows |
| No GST/IIP/PMI data integration | Medium | Not available via any free structured API |
| Promoter pledging data unavailable | High | Requires BSEIndia or SEBI scraping |
| Historical FII/DII data limited | High | NSE only provides recent data publicly |
| No monsoon/agricultural data integration | Low | Include in news queries for FMCG/agri stocks |

---

## 9. Quick Reference — Indian Ticker Conventions

### Major NSE Tickers

```
RELIANCE.NS      Reliance Industries
TCS.NS           Tata Consultancy Services
HDFCBANK.NS      HDFC Bank
INFY.NS          Infosys
HINDUNILVR.NS    Hindustan Unilever
ICICIBANK.NS     ICICI Bank
KOTAKBANK.NS     Kotak Mahindra Bank
BHARTIARTL.NS    Bharti Airtel
LT.NS            Larsen and Toubro
BAJFINANCE.NS    Bajaj Finance
WIPRO.NS         Wipro
AXISBANK.NS      Axis Bank
MARUTI.NS        Maruti Suzuki
SUNPHARMA.NS     Sun Pharmaceutical
TATAMOTORS.NS    Tata Motors
ONGC.NS          Oil and Natural Gas Corporation
NTPC.NS          NTPC Limited
POWERGRID.NS     Power Grid Corporation
JSWSTEEL.NS      JSW Steel
TATASTEEL.NS     Tata Steel
ITC.NS           ITC Limited
NESTLEIND.NS     Nestle India
ULTRACEMCO.NS    UltraTech Cement
DRREDDY.NS       Dr Reddys Laboratories
CIPLA.NS         Cipla
```

### Key Indian Indices

```
^NSEI            Nifty 50
^BSESN           BSE Sensex
^NSEBANK         Nifty Bank
^CNXIT           Nifty IT
^CNXPHARMA       Nifty Pharma
^CNXAUTO         Nifty Auto
^CNXFMCG         Nifty FMCG
^CNXMETAL        Nifty Metal
^CNXENERGY       Nifty Energy
^CNXMIDCAP       Nifty Midcap 100
^CNXSMALLCAP     Nifty Smallcap 100
^INDIAVIX        India VIX
USDINR=X         USD/INR spot rate
```

### FRED Series Relevant for India

```
INDCPIALLMINMEI    India CPI (monthly)
DEXINUS            USD/INR daily exchange rate
INTDSRINM193N      India RBI lending rate
INDIRLTLT01STM     India 10Y government bond yield
INDGDPRPCPPPT      India GDP per capita (PPP)
FEDFUNDS           US Fed Funds Rate (affects FII flows into India)
DTWEXBGS           US Dollar Index (DXY)
VIXCLS             CBOE VIX (global risk-off proxy)
```

---

## Prioritized Action Summary

| # | Action | File to Change | Effort | Impact |
|---|---|---|---|---|
| 1 | Set global_news_queries to India-first | default_config.py or runtime config | 5 min | High |
| 2 | Add FRED India aliases to MACRO_SERIES | tradingagents/dataflows/fred.py | 30 min | High |
| 3 | Change DEFAULT_SUBREDDITS to India subs | tradingagents/dataflows/reddit.py | 2 min | High |
| 4 | Fix StockTwits .NS/.BO suffix stripping | tradingagents/dataflows/stocktwits.py | 15 min | Medium |
| 5 | Set benchmark_ticker to ^NSEI | .env or runtime config | 1 min | Medium |
| 6 | Update news analyst prompt for India macros | tradingagents/agents/analysts/news_analyst.py | 30 min | High |
| 7 | Update fundamentals analyst for INR context | tradingagents/agents/analysts/fundamentals_analyst.py | 20 min | Medium |
| 8 | Update sentiment analyst for India platforms | tradingagents/agents/analysts/sentiment_analyst.py | 20 min | Medium |
| 9 | Build nse_india.py for FII/DII and PCR data | tradingagents/dataflows/nse_india.py (new) | 3-4 hours | Very High |
| 10 | Add Indian financial news RSS vendor | tradingagents/dataflows/india_news.py (new) | 3-5 hours | High |
| 11 | Add India VIX to market analyst tools | tradingagents/agents/analysts/market_analyst.py | 30 min | Medium |
| 12 | Build NSE corporate announcements fetcher | tradingagents/dataflows/nse_india.py (extend) | 2-3 hours | High |

---

*Generated for TradingAgents v0.4.x — September 2026*
*Covers NSE/BSE Indian equities. Not financial advice.*
