import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# Mapping from exchange suffix to region code for auto-selecting news queries.
SUFFIX_TO_REGION = {
    ".NS": "IN", ".BO": "IN",
    ".T": "JP",
    ".L": "UK",
}

# Region-specific macro news queries used when the caller does not supply
# an explicit ``global_news_queries`` override.  The "US" entry mirrors
# the DEFAULT_CONFIG default so the behaviour is identical for US tickers.
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

# Single source of truth for env-var → config-key overrides. To expose
# a new config key for environment-based override, add a row here — no
# entry-point script changes required. Coercion is driven by the type
# of the existing default, so users can keep writing plain strings in
# their .env file.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":         "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":       "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":      "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL":      "backend_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":      "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":    "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":      "max_risk_discuss_rounds",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":   "checkpoint_enabled",
    "TRADINGAGENTS_REDDIT_ENABLED":       "reddit_enabled",
    "TRADINGAGENTS_REDDIT_MAX_WAIT_SECONDS": "reddit_max_wait_seconds",
    "TRADINGAGENTS_INDIA_DATA_ENABLED":   "india_data_enabled",
    "TRADINGAGENTS_INDIA_MAX_STALENESS_DAYS": "india_max_staleness_days",
    "TRADINGAGENTS_SCREENER_ENABLED":     "screener_enabled",
    "TRADINGAGENTS_BENCHMARK_TICKER":     "benchmark_ticker",
    "TRADINGAGENTS_TEMPERATURE":          "temperature",
    "TRADINGAGENTS_LLM_MAX_RETRIES":      "llm_max_retries",
    "TRADINGAGENTS_MAX_TOKENS":           "max_tokens",
    # Provider-specific reasoning/thinking knobs (None = each provider's own
    # default). Settable here for non-interactive runs; the CLI also offers an
    # interactive choice, which is skipped when the matching var is set.
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL":   "google_thinking_level",
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
    "TRADINGAGENTS_ANTHROPIC_EFFORT":        "anthropic_effort",
}


# Zero-cost preset. Every entry below was chosen against a measured free-tier
# limit rather than a marketing page (checked 2026-09-20):
#
#   Gemini free tier  - 1,000 req/day, 15 req/min, 250,000 tokens/min on
#                       flash-lite. MEASURED on a real 50-ticker run
#                       (2026-09-20): the quota ran out after ~33 tickers, so
#                       a ticker costs roughly 30 requests, not the ~13 first
#                       estimated here. Each analyst makes several tool-call
#                       rounds, and every round is its own request. Budget
#                       ~30/ticker: ~33 tickers/day, and a 50-name universe
#                       needs two days or a --limit split.
#   Groq free tier    - 30 req/min but only 6,000 tokens/min. A single market
#                       analyst turn can exceed that on its own, which is why
#                       an earlier Groq batch run died on rate limits.
#   OpenRouter :free  - 20 req/min but 50 req/day at zero balance: about 3
#                       tickers a day. Fine for a one-off, not for a universe.
#   Ollama            - genuinely unlimited and offline, but quality and speed
#                       depend on local hardware, and small local models are
#                       unreliable at the structured output and tool calls this
#                       pipeline depends on. Good for development, not a batch.
#
# Every data source here is already keyless: yfinance for prices and
# statements, NSE/RBI/screener.in for the India context, Reddit RSS and
# StockTwits for sentiment. Alpha Vantage is deliberately avoided (its free
# tier is 25 requests/day) and FRED simply degrades to a sentinel when no key
# is set, so the run continues without it.
FREE_TIER_CONFIG = {
    "llm_provider": "google",
    "deep_think_llm": "gemini-3.1-flash-lite",
    "quick_think_llm": "gemini-3.1-flash-lite",
    "data_vendors": {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "yfinance",
        "news_data": "india_rss,yfinance",
        "macro_data": "fred",
        "prediction_markets": "polymarket",
    },
    # The dominant token cost is not any single payload (all tools together are
    # ~6,100 tokens) but the tool-call loop: the agent re-sends its whole
    # message history each round, so 8 indicators means ~10 rounds and a
    # roughly quadratic climb to ~22,000 tokens. Halving the indicator budget
    # is the single biggest saving available and costs little analytically,
    # since the prompt already tells the model to avoid redundant indicators.
    "market_indicator_budget": 4,
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "india_data_enabled": True,
    "screener_enabled": True,
}


