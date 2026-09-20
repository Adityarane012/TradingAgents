"""Tests for the zero-cost preset (FREE_TIER_CONFIG) and the token lever it sets.

The preset exists because free tiers are bounded by different things and only
one of them clears this pipeline's token budget. Measured 2026-09-20:

    Gemini flash-lite  500 req/day (this key; guides claim 1,000), 15 rpm,
                       250,000 tokens/min -> ~33 tickers at ~15 requests each
    Groq                  30 rpm but 6,000 tokens/min  <- a single market
                          analyst turn can exceed this on its own
    OpenRouter :free      20 rpm but 50 req/day at zero balance
    Ollama                unlimited, but local models are unreliable at the
                          structured output and tool calls this pipeline needs

These tests pin the choices that follow from those numbers, so a later edit
cannot quietly point the preset at a tier that cannot run a batch.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable

from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.dataflows.config import get_config, set_config
from tradingagents.default_config import DEFAULT_CONFIG, FREE_TIER_CONFIG


class TestPresetContents:
    def test_uses_the_only_free_tier_that_fits_a_batch(self):
        assert FREE_TIER_CONFIG["llm_provider"] == "google"
        assert "flash-lite" in FREE_TIER_CONFIG["deep_think_llm"]
        assert "flash-lite" in FREE_TIER_CONFIG["quick_think_llm"]

    def test_every_data_vendor_is_keyless(self):
        """Alpha Vantage's free tier is 25 requests/day, which one ticker can
        exhaust, so the preset must never select it."""
        vendors = FREE_TIER_CONFIG["data_vendors"]
        assert "alpha_vantage" not in ",".join(vendors.values())
        assert vendors["core_stock_apis"] == "yfinance"
        assert vendors["fundamental_data"] == "yfinance"

    def test_keyless_india_sources_are_on(self):
        assert FREE_TIER_CONFIG["india_data_enabled"] is True
        assert FREE_TIER_CONFIG["screener_enabled"] is True

    def test_debate_rounds_stay_minimal(self):
        assert FREE_TIER_CONFIG["max_debate_rounds"] == 1
        assert FREE_TIER_CONFIG["max_risk_discuss_rounds"] == 1

    def test_the_indicator_budget_is_reduced(self):
        assert FREE_TIER_CONFIG["market_indicator_budget"] < DEFAULT_CONFIG[
            "market_indicator_budget"
        ]

    def test_every_preset_key_is_a_real_config_key(self):
        """A typo here would silently do nothing."""
        unknown = set(FREE_TIER_CONFIG) - set(DEFAULT_CONFIG)
        assert unknown == set()


class TestIndicatorBudget:
    """The budget is the main lever on tokens per run: each indicator is a
    separate tool-call round and the agent re-sends its whole message history
    every round, so 8 indicators cost ~22,000 tokens against ~6,100 for the
    underlying data."""

    @staticmethod
    def _rendered_prompt() -> str:
        captured: dict = {}

        class _Bound(Runnable):
            def invoke(self, input, config=None, **kwargs):
                messages = input.to_messages() if hasattr(input, "to_messages") else input
                captured["text"] = "\n".join(
                    str(getattr(m, "content", m)) for m in messages
                )
                return AIMessage(content="ok")

        llm = MagicMock()
        llm.bind_tools.return_value = _Bound()
        create_market_analyst(llm)(
            {
                "company_of_interest": "RELIANCE.NS",
                "trade_date": "2026-09-18",
                "messages": [],
                "asset_type": "stock",
            }
        )
        return captured["text"]

    def test_default_budget_is_unchanged(self):
        assert "up to **8 indicators**" in self._rendered_prompt()

    @pytest.mark.parametrize("budget", [3, 4, 6])
    def test_the_configured_budget_reaches_the_prompt(self, budget):
        set_config({"market_indicator_budget": budget})
        assert f"up to **{budget} indicators**" in self._rendered_prompt()

    def test_the_indicator_menu_survives_the_substitution(self):
        """The budget is interpolated into a long prompt that lists every
        indicator; a broken f-string would silently truncate that menu."""
        set_config({"market_indicator_budget": 4})
        text = self._rendered_prompt()
        for indicator in ("close_50_sma", "macd", "rsi", "boll_ub", "atr"):
            assert indicator in text

    def test_applying_the_preset_lowers_the_budget_in_the_live_config(self):
        set_config(FREE_TIER_CONFIG)
        assert get_config()["market_indicator_budget"] == 4
        assert "up to **4 indicators**" in self._rendered_prompt()
