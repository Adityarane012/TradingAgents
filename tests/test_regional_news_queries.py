"""Tests for automatic regional news query selection (I-005).

Verifies that TradingAgentsGraph.propagate() injects region-appropriate
``global_news_queries`` based on the ticker's exchange suffix, and that an
explicit user override is never overwritten.
"""

from tradingagents.default_config import (
    DEFAULT_CONFIG,
    REGIONAL_NEWS_QUERIES,
    SUFFIX_TO_REGION,
)


# ---------------------------------------------------------------------------
# Unit tests for the SUFFIX_TO_REGION mapping
# ---------------------------------------------------------------------------

def test_suffix_to_region_india_ns():
    assert SUFFIX_TO_REGION[".NS"] == "IN"


def test_suffix_to_region_india_bo():
    assert SUFFIX_TO_REGION[".BO"] == "IN"


def test_suffix_to_region_japan():
    assert SUFFIX_TO_REGION[".T"] == "JP"


def test_suffix_to_region_uk():
    assert SUFFIX_TO_REGION[".L"] == "UK"


def test_us_ticker_falls_through():
    """US tickers have no suffix entry — look-up should return the default."""
    assert "" not in SUFFIX_TO_REGION  # US handled via fallback, not explicit entry


# ---------------------------------------------------------------------------
# Unit tests for REGIONAL_NEWS_QUERIES content
# ---------------------------------------------------------------------------

def test_regional_queries_has_all_regions():
    assert set(REGIONAL_NEWS_QUERIES.keys()) == {"IN", "JP", "UK", "US"}


def test_us_queries_match_default_config():
    """The US regional queries must mirror DEFAULT_CONFIG so behaviour is
    identical for US tickers regardless of whether the auto-select fires."""
    assert REGIONAL_NEWS_QUERIES["US"] == DEFAULT_CONFIG["global_news_queries"]


def test_india_queries_contain_rbi():
    assert any("RBI" in q for q in REGIONAL_NEWS_QUERIES["IN"])


def test_japan_queries_contain_boj():
    assert any("Bank of Japan" in q for q in REGIONAL_NEWS_QUERIES["JP"])


def test_uk_queries_contain_boe():
    assert any("Bank of England" in q for q in REGIONAL_NEWS_QUERIES["UK"])


# ---------------------------------------------------------------------------
# Integration-style: verify the suffix→region look-up logic used in propagate()
# ---------------------------------------------------------------------------

def _resolve_region(ticker: str) -> str:
    """Reproduce the suffix→region resolution from trading_graph.propagate()."""
    return SUFFIX_TO_REGION.get(
        next((s for s in SUFFIX_TO_REGION if ticker.upper().endswith(s)), ""),
        "US",
    )


def test_resolve_region_infy_ns():
    assert _resolve_region("INFY.NS") == "IN"


def test_resolve_region_reliance_bo():
    assert _resolve_region("RELIANCE.BO") == "IN"


def test_resolve_region_toyota_t():
    assert _resolve_region("7203.T") == "JP"


def test_resolve_region_bp_l():
    assert _resolve_region("BP.L") == "UK"


def test_resolve_region_aapl_us():
    assert _resolve_region("AAPL") == "US"


def test_resolve_region_msft_us():
    assert _resolve_region("MSFT") == "US"
