"""Tests for automatic regional news query selection (I-005).

Verifies that TradingAgentsGraph.propagate() injects region-appropriate
``global_news_queries`` based on the ticker's exchange suffix, and that an
explicit user override is never overwritten.
"""

import pytest

from tradingagents.default_config import (
    DEFAULT_CONFIG,
    REGIONAL_NEWS_QUERIES,
    SUFFIX_TO_REGION,
)
from tradingagents.graph.trading_graph import _select_regional_news_queries

# Fast, isolated, no I/O — matches every other file in this suite's use of
# the "unit" marker (pyproject.toml runs `-m unit` as the default fast pass).
# This module previously had no marker at all, so it was silently excluded
# from that pass.
pytestmark = pytest.mark.unit


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


# ---------------------------------------------------------------------------
# Regression tests for _select_regional_news_queries (the actual function
# TradingAgentsGraph.propagate() calls, not a local reimplementation).
#
# The original wiring gated auto-select on `"global_news_queries" in config`,
# which is always True through every real entry point: main.py and
# cli/main.py both build their config as `DEFAULT_CONFIG.copy()`, so the key
# is always present and the presence check never fires in practice. These
# tests reproduce that exact construction pattern rather than a bespoke dict
# that happens to omit the key, which is what let the bug ship.
# ---------------------------------------------------------------------------

def test_fires_for_india_ticker_with_default_config_copy():
    """Reproduces main.py's `config = DEFAULT_CONFIG.copy()` construction."""
    config = DEFAULT_CONFIG.copy()
    result = _select_regional_news_queries("RELIANCE.NS", config["global_news_queries"])
    assert result == REGIONAL_NEWS_QUERIES["IN"]


def test_fires_for_japan_ticker_with_default_config_copy():
    config = DEFAULT_CONFIG.copy()
    result = _select_regional_news_queries("7203.T", config["global_news_queries"])
    assert result == REGIONAL_NEWS_QUERIES["JP"]


def test_no_op_for_us_ticker_with_default_config_copy():
    """US queries already match the default, so selecting them is a no-op —
    but the function must still return the (identical) US list, not None,
    since it cannot distinguish "unmodified" from "explicitly set to the
    same content" and either reading is correct for a US ticker."""
    config = DEFAULT_CONFIG.copy()
    result = _select_regional_news_queries("AAPL", config["global_news_queries"])
    assert result == REGIONAL_NEWS_QUERIES["US"]


def test_respects_explicit_user_override_even_for_india_ticker():
    """A caller who actually customized global_news_queries must never be
    overridden, regardless of the ticker's region."""
    custom = ["my custom query"]
    result = _select_regional_news_queries("RELIANCE.NS", custom)
    assert result is None


def test_none_current_queries_treated_as_customized():
    # A config that never had the key (bespoke dict, not DEFAULT_CONFIG.copy())
    # must not crash and must not silently inject regional queries either —
    # None is not equal to the default list, so it's treated as "leave alone."
    result = _select_regional_news_queries("RELIANCE.NS", None)
    assert result is None


def test_does_not_mutate_default_config():
    """Calling the selector must never mutate DEFAULT_CONFIG's list itself —
    regression guard for the original bug where self.config could literally
    be the DEFAULT_CONFIG object and get mutated in place, leaking one
    ticker's regional queries into every later run's "default"."""
    original = list(DEFAULT_CONFIG["global_news_queries"])
    _select_regional_news_queries("RELIANCE.NS", DEFAULT_CONFIG["global_news_queries"])
    assert DEFAULT_CONFIG["global_news_queries"] == original
