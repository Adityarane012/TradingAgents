# TradingAgents — Tracked Issues

> Issues identified from a full codebase audit on 2026-09-13.
> Each entry is formatted as a ready-to-file GitHub issue with reproduction steps,
> expected vs actual behavior, exact file locations, and suggested fixes.
> Organized and sorted by implementation complexity: 🟢 Good First Issue → 🟡 Beginner → 🟠 Intermediate → 🔴 Advanced.
> *(Note: I-007 was merged into I-005 as they were duplicate representations of the regional news query issue).*

---

## Implementation status (2026-09-17)

All nine issues below were implemented on `fix/stocktwits-symbol-exchange-suffix`
(local branch, not pushed — see git log). **Several of the suggested fixes
below were wrong and were corrected during implementation after live testing
against the real APIs** — read the notes before reusing this file's code
snippets verbatim.

| # | Status | Note |
|---|---|---|
| I-001 | ⚠️ **Implemented differently** | The suggested fix (strip `.NS`/`.BO` and query the bare symbol) is unsafe, not just incomplete. Checked live against api.stocktwits.com: most NSE roots 404 (RELIANCE, HDFCBANK, SBIN, WIPRO, MARUTI, ...), but `TCS.NS`→`TCS` resolves to **Container Store Group Inc** and `ITC.NS`→`ITC` to **ITC Holdings Corp** — a US utility. Both would have silently fed the sentiment analyst a wrong company's data. Shipped a verified allowlist (`_INDIA_ADR_ALIASES` in `stocktwits.py`) of confirmed-correct NSE-root→ADR mappings (INFY, WIT, IBN, HDB, RDY, VEDL, TTM) instead; everything else returns "no mapping" rather than guessing. |
| I-002 | ✅ Done | Already committed on `fix/env-example-alpha-vantage-key`; merged in. Could not review `.env.example`'s content directly (blocked by a permission rule in this session), but the commit message and stat matched the proposed fix. |
| I-003 | ✅ Done, corrected | Checked candidate subreddits live: r/IndiaInvestments and r/IndianStreetBets are active (posts from today/3 days ago); **r/DalalStreet's newest post was from January 2024** — dropped rather than shipped as a guaranteed-empty source. Also fixed something this issue didn't mention: the search query itself was never stripped of the exchange suffix, so even a real Indian post would rarely match "RELIANCE.NS" as a literal search term. |
| I-004 | ✅ Done, corrected | Did NOT add "benchmark against Nifty 50 ~22x" or "83-86 INR/USD" as suggested — those are hardcoded facts that go stale. Instead added `currency`/`financialCurrency` to `resolve_instrument_identity` (agent_utils.py) so every analyst gets a real, live currency fact for ANY non-USD ticker, not just India, and an explicit "read this at face value, don't convert to USD" instruction. Promoter shareholding/pledging guidance was reframed as "tell the model this data isn't available, don't invent it" rather than "always analyze it" — yfinance has no such field, and the original phrasing would have invited fabrication. |
| I-005 | ✅ Done, bug fixed | The already-merged `feat/regional-news-queries` branch had two bugs that meant it never fired through the real CLI/main.py entry points (both build config as `DEFAULT_CONFIG.copy()`, so the "did the user override this?" presence-check was always true) and could permanently mutate the shared `DEFAULT_CONFIG` object. Fixed both; see the `fix(graph)` commit. |
| I-006 | ❌ **Partly reverted 2026-09-20** | The aliases were added, then checked against FRED directly. `india_discount_rate` (INTDSRINM193N) has not updated since **July 2022** — it reports 5.15% while the actual RBI policy repo rate is 5.25%, so it was **removed**: an alias that answers "the RBI rate" with a four-year-old number is worse than no alias. FRED has no live India policy-rate series at all. Live rates are now scraped from rbi.org.in (`rbi_rates.py`) instead. `india_cpi` is stale too (latest observation March 2025) but kept, since it is the only India CPI on FRED and reports now carry an automatic staleness warning. `usdinr` and `india_10y_yield` are current. |
| I-008 | ✅ Done | README updated as suggested. |
| I-009 | ✅ Done | Confirmed `^INDIAVIX` resolves via yfinance with live data before wiring it into the market analyst prompt. |
| I-010 | ✅ Done | Implemented with the corrected I-006 aliases and softened "always cite RBI/Budget/expiry" into "these are things to watch, not asserted facts" — the model has no live tool for most of them. |

