"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import os
from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        # `or` not a .get default: an env var present but empty (e.g. a key left
        # blank in a .env copied from .env.example) must still get the placeholder.
        monkeypatch.setenv(env_var, os.environ.get(env_var) or "placeholder")


@pytest.fixture(autouse=True)
def _isolate_config():
    """Reset the global dataflows config before and after each test.

    ``set_config`` merges (it never clears keys absent from the override), so a
    test that sets e.g. ``tool_vendors`` would otherwise leak into later tests
    and make routing behavior order-dependent. Replace the global outright so
    every test starts from a clean DEFAULT_CONFIG.
    """
    import copy

    import tradingagents.dataflows.config as config_module
    import tradingagents.default_config as default_config

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    yield
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


@pytest.fixture(autouse=True)
def _no_live_nse_calls(monkeypatch):
    """Keep NSE off the wire in tests, and make a slip fail loudly.

    The news and fundamentals analysts pre-fetch NSE context for .NS/.BO
    tickers when they build their prompt, so any test that renders one of
    those prompts would otherwise issue real HTTP requests — slow, flaky, and
    rude to NSE. Two layers: the India context is off by default (a test that
    wants it opts in with ``set_config``), and the underlying ``urlopen`` is
    replaced by a raiser, so a future code path that bypasses the config flag
    fails with a clear message instead of silently hitting the network.
    """
    import tradingagents.dataflows.config as config_module
    from tradingagents.dataflows import nse_india

    config_module._config["india_data_enabled"] = False

    def _blocked(*args, **kwargs):
        raise RuntimeError(
            "A test tried to make a live NSE request. Stub the nse_india block "
            "functions (see tests/test_india_context.py) or keep "
            "india_data_enabled False for this test."
        )

    monkeypatch.setattr(nse_india, "urlopen", _blocked)
    nse_india.reset_state()
    yield
    nse_india.reset_state()


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
