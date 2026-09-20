"""India-aware prompt guidance for the four analysts (#1350).

Each analyst appends extra system-message content when the ticker carries an
NSE/BSE exchange suffix (fundamentals: currency/governance-data caveats;
news: India macro FRED aliases; market: India VIX; sentiment: regional
subreddits + StockTwits coverage-gap framing). These tests capture the
*actual rendered prompt* each node sends, not just that a constant exists in
the module, so a refactor that silently drops the wiring gets caught.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable

from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.agents.analysts.news_analyst import create_news_analyst
from tradingagents.agents.analysts.sentiment_analyst import create_sentiment_analyst
from tradingagents.agents.schemas import SentimentBand, SentimentReport


class _CapturingBoundLLM(Runnable):
    """A real Runnable so `prompt | bound_llm` composes without mock coercion
    ambiguity — invoke() just records the formatted prompt it received."""

    def __init__(self, captured: dict, result):
        self._captured = captured
        self._result = result

    def invoke(self, input, config=None, **kwargs):
        messages = input.to_messages() if hasattr(input, "to_messages") else input
        self._captured["messages"] = messages
        return self._result


def _capturing_tool_llm(captured: dict, result=None):
    """Fake LLM for the bind_tools -> prompt|llm -> invoke shape shared by
    the fundamentals/news/market analysts."""
    if result is None:
        result = AIMessage(content="ok")
    llm = MagicMock()
    llm.bind_tools.return_value = _CapturingBoundLLM(captured, result)
    return llm


def _prompt_text(captured: dict) -> str:
    return "\n".join(str(getattr(m, "content", m)) for m in captured["messages"])


def _state(ticker: str) -> dict:
    return {
        "company_of_interest": ticker,
        "trade_date": "2026-09-17",
        "asset_type": "stock",
        "messages": [],
    }


@pytest.mark.unit
class TestFundamentalsAnalystIndiaPrompt:
    def test_india_ticker_gets_governance_and_currency_caveat(self):
        captured = {}
        create_fundamentals_analyst(_capturing_tool_llm(captured))(_state("RELIANCE.NS"))
        text = _prompt_text(captured)
        assert "promoter shareholding" in text
        assert "rather than estimating or inventing" in text
        assert "Nifty 50" in text

    def test_us_ticker_gets_no_india_caveat(self):
        captured = {}
        create_fundamentals_analyst(_capturing_tool_llm(captured))(_state("AAPL"))
        text = _prompt_text(captured)
        assert "promoter shareholding" not in text
        assert "Nifty 50" not in text

    def test_bo_suffix_also_triggers_guidance(self):
        captured = {}
        create_fundamentals_analyst(_capturing_tool_llm(captured))(_state("HDFCBANK.BO"))
        assert "promoter shareholding" in _prompt_text(captured)


@pytest.mark.unit
class TestNewsAnalystIndiaPrompt:
    def test_india_ticker_gets_macro_alias_guidance(self):
        captured = {}
        create_news_analyst(_capturing_tool_llm(captured))(_state("INFY.NS"))
        text = _prompt_text(captured)
        assert "india_cpi" in text
        assert "usdinr" in text
        # india_discount_rate was deliberately dropped: its FRED series died in
        # July 2022. The prompt now steers to the live RBI rates instead.
        assert "india_discount_rate" not in text
        assert "Do NOT ask it for an RBI policy rate" in text
        assert "RBI Monetary Policy Committee" in text

    def test_us_ticker_gets_no_india_macro_guidance(self):
        captured = {}
        create_news_analyst(_capturing_tool_llm(captured))(_state("NVDA"))
        text = _prompt_text(captured)
        assert "india_cpi" not in text
        assert "usdinr" not in text


@pytest.mark.unit
class TestMarketAnalystIndiaPrompt:
    def test_india_ticker_gets_india_vix_guidance(self):
        captured = {}
        create_market_analyst(_capturing_tool_llm(captured))(_state("TCS.NS"))
        text = _prompt_text(captured)
        assert "^INDIAVIX" in text
        assert "F&O expiry" in text

    def test_us_ticker_gets_no_india_vix_guidance(self):
        captured = {}
        create_market_analyst(_capturing_tool_llm(captured))(_state("MSFT"))
        assert "^INDIAVIX" not in _prompt_text(captured)


def _structured_sentiment_llm(captured: dict, report: SentimentReport | None = None):
    if report is None:
        report = SentimentReport(
            overall_band=SentimentBand.NEUTRAL, overall_score=5.0,
            confidence="low", narrative="placeholder",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or report
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
class TestSentimentAnalystIndiaPrompt:
    @pytest.fixture(autouse=True)
    def _stub_prefetched_sources(self, monkeypatch):
        # Same rationale as test_structured_agents.py: without this, the
        # analyst hits the live network (Reddit/StockTwits/Yahoo).
        from tradingagents.agents.analysts import sentiment_analyst as sentiment

        monkeypatch.setattr(sentiment, "fetch_stocktwits_messages", lambda *a, **k: "st")
        monkeypatch.setattr(sentiment, "fetch_reddit_posts", lambda *a, **k: "rd")
        monkeypatch.setattr(sentiment.get_news, "func", lambda *a, **k: "news", raising=False)

    def test_india_ticker_names_india_subreddits_and_coverage_gap(self):
        captured = {}
        create_sentiment_analyst(_structured_sentiment_llm(captured))(_state("RELIANCE.NS"))
        text = "\n".join(str(m) for m in captured["prompt"])
        assert "r/IndianStreetBets" in text
        assert "r/IndiaInvestments" in text
        assert "r/wallstreetbets" not in text
        assert "coverage gap" in text

    def test_us_ticker_names_default_subreddits_no_coverage_gap_note(self):
        captured = {}
        create_sentiment_analyst(_structured_sentiment_llm(captured))(_state("AAPL"))
        text = "\n".join(str(m) for m in captured["prompt"])
        assert "r/wallstreetbets" in text
        assert "r/IndianStreetBets" not in text
        assert "coverage gap" not in text


@pytest.mark.unit
class TestSentimentAnalystRedditToggle:
    """reddit_enabled=False skips the Reddit fetch entirely — for users
    without REDDIT_CLIENT_ID/SECRET who don't want a multi-ticker batch run
    burning wall time on 429 backoffs against the anonymous RSS path."""

    @pytest.fixture(autouse=True)
    def _stub_non_reddit_sources(self, monkeypatch):
        from tradingagents.agents.analysts import sentiment_analyst as sentiment

        monkeypatch.setattr(sentiment, "fetch_stocktwits_messages", lambda *a, **k: "st")
        monkeypatch.setattr(sentiment.get_news, "func", lambda *a, **k: "news", raising=False)

    def test_disabled_skips_the_fetch_and_says_so(self, monkeypatch):
        from tradingagents.dataflows.config import set_config

        set_config({"reddit_enabled": False})
        from tradingagents.agents.analysts import sentiment_analyst as sentiment

        monkeypatch.setattr(
            sentiment, "fetch_reddit_posts",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch Reddit")),
        )
        captured = {}
        create_sentiment_analyst(_structured_sentiment_llm(captured))(_state("RELIANCE.NS"))
        text = "\n".join(str(m) for m in captured["prompt"])
        assert "Reddit disabled via config" in text

    def test_enabled_by_default_still_fetches(self, monkeypatch):
        from tradingagents.agents.analysts import sentiment_analyst as sentiment

        monkeypatch.setattr(sentiment, "fetch_reddit_posts", lambda *a, **k: "rd")
        captured = {}
        create_sentiment_analyst(_structured_sentiment_llm(captured))(_state("AAPL"))
        text = "\n".join(str(m) for m in captured["prompt"])
        assert "Reddit disabled via config" not in text