**Bonus, not in this file:** a real bug — `fundamentals_analyst.py`'s
`system_message` had a trailing comma turning it into a 1-element tuple,
rendered into the prompt as a literal tuple repr on every run. Fixed.
Also added an opt-in India news RSS vendor (Economic Times + Mint — two of
the four feeds suggested in `suggestions.md` actually work; Business
Standard 403s and the Financial Express URL serves HTML, not RSS).

**Investigated and rejected (2026-09-17) — SUPERSEDED, see below.**

---

## Correction (2026-09-20)

Two conclusions recorded on 2026-09-17 were wrong, and both were acted on:

1. **"NSE returns HTTP 403, so the FII/DII / PCR module cannot be built."**
   Only nseindia.com's *homepage* 403s. Its JSON API endpoints — the ones the
   site's own pages call — return 200 with a browser User-Agent, no cookies and
   no Referer needed. The earlier test never got past the homepage. Built as
   `tradingagents/dataflows/nse_india.py`: FII/DII flows, India VIX, Nifty level,
   Nifty put-call ratio, promoter shareholding, corporate actions and exchange
   announcements, injected into the news and fundamentals analysts.
   Caveat for whoever maintains this: NSE silently drops non-browser
   User-Agents (they hang until timeout), and endpoints move — the old
   `option-chain-indices` URL now 404s.

2. **"RBI DBIE has a broken TLS certificate, so RBI data is unavailable."**
   True of `dbie.rbi.org.in`, but rbi.org.in's own homepage carries a
   server-rendered "Current Rates" box with repo, SDF, MSF, bank rate, CRR and
   SLR. Built as `rbi_rates.py`. It has no as-of date, so it refuses historical
   runs rather than assert a rate that may not have been in force.

Still not built, and still for the original reasons: X/Twitter (paid API) and
the SEBI filings scraper (no clean API). **Promoter pledging remains
unavailable** — NSE's `corporate-pledgedata` endpoint returns empty for every
symbol tried, including known-pledged names.

Added beyond the original scope: `screener.in` as an opt-in source for the
FII/DII split within public holding, which NSE's filings do not break out, and
`scripts/verify_india_sources.py` to cross-check every source live.


---

## Index

