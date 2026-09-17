"""Tests for the curated India universe list and its verification helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tradingagents.dataflows.india_universe import NIFTY_50_APPROX, verify_universe


@pytest.mark.unit
class TestNifty50Approx:
    def test_has_fifty_entries(self):
        assert len(NIFTY_50_APPROX) == 50

    def test_no_duplicate_tickers(self):
        assert len(NIFTY_50_APPROX) == len(set(NIFTY_50_APPROX))

    def test_every_ticker_has_ns_suffix(self):
        assert all(t.endswith(".NS") for t in NIFTY_50_APPROX)

    def test_every_entry_has_a_nonempty_name(self):
        assert all(isinstance(name, str) and name.strip() for name in NIFTY_50_APPROX.values())

    def test_tata_motors_uses_post_demerger_symbol(self):
        # TATAMOTORS.NS 404s since the 2025 demerger; regression guard so a
        # future edit doesn't silently reintroduce the dead symbol.
        assert "TATAMOTORS.NS" not in NIFTY_50_APPROX
        assert "TMCV.NS" in NIFTY_50_APPROX


@pytest.mark.unit
class TestVerifyUniverse:
    def _mock_ticker(self, price):
        mock = MagicMock()
        mock.info = {"currentPrice": price} if price else {}
        return mock

    def test_dict_input_splits_good_and_bad(self):
        def fake_ticker(symbol):
            return self._mock_ticker(100.0 if symbol == "GOOD.NS" else None)

        with patch("yfinance.Ticker", side_effect=fake_ticker):
            good, bad = verify_universe({"GOOD.NS": "Good Co", "BAD.NS": "Bad Co"})
        assert good == ["GOOD.NS"]
        assert bad == ["BAD.NS"]

    def test_list_input_also_works(self):
        with patch("yfinance.Ticker", return_value=self._mock_ticker(50.0)):
            good, bad = verify_universe(["AAA.NS", "BBB.NS"])
        assert good == ["AAA.NS", "BBB.NS"]
        assert bad == []

    def test_exception_counts_as_bad_not_a_crash(self):
        with patch("yfinance.Ticker", side_effect=RuntimeError("rate limited")):
            good, bad = verify_universe(["X.NS"])
        assert good == []
        assert bad == ["X.NS"]

    def test_falls_back_to_regular_market_price(self):
        mock = MagicMock()
        mock.info = {"regularMarketPrice": 42.0}  # no currentPrice field
        with patch("yfinance.Ticker", return_value=mock):
            good, bad = verify_universe(["Y.NS"])
        assert good == ["Y.NS"]
