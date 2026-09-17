"""StockTwits fetch: transport-error resilience (#1024), crypto symbol
mapping (#1113), and Indian ADR remapping (#1345).

StockTwits lists crypto under ``<BASE>.X`` (Yahoo's ``BTC-USD`` 404s), and
any transport error must degrade to a placeholder rather than raise.

Indian exchange-suffixed tickers (``.NS``/``.BO``/...) are deliberately
*not* handled by stripping the suffix and guessing the bare symbol: most
NSE roots simply 404 on StockTwits, and some collide with an unrelated US
ticker of the same letters (``TCS.NS`` bare-stripped is Container Store
Group Inc, not Tata Consultancy Services; ``ITC.NS`` bare-stripped is ITC
Holdings Corp, a US utility, not ITC Limited) — confirmed live against
api.stocktwits.com. Only the verified ADR mappings in
``_INDIA_ADR_ALIASES`` resolve; every other Indian root must return
``None`` rather than risk a wrong-company result.

The suffix set is explicit for the same reason: a generic "strip after
the last dot" rule would also break dotted symbols that StockTwits *does*
index (``BRK.B``, ``BF.B``) or silently redirect to a different
instrument (``SHEL.L`` → ``$SHEL``, the US ADR).  The parametrized rows
below are chosen to catch that mutation.
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
            # ── Indian tickers with a verified ADR mapping → the ADR symbol ──
            ("INFY.BSE", "INFY"),          # coincidental root/ADR match
            ("infy.ns", "INFY"),           # case-insensitive
            ("WIPRO.NS", "WIT"),
            ("ICICIBANK.NS", "IBN"),
            ("HDFCBANK.BO", "HDB"),
            ("DRREDDY.NS", "RDY"),
            ("VEDL.NS", "VEDL"),           # coincidental root/ADR match
            ("TATAMOTORS.NS", "TTM"),
            # ── Indian tickers with NO verified mapping → None, never a guess ──
            ("RELIANCE.NS", None),         # bare "RELIANCE" 404s on StockTwits
            ("TCS.NSE", None),             # bare "TCS" is Container Store Group
            ("ITC.NS", None),              # bare "ITC" is ITC Holdings Corp (US)
            ("SBIN.NS", None),
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
        """A ticker that IS the suffix (e.g. '.NS') must not produce ''.

        The empty root has no entry in ``_INDIA_ADR_ALIASES``, so the lookup
        naturally falls through to ``None`` (no request made) instead of
        building a ``/symbol/.json`` URL.
        """
        result = stocktwits._stocktwits_symbol(".NS")
        assert result is None


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

    def test_verified_india_adr_requests_adr_endpoint(self):
        url = self._capture_url("HDFCBANK.BO")
        assert "/symbol/HDB.json" in url
        assert "HDFCBANK" not in url

    def test_dotted_symbol_preserved_in_endpoint(self):
        """BRK.B must NOT be stripped — it's a real StockTwits symbol."""
        url = self._capture_url("BRK.B")
        assert "/symbol/BRK.B.json" in url

    def test_unmapped_india_ticker_makes_no_request(self):
        """RELIANCE.NS has no verified mapping — must not guess and fetch."""
        with patch.object(
            stocktwits, "urlopen", side_effect=AssertionError("should not fetch")
        ):
            out = stocktwits.fetch_stocktwits_messages("RELIANCE.NS")
        assert "no StockTwits mapping" in out
        assert "$RELIANCE.NS" in out
