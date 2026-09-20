"""Tests for the India context blocks and their wiring into the analysts.

The NSE fetchers are stubbed here — nse_india's own tests cover the parsing.
What matters at this level is the wiring: the right analyst gets the right
block, a non-Indian ticker gets nothing, the opt-out is honoured, and a dead
NSE degrades instead of failing the analysis.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable

from tradingagents.dataflows import india_context
from tradingagents.dataflows.config import set_config

# The six nse_india blocks india_context composes, with a recognisable stub
# value each so a test can assert which ones reached which prompt.
_BLOCKS = {
    "fii_dii_block": "FII_STUB",
    "market_levels_block": "LEVELS_STUB",
    "nifty_pcr_block": "PCR_STUB",
    "announcements_block": "ANNOUNCE_STUB",
    "shareholding_block": "SHARES_STUB",
    "corporate_actions_block": "ACTIONS_STUB",
}


@pytest.fixture(autouse=True)
def _india_data_on():
    """conftest turns the India context off for the whole suite so no test can
    reach NSE by accident; this file is the one that exercises it, so turn it
    back on here (with the fetchers stubbed below, nothing touches the wire)."""
    set_config({"india_data_enabled": True})


@pytest.fixture()
def stub_nse():
    """Replace every nse_india block with a constant, recording its calls."""
    with patch.multiple(
        india_context,
        **{name: lambda *a, _v=value, **k: _v for name, value in _BLOCKS.items()},
    ):
        yield


class TestScope:
    @pytest.mark.parametrize("ticker", ["RELIANCE.NS", "TCS.NS", "RELIANCE.BO", "infy.ns"])
    def test_indian_tickers_are_in_scope(self, ticker):
        assert india_context.india_data_enabled(ticker) is True

    @pytest.mark.parametrize("ticker", ["AAPL", "BRK.B", "7203.T", "BTC-USD", ""])
    def test_non_indian_tickers_are_not(self, ticker):
        assert india_context.india_data_enabled(ticker) is False

    def test_the_config_flag_disables_it(self):
        set_config({"india_data_enabled": False})
        assert india_context.india_data_enabled("RELIANCE.NS") is False

    def test_out_of_scope_tickers_make_no_network_calls(self, stub_nse):
        called = []
        with patch.object(india_context, "fii_dii_block", lambda *a, **k: called.append(1) or ""):
            assert india_context.india_market_context("AAPL", "2026-09-18") == ""
            set_config({"india_data_enabled": False})
            assert india_context.india_market_context("RELIANCE.NS", "2026-09-18") == ""
        assert called == []


class TestMarketContext:
    def test_carries_the_market_wide_blocks_and_announcements(self, stub_nse):
        text = india_context.india_market_context("RELIANCE.NS", "2026-09-18")
        for stub in ("FII_STUB", "LEVELS_STUB", "PCR_STUB", "ANNOUNCE_STUB"):
            assert stub in text

    def test_does_not_carry_the_fundamentals_blocks(self, stub_nse):
        text = india_context.india_market_context("RELIANCE.NS", "2026-09-18")
        assert "SHARES_STUB" not in text
        assert "ACTIONS_STUB" not in text

    def test_tells_the_model_an_unavailable_line_is_not_a_zero(self, stub_nse):
        text = india_context.india_market_context("RELIANCE.NS", "2026-09-18")
        assert "NOT the same as the value being zero" in text
        assert "no tool to re-query" in text

    def test_pcr_is_labelled_as_contested(self, stub_nse):
        text = india_context.india_market_context("RELIANCE.NS", "2026-09-18")
        assert "contrarian" in text and "debated" in text

    def test_the_trade_date_is_passed_through_to_every_fetcher(self):
        seen = []

        def record(*args, **kwargs):
            seen.append(args[-1] if len(args) > 1 else args[0])
            return "x"

        with patch.multiple(india_context, **dict.fromkeys(_BLOCKS, record)):
            india_context.india_market_context("RELIANCE.NS", "2026-09-18")
        assert seen and all(d == "2026-09-18" for d in seen)


class TestOwnershipContext:
    def test_carries_shareholding_and_corporate_actions(self, stub_nse):
        text = india_context.india_ownership_context("RELIANCE.NS", "2026-09-18")
        assert "SHARES_STUB" in text and "ACTIONS_STUB" in text

    def test_does_not_carry_the_market_wide_blocks(self, stub_nse):
        text = india_context.india_ownership_context("RELIANCE.NS", "2026-09-18")
        for stub in ("FII_STUB", "LEVELS_STUB", "PCR_STUB"):
            assert stub not in text

    def test_tells_the_model_to_distrust_the_vendor_insider_field(self, stub_nse):
        text = india_context.india_ownership_context("RELIANCE.NS", "2026-09-18")
        assert "Prefer these figures" in text
        assert "inaccurately" in text

    def test_states_the_point_in_time_rule(self, stub_nse):
        text = india_context.india_ownership_context("RELIANCE.NS", "2026-09-18")
        assert "public on or before the analysis date" in text


class TestSentinelsSurviveIntoThePrompt:
    """A failed fetch must reach the model as an explicit sentinel, not as a
    gap the model could read as 'nothing happened'."""

    @pytest.fixture()
    def dead_nse(self):
        dead = "<NSE FII/DII flows unavailable: HTTP 403; this is not an absence of data>"
        with patch.multiple(india_context, **dict.fromkeys(_BLOCKS, lambda *a, **k: dead)):
            yield dead

    def test_market_context_still_renders(self, dead_nse):
        text = india_context.india_market_context("RELIANCE.NS", "2026-09-18")
        assert dead_nse in text
        assert "not an absence of data" in text

    def test_ownership_context_still_renders(self, dead_nse):
        assert dead_nse in india_context.india_ownership_context("RELIANCE.NS", "2026-09-18")


class TestAnalystWiring:
    """The prompt each analyst actually builds. These call the real analyst
    factories with a fake LLM and read back the system message."""

    @staticmethod
    def _capture_prompt(create_analyst, state):
        """Render an analyst's real prompt and return it as text.

        Same shape as tests/test_india_analyst_prompts.py: bind_tools returns a
        genuine Runnable so ``prompt | llm`` composes, and invoke records the
        formatted messages instead of calling a model.
        """
        captured: dict = {}

        class _CapturingBoundLLM(Runnable):
            def invoke(self, input, config=None, **kwargs):
                messages = input.to_messages() if hasattr(input, "to_messages") else input
                captured["messages"] = messages
                return AIMessage(content="ok")

        llm = MagicMock()
        llm.bind_tools.return_value = _CapturingBoundLLM()
        create_analyst(llm)(state)
        return "\n".join(str(getattr(m, "content", m)) for m in captured["messages"])

    def _state(self, ticker):
        return {
            "company_of_interest": ticker,
            "trade_date": "2026-09-18",
            "messages": [],
            "asset_type": "stock",
        }

    def test_news_analyst_gets_the_market_block(self, stub_nse):
        from tradingagents.agents.analysts.news_analyst import create_news_analyst

        system = self._capture_prompt(create_news_analyst, self._state("RELIANCE.NS"))
        assert "FII_STUB" in system and "PCR_STUB" in system and "ANNOUNCE_STUB" in system
        assert "SHARES_STUB" not in system

    def test_fundamentals_analyst_gets_the_ownership_block(self, stub_nse):
        from tradingagents.agents.analysts.fundamentals_analyst import (
            create_fundamentals_analyst,
        )

        system = self._capture_prompt(create_fundamentals_analyst, self._state("RELIANCE.NS"))
        assert "SHARES_STUB" in system and "ACTIONS_STUB" in system
        assert "FII_STUB" not in system

    def test_us_tickers_get_no_india_block_in_either_analyst(self, stub_nse):
        from tradingagents.agents.analysts.fundamentals_analyst import (
            create_fundamentals_analyst,
        )
        from tradingagents.agents.analysts.news_analyst import create_news_analyst

        for factory in (create_news_analyst, create_fundamentals_analyst):
            system = self._capture_prompt(factory, self._state("AAPL"))
            assert not any(stub in system for stub in _BLOCKS.values())
            assert "NSE" not in system

    def test_the_fundamentals_note_no_longer_calls_shareholding_unavailable(self, stub_nse):
        from tradingagents.agents.analysts.fundamentals_analyst import (
            create_fundamentals_analyst,
        )

        system = self._capture_prompt(create_fundamentals_analyst, self._state("RELIANCE.NS"))
        # The old note listed promoter shareholding among the things the model
        # could not see. It is now in the prompt, so the note must not say so.
        assert "promoter shareholding is provided below" in system
        # Pledging and the FII/DII split genuinely are still missing.
        assert "pledging" in system and "NOT available" in system

    def test_disabling_india_data_removes_it_from_both_prompts(self, stub_nse):
        from tradingagents.agents.analysts.fundamentals_analyst import (
            create_fundamentals_analyst,
        )
        from tradingagents.agents.analysts.news_analyst import create_news_analyst

        set_config({"india_data_enabled": False})
        for factory in (create_news_analyst, create_fundamentals_analyst):
            system = self._capture_prompt(factory, self._state("RELIANCE.NS"))
            assert not any(stub in system for stub in _BLOCKS.values())
        # ...but the India prompt guidance itself stays: the ticker is still Indian.
        system = self._capture_prompt(create_news_analyst, self._state("RELIANCE.NS"))
        assert "Indian (NSE/BSE) equity" in system

    def test_a_dead_nse_does_not_break_either_analyst(self):
        from tradingagents.agents.analysts.fundamentals_analyst import (
            create_fundamentals_analyst,
        )
        from tradingagents.agents.analysts.news_analyst import create_news_analyst

        dead = "<NSE unavailable: HTTP 403; this is not an absence of data>"
        with patch.multiple(india_context, **dict.fromkeys(_BLOCKS, lambda *a, **k: dead)):
            for factory in (create_news_analyst, create_fundamentals_analyst):
                system = self._capture_prompt(factory, self._state("RELIANCE.NS"))
                assert dead in system
