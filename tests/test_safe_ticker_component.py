"""Tests for the ticker path-component validator that blocks directory traversal."""

import os
import unittest

import pytest

from tradingagents.dataflows.utils import safe_ticker_component


@pytest.mark.unit
class TestSafeTickerComponent(unittest.TestCase):
    def test_accepts_common_ticker_formats(self):
        for ticker in ("AAPL", "BRK-B", "BRK.A", "0700.HK", "7203.T", "BHP.AX", "^GSPC"):
            self.assertEqual(safe_ticker_component(ticker), ticker)

    def test_accepts_futures_and_forex_formats(self):
        # Futures use '=' (GC=F gold, CL=F crude), forex/CFD symbols use '+'.
        for ticker in ("GC=F", "CL=F", "ES=F", "XAUUSD+", "EURUSD+"):
            self.assertEqual(safe_ticker_component(ticker), ticker)

    def test_rejects_path_separators(self):
        for bad in (".", "..", "../etc", "a/b", "a\\b", "/abs", "..\\..\\x"):
            with self.assertRaises(ValueError):
                safe_ticker_component(bad)

    def test_rejects_null_byte_and_whitespace(self):
        for bad in ("AAP L", "AAPL\x00", "AAPL\n", "\tAAPL"):
            with self.assertRaises(ValueError):
                safe_ticker_component(bad)

    def test_rejects_empty_or_non_string(self):
        for bad in ("", None, 123, b"AAPL"):
            with self.assertRaises(ValueError):
                safe_ticker_component(bad)

    def test_rejects_overlong_input(self):
        with self.assertRaises(ValueError):
            safe_ticker_component("A" * 33)

    def test_rejects_dot_only_values(self):
        # '.' and '..' pass the regex but traverse when used as a path
        # component (e.g. ``Path(results_dir) / ticker / "logs"``).
        for bad in (".", "..", "...", "...."):
            with self.assertRaises(ValueError):
                safe_ticker_component(bad)

    def test_traversal_string_does_not_escape_join(self):
        """Sanity: sanitized values stay within base when joined."""
        base = os.path.realpath("/tmp/cache")
        ticker = safe_ticker_component("AAPL")
        joined = os.path.realpath(os.path.join(base, f"{ticker}.csv"))
        self.assertTrue(joined.startswith(base + os.sep))


if __name__ == "__main__":
    unittest.main()


@pytest.mark.unit
class TestAmpersandTickers(unittest.TestCase):
    """NSE symbols can contain '&' — M&M.NS (Mahindra & Mahindra) is a Nifty 50
    constituent. A live batch run failed on it with "characters not allowed in
    a filesystem path", losing the whole ticker over a character that is legal
    in a filename on every platform this runs on.
    """

    def test_accepts_nse_ampersand_symbols(self):
        for ticker in ("M&M.NS", "M&MFIN.NS", "L&T.NS"):
            self.assertEqual(safe_ticker_component(ticker), ticker)

    def test_an_ampersand_ticker_stays_inside_its_directory(self):
        """The property the validator actually protects: the value cannot
        escape the directory it is joined onto."""
        base = os.path.join("results", "reports")
        joined = os.path.join(base, safe_ticker_component("M&M.NS"))
        self.assertEqual(os.path.normpath(joined), os.path.normpath("results/reports/M&M.NS"))
        self.assertTrue(os.path.normpath(joined).startswith(os.path.normpath(base)))

    def test_shell_metacharacters_other_than_ampersand_are_still_rejected(self):
        """'&' is allowed only because nothing here executes a shell; the rest
        of the metacharacter set stays blocked regardless."""
        for bad in ("a;rm -rf /", "a|b", "a$b", "a`b`", "a>b", "a<b", "a b", "a'b", 'a"b'):
            with self.assertRaises(ValueError):
                safe_ticker_component(bad)

    def test_traversal_with_an_ampersand_is_still_rejected(self):
        for bad in ("../M&M", "M&M/../..", "a/&/b"):
            with self.assertRaises(ValueError):
                safe_ticker_component(bad)
