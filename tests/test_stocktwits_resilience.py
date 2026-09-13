"""StockTwits fetch: transport-error resilience (#1024), crypto symbol
mapping (#1113), and exchange-suffix stripping (#1345).

StockTwits lists crypto under ``<BASE>.X`` (Yahoo's ``BTC-USD`` 404s),
Indian equities under bare cashtags (``RELIANCE`` not ``RELIANCE.NS``),
and any transport error must degrade to a placeholder rather than raise.

The suffix set is explicit: a generic "strip after the last dot" rule
would break dotted symbols that StockTwits *does* index (``BRK.B``,
``BF.B``) or silently redirect to a different instrument (``SHEL.L`` →
``$SHEL``, the US ADR).  The parametrized rows below are chosen to catch
that mutation.
"""

from __future__ import annotations

import http.client
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import stocktwits


def _raise(exc):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            raise exc
    return _Resp()


# ── Transport-error resilience ───────────────────────────────────────

@pytest.mark.unit
class TestStockTwitsResilience:
    @pytest.mark.parametrize(
        "exc",
        [
            http.client.IncompleteRead(b""),
            HTTPError("url", 503, "down", {}, None),
            TimeoutError("slow"),
        ],
    )
    def test_transport_errors_return_placeholder(self, exc):
        with patch.object(stocktwits, "urlopen", return_value=_raise(exc)):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert "unavailable" in out.lower()
        assert out.startswith("<stocktwits unavailable")


# ── Symbol mapping (crypto, exchange-suffix, passthrough) ────────────

@pytest.mark.unit
class TestStockTwitsSymbolMapping:
    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            # ── Crypto pairs → <BASE>.X ──
            ("BTC-USD", "BTC.X"),
            ("eth-usd", "ETH.X"),
            ("SOL-USD", "SOL.X"),
            ("BTCUSD", "BTC.X"),       # undashed broker form
            ("BTC-USDT", "BTC.X"),     # stablecoin quote
            # ── Indian exchange suffixes → stripped ──
            ("RELIANCE.NS", "RELIANCE"),
            ("reliance.ns", "RELIANCE"),   # case-insensitive
            ("HDFCBANK.BO", "HDFCBANK"),
            ("TCS.NSE", "TCS"),
            ("INFY.BSE", "INFY"),
            # ── Dotted symbols StockTwits indexes as-is (must NOT strip) ──
            ("BRK.B", "BRK.B"),
            ("BF.B", "BF.B"),
            # ── Non-Indian suffixes (must pass through unchanged) ──
            ("SHEL.L", "SHEL.L"),      # London — $SHEL is the US ADR
            ("7203.T", "7203.T"),      # Tokyo
            # ── Plain equities ──
            ("AMD", "AMD"),
            ("GOLD", "GOLD"),
            ("NVDA", "NVDA"),
            # ── Edge: dashed class share (not crypto) ──
            ("BRK-B", "BRK-B"),
            # ── Edge: unknown base is not crypto ──
            ("XYZ-USD", "XYZ-USD"),
        ],
    )
    def test_symbol_mapping(self, ticker, expected):
        assert stocktwits._stocktwits_symbol(ticker) == expected

    def test_empty_symbol_guard(self):
        """A ticker that IS the suffix (e.g. '.NS') must not produce ''."""
        result = stocktwits._stocktwits_symbol(".NS")
        assert result != ""
        assert result == ".NS"


# ── Endpoint URL integration checks ─────────────────────────────────

@pytest.mark.unit
class TestStockTwitsEndpointURL:
    """Verify the suffix is gone from the URL the fetcher actually opens."""

    def _capture_url(self, ticker: str) -> str:
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            raise TimeoutError("stop after capturing the URL")

        with patch.object(stocktwits, "urlopen", side_effect=fake_urlopen):
            stocktwits.fetch_stocktwits_messages(ticker)
        return seen["url"]

    def test_crypto_pair_requests_dot_x_endpoint(self):
        assert "/symbol/BTC.X.json" in self._capture_url("BTC-USD")

    def test_exchange_suffix_stripped_in_endpoint(self):
        url = self._capture_url("RELIANCE.NS")
        assert "/symbol/RELIANCE.json" in url
        assert ".NS" not in url.split("/symbol/")[1]

    def test_bo_suffix_stripped_in_endpoint(self):
        url = self._capture_url("HDFCBANK.BO")
        assert "/symbol/HDFCBANK.json" in url

    def test_dotted_symbol_preserved_in_endpoint(self):
        """BRK.B must NOT be stripped — it's a real StockTwits symbol."""
        url = self._capture_url("BRK.B")
        assert "/symbol/BRK.B.json" in url