_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")


def _coerce(value: str, reference):
    """Coerce env-var string to the type of the existing default value.

    Invalid values raise ``ValueError`` rather than silently falling back to a
    default — a misspelled boolean (e.g. ``treu``) or non-numeric int should fail
    loudly at startup, not quietly misconfigure an unattended run.
    """
    if isinstance(reference, bool):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
        raise ValueError(
            f"expected a boolean ({'/'.join(_BOOL_TRUE + _BOOL_FALSE)}), got {value!r}"
        )
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


def _apply_env_overrides(config: dict) -> dict:
    """Apply TRADINGAGENTS_* env vars to the config dict in-place."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            config[key] = _coerce(raw, config.get(key))
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc
    return config


DEFAULT_CONFIG = _apply_env_overrides({
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # Optional cap on the number of resolved memory log entries. When set,
    # the oldest resolved entries are pruned once this limit is exceeded.
    # Pending entries are never pruned. None disables rotation entirely.
    "memory_log_max_entries": None,
    # LLM settings
    "llm_provider": "openai",
    "deep_think_llm": "gpt-5.6",
    "quick_think_llm": "gpt-5.6-luna",
    # When None, each provider's client falls back to its own default endpoint
    # (api.openai.com for OpenAI, generativelanguage.googleapis.com for Gemini, ...).
    # The CLI overrides this per provider when the user picks one. Keeping a
    # provider-specific URL here would leak (e.g. OpenAI's /v1 was previously
    # being forwarded to Gemini, producing malformed request URLs).
    "backend_url": None,
    # Provider-specific thinking configuration
    "google_thinking_level": None,      # "high", "minimal", etc.
    "openai_reasoning_effort": None,    # "medium", "high", "low"
    "anthropic_effort": None,           # "high", "medium", "low"
    # Sampling temperature, forwarded to every provider when set. None leaves
    # each provider at its own default. Lower values reduce run-to-run
    # variation on models that honor it; reasoning models largely ignore it
    # and no setting makes LLM output bit-identical across runs (see README).
    "temperature": None,
    # SDK retry budget forwarded to every provider chat client. None leaves each
    # provider/SDK at its own default (usually 2). Raise it to ride out bursty
    # 429 throttling on rate-limited deployments instead of aborting a run (#1091).
    "llm_max_retries": None,
    # Cap on output tokens forwarded to every provider chat client. None leaves
    # each provider at its own default. Set it to bound a model that emits
    # unbounded reasoning/output and hangs or trips a gateway idle timeout
    # (e.g. some deepseek-v4-flash deployments, #1204).
    "max_tokens": None,
    # Checkpoint/resume: when True, LangGraph saves state after each node
    # so a crashed run can resume from the last successful step.
    "checkpoint_enabled": False,
    # Output language for analyst reports and final decision
    # Internal agent debate stays in English for reasoning quality
    "output_language": "English",
    # Debate and discussion settings
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "max_recur_limit": 100,
    # How many technical indicators the market analyst may request. Each one is
    # a separate tool-call round and the agent re-sends its whole message
    # history every round, so this is the main lever on tokens per run: 8
    # indicators cost roughly 22,000 tokens, 4 roughly half that. Lower it on a
    # rate-limited free tier (see FREE_TIER_CONFIG).
    "market_indicator_budget": 8,
    # News / data fetching parameters
    # Increase for longer lookback strategies or to broaden macro coverage;
    # decrease to reduce token usage in agent prompts.
    "news_article_limit": 20,             # max articles per ticker (ticker-news)
    "global_news_article_limit": 10,      # max articles for global/macro news
    "global_news_lookback_days": 7,       # macro news lookback window
    # Reddit is fetched anonymously by default and shares a strict per-IP rate
    # limit with every other analysis on the same network — fine for one
    # ticker, but a multi-ticker batch run can spend more wall time in 429
    # backoffs than in actual analysis. Set REDDIT_CLIENT_ID/SECRET (see
    # dataflows/reddit.py) to switch to Reddit's OAuth API and sidestep that
    # limit entirely, or set this False to skip Reddit and fall back to
    # News + StockTwits only.
    "reddit_enabled": True,
    # Longest a single Reddit fetch may block waiting for the anonymous per-IP
    # rate-limit window to reset. Reddit states the window on every response
    # (x-ratelimit-reset, typically 12-60s); waiting exactly that long is what
    # makes the RSS path return data instead of 429s. Past this cap the fetch
    # is reported unavailable rather than stalling the run, so one ticker's
    # three subreddits cost at most ~3x this. A large batch is still better
    # served by reddit_enabled=False / --no-reddit.
    "reddit_max_wait_seconds": 75,
    # NSE India context (FII/DII flows, India VIX, Nifty PCR, promoter
    # shareholding, corporate actions, exchange announcements) injected into
    # the news and fundamentals analysts for .NS/.BO tickers. Ignored entirely
    # for non-Indian tickers, so leaving it on costs a US run nothing. Set
    # False (or pass --no-india-data) to skip the NSE calls — useful if NSE is
    # blocking your network, since each blocked fetch still costs a timeout
    # before the circuit breaker opens.
    "india_data_enabled": True,
    # How many days before the analysis date an NSE snapshot may be and still
    # be served. 5 covers a weekend plus a market holiday. A snapshot dated
    # AFTER the analysis date is always refused regardless of this setting —
    # that would leak post-decision data into a historical run.
    "india_max_staleness_days": 5,
    # screener.in as a second India source, for the one thing NSE's filings do
    # not carry: the FII/DII split within public shareholding, plus headline
    # ratios (P/E, ROCE, ROE, book value). Off by default — it is a
    # third-party site with no API whose terms cover personal, non-commercial
    # use, so it is a deliberate choice rather than something enabled for
    # everyone. Requests are throttled and cached; NSE stays primary and the
    # block flags any disagreement between the two on promoter holding.
    "screener_enabled": False,
    # Search queries used by get_global_news for macro headlines. Extend or
    # replace to broaden geographic / sector coverage.
    "global_news_queries": [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings GDP economic outlook",
        "geopolitical risk trade war sanctions",
        "ECB Bank of England BOJ central bank policy",
        "oil commodities supply chain energy",
    ],
    # Data vendor configuration
    # Category-level configuration (default for all tools in category).
    # The configured value is the exact vendor chain — requests are NOT silently
    # routed to vendors you didn't choose. For ordered fallback, list several,
    # e.g. "yfinance,alpha_vantage". "default" uses all available vendors.
    "data_vendors": {
        "core_stock_apis": "yfinance",       # Options: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # Options: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # Options: alpha_vantage, yfinance
        "news_data": "yfinance",             # Options: alpha_vantage, yfinance. get_global_news
                                              # (macro/market news) also accepts "india_rss" (ET +
                                              # Mint RSS feeds) — not a default, opt in for Indian
                                              # tickers e.g. "india_rss,yfinance"
        "macro_data": "fred",                # Options: fred (needs FRED_API_KEY)
        "prediction_markets": "polymarket",  # Options: polymarket (keyless)
    },
    # Tool-level configuration (takes precedence over category-level)
    "tool_vendors": {
        # Example: "get_stock_data": "alpha_vantage",  # Override category default
    },
    # Benchmark for alpha calculation in the reflection layer.
    # ``benchmark_ticker`` (when set) overrides the suffix map for all
    # tickers; leave it None to use ``benchmark_map`` for auto-detection
    # based on the ticker's exchange suffix. SPY remains the US default
    # so the reflection label keeps reading "Alpha vs SPY" for US tickers
    # while non-US tickers get their regional index automatically.
    "benchmark_ticker": None,
    "benchmark_map": {
        ".NS":  "^NSEI",       # NSE India (Nifty 50)
        ".BO":  "^BSESN",      # BSE India (Sensex)
        ".T":   "^N225",       # Tokyo (Nikkei 225)
        ".HK":  "^HSI",        # Hong Kong (Hang Seng)
        ".L":   "^FTSE",       # London (FTSE 100)
        ".TO":  "^GSPTSE",     # Toronto (TSX Composite)
        ".AX":  "^AXJO",       # Australia (ASX 200)
        ".SS":  "000001.SS",   # Shanghai (SSE Composite)
        ".SZ":  "399001.SZ",   # Shenzhen (SZSE Component)
        "":     "SPY",         # default for US-listed tickers (no suffix)
    },
})