| # | Title | Type | Priority | Effort | Level |
| :--- | :--- | :--- | :--- | :--- | :--- |
| [I-002](#i-002) | `.env.example` template missing `ALPHA_VANTAGE_API_KEY` — silent vendor failures | Bug | Medium | Trivial | 🟢 Good First Issue |
| [I-008](#i-008) | Alpha Vantage key in README is misclassified under LLM providers and lacks rate limit info | Docs | Low | Trivial | 🟢 Good First Issue |
| [I-001](#i-001) | StockTwits lookup fails for Indian tickers (`.NS` / `.BO`) — suffix not stripped | Bug | High | Trivial | 🟢 Good First Issue |
| [I-003](#i-003) | Sentiment analyst returns Neutral / low-confidence for non-US tickers — subreddits are US-only | Bug | High | Trivial | 🟢 Good First Issue |
| [I-006](#i-006) | Add India macro FRED series as friendly aliases | Feature | High | Small | 🟡 Beginner |
| [I-009](#i-009) | Add India VIX (`^INDIAVIX`) as a market fear gauge for Indian tickers | Feature | Medium | Small | 🟡 Beginner |
| [I-004](#i-004) | Fundamentals analyst has no currency context — misinterprets INR Crores as USD | Bug | High | Small | 🟠 Intermediate |
| [I-010](#i-010) | India-aware macro guidance in news analyst system prompt | Feature | High | Small | 🟠 Intermediate |
| [I-005](#i-005) | Auto-select region-appropriate `global_news_queries` based on ticker suffix | Feature | High | Medium | 🔴 Advanced |

---

## 🟢 Good First Issues

### I-002

**Title:** `.env.example` template is missing `ALPHA_VANTAGE_API_KEY` — users get silent vendor failures

**Labels:** `bug` `documentation` `developer-experience` `good-first-issue`

**Priority:** Medium  
**Effort:** Trivial (1 line)  
**Level:** 🟢 Good First Issue  

**Description:**

The `.env.example` template (which users copy to `.env` as their first setup step) lists every LLM provider key (OpenAI, Anthropic, Groq, etc.) but has no `ALPHA_VANTAGE_API_KEY` entry, despite:

- `README.md` line 148 listing `export ALPHA_VANTAGE_API_KEY=...`
- `tradingagents/dataflows/alpha_vantage_common.py` line 30 reading from `ALPHA_VANTAGE_API_KEY`
- `FRED_API_KEY` having a proper commented-out entry with a link in `.env.example`

A user who follows the documented setup path (`cp .env.example .env`) has no indication that this key exists. When they later switch `data_vendors` to `alpha_vantage`, they receive `AlphaVantageNotConfiguredError` with no actionable hint connecting it back to the missing entry in their `.env`.

**File:**

[`.env.example`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/.env.example) — missing `ALPHA_VANTAGE_API_KEY` entry

**Steps to Reproduce:**

```bash
cp .env.example .env
# Set data_vendors: core_stock_apis = "alpha_vantage" in config
python -m cli.main
# Raises: AlphaVantageNotConfiguredError: ALPHA_VANTAGE_API_KEY environment variable is not set.
```

**Actual Behavior:**

`AlphaVantageNotConfiguredError` is raised with no hint that the key is simply absent from the template the user was told to copy.

**Expected Behavior:**

`.env.example` should contain a commented-out `ALPHA_VANTAGE_API_KEY` entry, consistent with how `FRED_API_KEY` is already documented in the same file.

**Suggested Fix:**

Add to [`.env.example`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/.env.example) following the FRED key:

```env
# Alpha Vantage (optional — yfinance is the default and requires no key).
# Enables an alternative vendor for stock prices, fundamentals, news, and technical indicators.
# Free key (25 req/day): https://www.alphavantage.co/support/#api-key
#ALPHA_VANTAGE_API_KEY=
```

*(Note: Feature branch `fix/env-example-alpha-vantage-key` has already been pushed to the fork for this issue).*

---

### I-008

**Title:** Docs: Alpha Vantage key in README is misclassified under LLM providers and lacks rate limit info

**Labels:** `documentation` `developer-experience` `good-first-issue`

**Priority:** Low  
**Effort:** Trivial (documentation edit)  
**Level:** 🟢 Good First Issue  

**Description:**

`README.md` lines 131–150 list `export ALPHA_VANTAGE_API_KEY=...` under the "Required APIs" section where the heading explicitly states:

> *"TradingAgents supports multiple LLM providers. Set the API key for your chosen provider:"*

This is misleading for several reasons:

1. Alpha Vantage is a financial market data vendor, **not** an LLM provider.
2. It is **optional** — `yfinance` is the default core data vendor and requires zero API keys.
3. The free Alpha Vantage tier has strict limits of **25 requests/day** and **5 requests/minute**. A single agent run calling fundamentals, technicals, and news can exhaust the daily quota in seconds.

Users who configure this key without understanding its role encounter sudden 429 quota exhaustion or wonder why changing this key does not alter their LLM model behavior.

**File:**

[`README.md`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/README.md), lines 131–150

**Actual Behavior:**

Alpha Vantage is presented as a required LLM provider API key alongside OpenAI, Anthropic, and Gemini.

**Expected Behavior:**

Alpha Vantage should be categorized separately under an "Optional Market Data Vendors" subsection explaining that:

- It is optional (`yfinance` is the zero-config default).
- It provides alternative stock prices, fundamentals, and news data.
- The free tier is limited to 25 requests/day and 5 requests/minute.

**Suggested Fix:**

In [`README.md`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/README.md), move the key out of the LLM block into an "Optional Market Data Vendors" subsection:

```bash
# Optional: Alpha Vantage (alternative vendor for prices, fundamentals, and news)
# Free key (25 req/day, 5 req/min): https://www.alphavantage.co/support/#api-key
export ALPHA_VANTAGE_API_KEY=...

# FRED (Federal Reserve Economic Data — macro indicators)
# Free key: https://fred.stlouisfed.org/docs/api/api_key.html
export FRED_API_KEY=...
```

---

### I-001

**Title:** StockTwits lookup fails for Indian tickers (`.NS` / `.BO`) — suffix not stripped

**Labels:** `bug` `sentiment` `non-us-markets` `good-first-issue`

**Priority:** High  
**Effort:** Trivial (3 lines)  
**Level:** 🟢 Good First Issue  

**Description:**

The StockTwits API uses bare cashtag symbols (e.g. `RELIANCE` for Reliance Industries, `TCS` for Tata Consultancy Services).
When a user passes an Indian NSE/BSE ticker such as `RELIANCE.NS` or `HDFCBANK.BO`, `_stocktwits_symbol()` sends the full suffixed symbol to the StockTwits endpoint (`api.stocktwits.com/api/2/streams/symbol/RELIANCE.NS.json`), which returns an empty result or a 404.

The function already handles crypto suffix normalization (`crypto_base(ticker)` → `<BASE>.X`) but lacks equivalent stripping for exchange suffixes.

**File:**

[`tradingagents/dataflows/stocktwits.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/dataflows/stocktwits.py), lines 56–65 (`_stocktwits_symbol`)

**Steps to Reproduce:**

```python
from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages

result = fetch_stocktwits_messages("RELIANCE.NS")
print(result)
# Actual:   <no StockTwits messages found for RELIANCE.NS>
# Expected: StockTwits message stream for RELIANCE
```

**Actual Behavior:**

`<no StockTwits messages found for $RELIANCE.NS>`

The `.NS` suffix is passed verbatim to StockTwits. StockTwits has no `$RELIANCE.NS` stream; the symbol is unknown. Consequently, Indian stocks always receive an empty StockTwits context and default to a `Neutral` sentiment rating with `low` confidence.

**Expected Behavior:**

`RELIANCE.NS` should be queried as `RELIANCE` on StockTwits, stripping the domestic exchange qualifier.

**Suggested Fix:**

In [`tradingagents/dataflows/stocktwits.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/dataflows/stocktwits.py):

```python
def _stocktwits_symbol(ticker: str) -> str:
    base = crypto_base(ticker)
    if base:
        return f"{base}.X"
    # Strip exchange suffixes — StockTwits uses bare symbols for equities
    clean = ticker.strip().upper()
    for suffix in (".NS", ".BO", ".BSE", ".NSE"):
        if clean.endswith(suffix):
            return clean[: -len(suffix)]
    return clean
```

---

### I-003

**Title:** Sentiment analyst returns Neutral / low-confidence for non-US tickers — subreddits are US-only

**Labels:** `bug` `sentiment` `non-us-markets` `good-first-issue`

**Priority:** High  
**Effort:** Trivial (Option A) / Small (Option B)  
**Level:** 🟢 Good First Issue  

**Description:**

The Reddit data fetcher uses a hardcoded `DEFAULT_SUBREDDITS` tuple:

```python
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")
```

These communities focus almost exclusively on US equities. For any non-US ticker (Indian NSE/BSE, Tokyo `.T`, London `.L`, etc.), all three subreddits return zero relevant posts. The sentiment analyst receives an empty `reddit_block` and consistently outputs `Neutral` with `low` confidence regardless of active market sentiment.

For Indian stocks, active retail investment communities include `r/IndiaInvestments` (800K+ members), `r/IndianStreetBets` (high volume, WSB equivalent), and `r/DalalStreet`. These communities are never queried.

**File:**

[`tradingagents/dataflows/reddit.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/dataflows/reddit.py), line 74

**Actual Behavior:**

```python
fetch_reddit_posts("RELIANCE.NS")
# Returns: <no posts found in r/wallstreetbets for RELIANCE.NS>
#           <no posts found in r/stocks for RELIANCE.NS>
#           <no posts found in r/investing for RELIANCE.NS>
```

Sentiment analyst outputs: `Neutral`, confidence `low`.

**Expected Behavior:**

The subreddit search should query relevant regional communities based on the ticker's exchange suffix.

**Suggested Fix (Option A — minimal change):**

```python
DEFAULT_SUBREDDITS = (
    "IndiaInvestments",   # 800K+ members
    "IndianStreetBets",   # High volume NSE/BSE retail
    "DalalStreet",        # Fundamental / equity focused
    "wallstreetbets",     # Retained for cross-listed / US equities
    "stocks",
    "investing",
)
```

**Suggested Fix (Option B — regional routing):**

Add a `REGIONAL_SUBREDDITS` map keyed by exchange suffix:

```python
REGIONAL_SUBREDDITS = {
    ".NS": ("IndiaInvestments", "IndianStreetBets", "DalalStreet"),
    ".BO": ("IndiaInvestments", "IndianStreetBets", "DalalStreet"),
    ".T":  ("japanfinance", "stocks"),
    ".L":  ("UKInvesting", "stocks"),
    ".HK": ("stocks",),
}
```

---

## 🟡 Beginner Issues

### I-006

**Title:** Feature: Add India macro FRED series as friendly aliases

**Labels:** `enhancement` `macro-data` `non-us-markets` `beginner`

**Priority:** High  
**Effort:** Small (6 dictionary entries)  
**Level:** 🟡 Beginner  

**Description:**

FRED hosts several India macroeconomic time series sourced from the IMF and OECD. These are valid, active FRED series. However, they are missing from the `MACRO_SERIES` alias table in `fred.py`.

When the news analyst or user calls `get_macro_indicators("india_cpi", ...)` or `get_macro_indicators("rbi_lending_rate", ...)`, the function raises `ValueError: Unknown macro indicator alias` because neither alias exists. The LLM must guess raw FRED series IDs or omit macro indicators completely.

**File:**

[`tradingagents/dataflows/fred.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/dataflows/fred.py), `MACRO_SERIES` dict (lines 44–79)

**Series to Add:**

| Friendly Alias | FRED Series ID | Description |
| :--- | :--- | :--- |
| `india_cpi` | `INDCPIALLMINMEI` | India CPI All Items (monthly) |
| `india_inflation` | `INDCPIALLMINMEI` | Alias for `india_cpi` |
| `rbi_lending_rate` | `INTDSRINM193N` | RBI lending / discount rate |
| `india_10y_yield` | `INDIRLTLT01STM` | India 10Y government bond yield |
| `usdinr` | `DEXINUS` | USD/INR foreign exchange rate (daily) |
| `india_gdp_per_capita` | `INDGDPRPCPPPT` | India GDP per capita (PPP) |

**Suggested Fix:**

Add the entries to `MACRO_SERIES` in [`tradingagents/dataflows/fred.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/dataflows/fred.py):

```python
    # India Macro Series
    "india_cpi":            "INDCPIALLMINMEI",
    "india_inflation":      "INDCPIALLMINMEI",
    "rbi_lending_rate":     "INTDSRINM193N",
    "india_10y_yield":      "INDIRLTLT01STM",
    "usdinr":               "DEXINUS",
    "india_gdp_per_capita": "INDGDPRPCPPPT",
```

---

### I-009

**Title:** Feature: Add India VIX (`^INDIAVIX`) as a market fear gauge for Indian tickers

**Labels:** `enhancement` `market-analyst` `non-us-markets` `beginner`

**Priority:** Medium  
**Effort:** Small  
**Level:** 🟡 Beginner  

**Description:**

India has its own domestic volatility index, **India VIX** (`^INDIAVIX` on Yahoo Finance), calculated by the National Stock Exchange of India (NSE) from Nifty option bid/ask quotes. It is the domestic equivalent of the CBOE VIX and serves as the primary sentiment and fear gauge for Indian equities:

- India VIX < 12: Complacency / low market volatility
- India VIX 12–20: Normal market conditions
- India VIX 20–30: Elevated market fear / uncertainty
- India VIX > 30: Extreme volatility / crisis conditions

The market analyst currently only references US VIX (`VIXCLS` via FRED) as a volatility proxy, which is an indirect signal for domestic Indian stocks. India VIX is freely accessible via the existing `yfinance` integration (`get_stock_data("^INDIAVIX", ...)`) without requiring any new data vendor or credentials.

**File:**

[`tradingagents/agents/analysts/market_analyst.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/agents/analysts/market_analyst.py)

**Suggested Fix:**

In `market_analyst.py`, detect Indian exchange suffixes (`.NS`, `.BO`) and retrieve `^INDIAVIX` data for the analysis window via `get_stock_data`. Prepend its current level and trend into the market analyst's prompt context.

---

## 🟠 Intermediate Issues

### I-004

**Title:** Fundamentals analyst has no currency context — misinterprets INR Crores as USD

**Labels:** `bug` `fundamentals` `non-us-markets` `llm-prompt`

**Priority:** High  
**Effort:** Small (prompt update)  
**Level:** 🟠 Intermediate  

**Description:**

The fundamentals analyst system prompt contains no instructions regarding currency units or regional reporting conventions. For Indian companies, `yfinance` returns financial statements in **INR** with figures formatted in **Crores** (1 Crore = 10,000,000 INR) or **Lakhs** (1 Lakh = 100,000 INR). Without explicit context, LLMs routinely interpret these numbers as USD millions or billions.

This produces distorted outputs: a company with ₹50,000 Cr revenue gets analyzed as having $50,000 billion USD revenue, making Indian companies appear ~83x larger than their true valuation (1 USD ≈ 83–86 INR). Furthermore:

- P/E and EV/EBITDA ratios are benchmarked against S&P 500 averages (~25x) rather than Nifty 50 norms (~20–22x).
- Key Indian governance signals (promoter shareholding %, promoter share pledging, and FII/DII institutional holdings) are omitted.

**File:**

[`tradingagents/agents/analysts/fundamentals_analyst.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/agents/analysts/fundamentals_analyst.py), lines 25–30 (`system_message`)

**Actual Behavior:**

- Revenue figures for `RELIANCE.NS` (~₹9,00,000 Cr) are read as hundreds of billions or trillions of USD.
- Valuation multiples are benchmarked against US tech stocks rather than domestic peers.
- No commentary on promoter pledging or PSU-specific governance factors.

**Expected Behavior:**

For Indian tickers (detected via `.NS` / `.BO` suffix), the prompt should explicitly inject:

- Currency is INR. Figures are in Crores (1 Cr = 10M INR) or Lakhs (1 L = 100K INR).
- P/E should be benchmarked against Nifty 50 averages (~22x).
- Promoter holding percentage and pledging ratios are mandatory risk factors.
- FII/DII ownership changes indicate institutional confidence.

**Suggested Fix:**

In [`tradingagents/agents/analysts/fundamentals_analyst.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/agents/analysts/fundamentals_analyst.py), inspect `state.get("company_of_interest", "")` and append regional context:

```python
ticker = state.get("company_of_interest", "")
if ticker.upper().endswith((".NS", ".BO")):
    india_context = (
        "

### REGIONAL CONTEXT (India / NSE / BSE):
"
        "- Currency: All financial figures reported for Indian companies are in INR (₹) and commonly expressed in Crores (1 Cr = 10,000,000 INR) or Lakhs (1 L = 100,000 INR).
"
        "- Conversion: Do NOT interpret numbers as USD. Use ~83-86 INR/USD if conversion is necessary.
"
        "- Benchmarks: Compare valuation metrics (P/E, P/B, EV/EBITDA) against the Nifty 50 / Sensex averages, not S&P 500 benchmarks.
"
        "- Governance: Always analyze promoter holding %, promoter share pledging, and FII/DII shareholding trends as critical risk factors."
    )
    system_message += india_context
```

---

### I-010

**Title:** India-aware macro guidance in news analyst system prompt

**Labels:** `enhancement` `news-analyst` `non-us-markets` `llm-prompt`

**Priority:** High  
**Effort:** Small (prompt update)  
**Level:** 🟠 Intermediate  

**Description:**

The news analyst system prompt instructs the LLM:

> `"...get_macro_indicators(indicator, curr_date, look_back_days) to ground macro commentary in actual data from FRED (e.g. 'cpi', 'core_pce', 'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve')..."`

All provided examples are US series. The model has no prompt guidance to query India-specific macro indicators or monitor domestic catalysts such as:

- RBI Monetary Policy Committee (MPC) decisions
- Union Budget announcements & fiscal deficit targets
- NSE F&O weekly / monthly expiry cycles (Thursday dynamics)
- FII vs DII institutional daily net flows
- Monsoon progression (critical for FMCG, auto, and rural demand)
- GST monthly collections and IIP (Index of Industrial Production) releases

Without this prompt guidance, the news analyst evaluates Indian equities primarily through the lens of US economic cycles.

**File:**

[`tradingagents/agents/analysts/news_analyst.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/agents/analysts/news_analyst.py), line 28 (`system_message`)

**Suggested Fix:**

Detect the ticker's exchange suffix and append an India-specific macro guidance block that pairs with the FRED aliases added in I-006:

```python
ticker = state.get("company_of_interest", "")
if ticker.upper().endswith((".NS", ".BO")):
    india_macro = (
        "

### REGIONAL MACRO GUIDANCE (Indian Equities):
"
        "For macro context, call get_macro_indicators with India-relevant series: "
        "'india_cpi' (Consumer Price Index), 'usdinr' (USD/INR exchange rate), "
        "'rbi_lending_rate' (RBI policy rate), 'fed_funds_rate' (US Fed rate — drives FII flows), "
        "and 'dollar_index' (DXY — impacts INR and foreign capital inflows).
"
        "Key domestic catalysts to consider: RBI MPC decisions, Union Budget announcements, "
        "NSE F&O expiry dynamics, daily FII/DII flow trends, and monsoon progress."
    )
    system_message += india_macro
```

---

## 🔴 Advanced Issues

### I-005

**Title:** Auto-select region-appropriate `global_news_queries` based on ticker suffix

**Labels:** `enhancement` `news-analyst` `non-us-markets` `configuration`

**Priority:** High  
**Effort:** Medium (configuration mapping + graph initialization)  
**Level:** 🔴 Advanced  

*(Note: Merged duplicate entries I-005 and I-007 into this single unified issue).*

**Description:**

The default `global_news_queries` in `default_config.py` are hardcoded to US and Federal Reserve topics:

```python
"global_news_queries": [
    "Federal Reserve interest rates inflation",
    "S&P 500 earnings GDP economic outlook",
    "geopolitical risk trade war sanctions",
    "ECB Bank of England BOJ central bank policy",
    "oil commodities supply chain energy",
],
```

When analyzing an Indian stock (such as `RELIANCE.NS` or `INFY.NS`), all five US queries are still executed via Yahoo Finance search in `yfinance_news.py`. The news analyst receives a macro context dominated by Federal Reserve decisions, S&P 500 outlooks, and ECB announcements — none of which are primary catalysts for domestic Indian equities.

Meanwhile, crucial domestic market drivers (RBI MPC decisions, India CPI, FII/DII flow trends, Union Budget impact, INR currency movement, and GST collections) are never queried unless the user manually overrides the configuration dictionary.

Because the ticker's exchange suffix is known during graph initialization, `trading_graph.py` should automatically select region-appropriate news queries if the user has not provided an explicit override.

**Files to Modify:**

- [`tradingagents/default_config.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/default_config.py), lines 127–133 — add `REGIONAL_NEWS_QUERIES` dictionary
- [`tradingagents/graph/trading_graph.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/graph/trading_graph.py), line 102 — select regional queries during graph initialization

**Proposed Behavior:**

- If ticker ends in `.NS` or `.BO`: Use India macro queries (RBI, Nifty 50, SEBI, Union Budget, INR)
- If ticker ends in `.T`: Use Japan macro queries (BOJ, Nikkei 225, JPY)
- If ticker ends in `.L`: Use UK macro queries (BOE, FTSE 100, GBP)
- If no international suffix (US): Retain standard Fed / S&P 500 defaults
- Explicit user-configured `global_news_queries` always takes precedence over automatic defaults

**Suggested Fix:**

In [`tradingagents/default_config.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/default_config.py):

```python
REGIONAL_NEWS_QUERIES = {
    "IN": [
        "RBI repo rate monetary policy India inflation",
        "Nifty 50 BSE Sensex earnings GDP India economic outlook",
        "SEBI regulation FII DII institutional flows India",
        "India Union Budget fiscal deficit tax policy",
        "rupee USD INR exchange rate RBI intervention",
    ],
    "JP": [
        "Bank of Japan monetary policy interest rates inflation",
        "Nikkei 225 earnings GDP Japan economic outlook",
        "yen USD JPY exchange rate intervention",
    ],
    "UK": [
        "Bank of England interest rates UK inflation",
        "FTSE 100 earnings UK economic outlook GDP",
        "pound sterling GBP USD exchange rate",
    ],
    "US": [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings GDP economic outlook",
        "geopolitical risk trade war sanctions",
        "ECB Bank of England BOJ central bank policy",
        "oil commodities supply chain energy",
    ],
}
```

In [`tradingagents/graph/trading_graph.py`](file:///c:/Users/Aditya%20Rane/Downloads/TradingAgents/tradingagents/graph/trading_graph.py) (`TradingAgentsGraph.__init__`):

```python
# Check if caller supplied explicit global_news_queries; if not, apply regional defaults
if "global_news_queries" not in user_config:
    ticker = (self.ticker or "").upper()
    if ticker.endswith((".NS", ".BO")):
        self.config["global_news_queries"] = REGIONAL_NEWS_QUERIES["IN"]
    elif ticker.endswith(".T"):
        self.config["global_news_queries"] = REGIONAL_NEWS_QUERIES["JP"]
    elif ticker.endswith(".L"):
        self.config["global_news_queries"] = REGIONAL_NEWS_QUERIES["UK"]
    else:
        self.config["global_news_queries"] = REGIONAL_NEWS_QUERIES["US"]
```

---

*Audit performed on TradingAgents codebase — September 2026*  
*All file references, line numbers, and API definitions verified against active repository source.*
