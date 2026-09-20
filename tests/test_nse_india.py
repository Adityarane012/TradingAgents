"""Tests for the NSE India fetchers (tradingagents.dataflows.nse_india).

Payloads are real NSE responses captured 2026-09-20 (tests/fixtures/india/),
trimmed but not edited, so a parser change that breaks on real-shaped data is
caught here rather than in production. ``urlopen`` is replaced by a router that
serves them and records every URL requested; nothing touches the network.
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

from tradingagents.dataflows import india_data_common as common, nse_india
from tradingagents.dataflows.india_data_common import (
    IndiaDataInvalid,
    IndiaSourceUnavailable,
    Throttle,
)

FIXTURES = Path(__file__).parent / "fixtures" / "india"
TODAY = "2026-09-20"  # the day the fixtures were captured; a Sunday


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._data if size is None or size < 0 else self._data[:size]


def _chain_for_requested_expiry(url: str):
    """Serve the captured chain, relabelled to whichever expiry was asked for."""
    expiry = parse_qs(urlparse(url).query)["expiry"][0]
    payload = copy.deepcopy(load("nse_option_chain_v3.json"))
    for row in payload["records"]["data"]:
        row["expiryDates"] = expiry
    return payload


class FakeNSE:
    """Routes an NSE API path to a canned payload (or exception / callable)."""

    def __init__(self):
        self.routes = {
            "fiidiiTradeReact": load("nse_fiidii.json"),
            "allIndices": load("nse_allindices.json"),
            "option-chain-contract-info": load("nse_option_contract_info.json"),
            "option-chain-v3": _chain_for_requested_expiry,
            "corporate-share-holdings-master": load("nse_shareholding_reliance.json"),
            "corporates-corporateActions": load("nse_corp_actions_reliance.json"),
            "corporate-announcements": load("nse_announcements_reliance.json"),
        }
        self.urls: list[str] = []
        self.requests: list = []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.urls.append(url)
        self.requests.append(req)
        key = url.split("/api/", 1)[1].split("?", 1)[0]
        payload = self.routes[key]
        if callable(payload):
            payload = payload(url)
        if isinstance(payload, Exception):
            raise payload
        if not isinstance(payload, bytes):
            payload = json.dumps(payload).encode()
        return _Resp(payload)

    def calls_to(self, key: str) -> int:
        return sum(1 for u in self.urls if f"/api/{key}" in u)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    nse_india.reset_state()
    monkeypatch.setattr(nse_india, "_THROTTLE", Throttle(0.0))
    monkeypatch.setattr(common, "get_current_date", lambda: TODAY)
    yield
    nse_india.reset_state()


@pytest.fixture()
def nse():
    fake = FakeNSE()
    with patch.object(nse_india, "urlopen", fake):
        yield fake


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://www.nseindia.com/api/x", code, "err", {}, None)


# --- symbol / date helpers -----------------------------------------------------


class TestNseSymbol:
    @pytest.mark.parametrize(
        "ticker, expected",
        [
            ("RELIANCE.NS", "RELIANCE"),
            ("reliance.ns", "RELIANCE"),
            ("  TCS.NS ", "TCS"),
            ("M&M.NS", "M&M"),
            ("BAJAJ-AUTO.NS", "BAJAJ-AUTO"),
            ("INFY.NSE", "INFY"),
        ],
    )
    def test_nse_tickers(self, ticker, expected):
        assert nse_india.nse_symbol(ticker) == expected

    @pytest.mark.parametrize("ticker", ["RELIANCE.BO", "TCS.BSE"])
    def test_bse_tickers_are_not_guessed(self, ticker):
        with pytest.raises(IndiaSourceUnavailable, match="BSE-listed"):
            nse_india.nse_symbol(ticker)

    @pytest.mark.parametrize("ticker", ["AAPL", "BRK.B", ".NS", "", "7203.T"])
    def test_non_nse_tickers_are_refused(self, ticker):
        with pytest.raises(IndiaSourceUnavailable):
            nse_india.nse_symbol(ticker)


class TestParseNseDatetime:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("18-Sep-2026 15:30", datetime(2026, 9, 18, 15, 30)),
            ("18-Sep-2026 15:40:00", datetime(2026, 9, 18, 15, 40)),
            ("16-JUL-2026 19:24:44", datetime(2026, 7, 16, 19, 24, 44)),
            ("18-Sep-2026", datetime(2026, 9, 18)),
        ],
    )
    def test_known_formats_are_ist_aware(self, raw, expected):
        parsed = nse_india.parse_nse_datetime(raw)
        assert parsed.replace(tzinfo=None) == expected
        assert parsed.utcoffset().total_seconds() == 5.5 * 3600

    @pytest.mark.parametrize("raw", ["", "-", "2026-09-18", "garbage", None, 20260918])
    def test_unparseable_is_none(self, raw):
        assert nse_india.parse_nse_datetime(raw) is None

    def test_date_variant(self):
        assert nse_india.parse_nse_date("30-JUN-2026") == date(2026, 6, 30)


# --- HTTP layer -----------------------------------------------------------------


class TestHttp:
    def test_sends_a_browser_user_agent(self, nse):
        nse_india.get_fii_dii("2026-09-18")
        assert "Mozilla/5.0" in nse.requests[0].get_header("User-agent")

    def test_second_call_is_served_from_cache(self, nse):
        nse_india.get_fii_dii("2026-09-18")
        nse_india.get_fii_dii("2026-09-18")
        assert nse.calls_to("fiidiiTradeReact") == 1

    def test_one_snapshot_serves_every_ticker_in_a_batch(self, nse):
        for _ in range(3):
            nse_india.market_levels_block("2026-09-18")
        assert nse.calls_to("allIndices") == 1

    def test_throttle_is_applied_only_to_network_calls(self, nse, monkeypatch):
        throttle = MagicMock()
        monkeypatch.setattr(nse_india, "_THROTTLE", throttle)
        nse_india.get_fii_dii("2026-09-18")
        nse_india.get_fii_dii("2026-09-18")
        assert throttle.wait.call_count == 1

    def test_http_403_is_unavailable_and_not_cached(self, nse):
        nse.routes["fiidiiTradeReact"] = _http_error(403)
        with pytest.raises(IndiaSourceUnavailable, match="HTTP 403"):
            nse_india.get_fii_dii("2026-09-18")
        nse.routes["fiidiiTradeReact"] = load("nse_fiidii.json")
        assert nse_india.get_fii_dii("2026-09-18").fii_net == pytest.approx(599.54)
        assert nse.calls_to("fiidiiTradeReact") == 2

    def test_404_means_the_endpoint_moved(self, nse):
        nse.routes["fiidiiTradeReact"] = _http_error(404)
        with pytest.raises(IndiaDataInvalid, match="may have changed this API"):
            nse_india.get_fii_dii("2026-09-18")

    def test_404_does_not_open_the_circuit_breaker(self, nse):
        nse.routes["fiidiiTradeReact"] = _http_error(404)
        for _ in range(5):
            with pytest.raises(IndiaDataInvalid):
                nse_india.get_fii_dii("2026-09-18")
        assert nse.calls_to("fiidiiTradeReact") == 5

    @pytest.mark.parametrize("exc", [TimeoutError("timed out"), ConnectionResetError("reset")])
    def test_network_errors_are_unavailable(self, nse, exc):
        nse.routes["fiidiiTradeReact"] = exc
        with pytest.raises(IndiaSourceUnavailable, match=type(exc).__name__):
            nse_india.get_fii_dii("2026-09-18")

    def test_html_block_page_is_invalid(self, nse):
        nse.routes["fiidiiTradeReact"] = b"<html>Access Denied</html>"
        with pytest.raises(IndiaDataInvalid, match="not JSON"):
            nse_india.get_fii_dii("2026-09-18")

    def test_oversized_body_is_refused(self, nse, monkeypatch):
        monkeypatch.setattr(nse_india, "_MAX_BYTES", 10)
        nse.routes["fiidiiTradeReact"] = b"[" + b" " * 100 + b"]"
        with pytest.raises(IndiaSourceUnavailable, match="exceeded"):
            nse_india.get_fii_dii("2026-09-18")

    def test_breaker_stops_calling_nse_after_repeated_failures(self, nse):
        nse.routes["fiidiiTradeReact"] = _http_error(403)
        for _ in range(3):
            with pytest.raises(IndiaSourceUnavailable, match="HTTP 403"):
                nse_india.get_fii_dii("2026-09-18")
        with pytest.raises(IndiaSourceUnavailable, match="circuit breaker"):
            nse_india.get_fii_dii("2026-09-18")
        assert nse.calls_to("fiidiiTradeReact") == 3

    def test_a_success_resets_the_breaker_count(self, nse):
        for _ in range(2):
            nse.routes["fiidiiTradeReact"] = _http_error(403)
            with pytest.raises(IndiaSourceUnavailable):
                nse_india.get_fii_dii("2026-09-18")
        nse.routes["fiidiiTradeReact"] = load("nse_fiidii.json")
        nse_india.get_fii_dii("2026-09-18")
        nse_india.reset_state()  # drop the cached success, keep going
        nse.routes["fiidiiTradeReact"] = _http_error(403)
        for _ in range(2):
            with pytest.raises(IndiaSourceUnavailable, match="HTTP 403"):
                nse_india.get_fii_dii("2026-09-18")


# --- FII / DII -------------------------------------------------------------------


class TestFiiDii:
    def test_parses_the_captured_response(self, nse):
        f = nse_india.get_fii_dii("2026-09-18")
        assert f.as_of == date(2026, 9, 18)
        assert (f.fii_buy, f.fii_sell, f.fii_net) == (38461.63, 37862.09, 599.54)
        assert (f.dii_buy, f.dii_sell, f.dii_net) == (17310.04, 16290.35, 1019.69)

    def test_none_means_a_live_run(self, nse):
        assert nse_india.get_fii_dii().as_of == date(2026, 9, 18)

    def test_net_that_is_not_buy_minus_sell_is_invalid(self, nse):
        payload = load("nse_fiidii.json")
        payload[1]["netValue"] = "700.00"
        nse.routes["fiidiiTradeReact"] = payload
        with pytest.raises(IndiaDataInvalid, match="net 700.00"):
            nse_india.get_fii_dii("2026-09-18")

    def test_missing_category_is_invalid(self, nse):
        nse.routes["fiidiiTradeReact"] = [r for r in load("nse_fiidii.json") if r["category"] != "DII"]
        with pytest.raises(IndiaDataInvalid, match="expected FII and DII"):
            nse_india.get_fii_dii("2026-09-18")

    def test_mismatched_sessions_are_invalid(self, nse):
        payload = load("nse_fiidii.json")
        payload[0]["date"] = "17-Sep-2026"
        nse.routes["fiidiiTradeReact"] = payload
        with pytest.raises(IndiaDataInvalid, match="different sessions"):
            nse_india.get_fii_dii("2026-09-18")

    def test_non_numeric_value_is_invalid(self, nse):
        payload = load("nse_fiidii.json")
        payload[0]["buyValue"] = "-"
        nse.routes["fiidiiTradeReact"] = payload
        with pytest.raises(IndiaDataInvalid, match="non-numeric"):
            nse_india.get_fii_dii("2026-09-18")

    def test_reshaped_response_names_the_likely_cause(self, nse):
        nse.routes["fiidiiTradeReact"] = {"data": []}
        with pytest.raises(IndiaDataInvalid, match="API may have changed"):
            nse_india.get_fii_dii("2026-09-18")

    def test_snapshot_after_the_trade_date_is_refused(self, nse):
        with pytest.raises(IndiaSourceUnavailable, match="leak post-decision data"):
            nse_india.get_fii_dii("2026-09-17")

    def test_stale_snapshot_is_refused(self, nse):
        with pytest.raises(IndiaDataInvalid, match="stale"):
            nse_india.get_fii_dii("2026-09-30")


# --- Nifty / VIX ------------------------------------------------------------------


class TestMarketLevels:
    def test_parses_the_captured_response(self, nse):
        m = nse_india.get_market_levels("2026-09-18")
        assert m.as_of.replace(tzinfo=None) == datetime(2026, 9, 18, 15, 30)
        assert (m.vix.last, m.vix.previous_close, m.vix.change_pct) == (11.39, 12.29, -7.36)
        assert (m.nifty.last, m.nifty.change_pct) == (23346.4, 0.33)

    def _with_vix(self, nse, **changes):
        payload = load("nse_allindices.json")
        for row in payload["data"]:
            if row["indexSymbol"] == "INDIA VIX":
                row.update(changes)
        nse.routes["allIndices"] = payload

    def test_vix_outside_zero_to_hundred_is_invalid(self, nse):
        self._with_vix(nse, last=150.0, previousClose=150.0, percentChange=0.0)
        with pytest.raises(IndiaDataInvalid, match="outside"):
            nse_india.get_market_levels("2026-09-18")

    def test_day_change_that_contradicts_the_levels_is_invalid(self, nse):
        self._with_vix(nse, percentChange=7.36)  # sign flipped
        with pytest.raises(IndiaDataInvalid, match="disagrees"):
            nse_india.get_market_levels("2026-09-18")

    def test_non_positive_level_is_invalid(self, nse):
        self._with_vix(nse, last=0.0)
        with pytest.raises(IndiaDataInvalid, match="non-positive"):
            nse_india.get_market_levels("2026-09-18")

    def test_missing_vix_row_is_invalid(self, nse):
        payload = load("nse_allindices.json")
        payload["data"] = [r for r in payload["data"] if r["indexSymbol"] != "INDIA VIX"]
        nse.routes["allIndices"] = payload
        with pytest.raises(IndiaDataInvalid, match="API may have changed"):
            nse_india.get_market_levels("2026-09-18")

    def test_unparseable_timestamp_is_invalid(self, nse):
        payload = load("nse_allindices.json")
        payload["timestamp"] = "yesterday"
        nse.routes["allIndices"] = payload
        with pytest.raises(IndiaDataInvalid, match="unparseable"):
            nse_india.get_market_levels("2026-09-18")

    def test_snapshot_after_the_trade_date_is_refused(self, nse):
        with pytest.raises(IndiaSourceUnavailable, match="leak post-decision data"):
            nse_india.get_market_levels("2026-01-05")


# --- Nifty PCR ---------------------------------------------------------------------


def _expected_totals():
    """Independent recomputation of the captured chain's open-interest totals."""
    rows = load("nse_option_chain_v3.json")["records"]["data"]
    calls = sum(r["CE"]["openInterest"] for r in rows)
    puts = sum(r["PE"]["openInterest"] for r in rows)
    return calls, puts, len(rows)


class TestNiftyPcr:
    def test_matches_an_independent_sum_of_the_captured_chain(self, nse):
        calls, puts, strikes = _expected_totals()
        p = nse_india.get_nifty_pcr("2026-09-18")
        assert (p.call_oi, p.put_oi, p.strikes) == (calls, puts, strikes)
        assert p.pcr == pytest.approx(puts / calls)
        assert p.expiry == date(2026, 9, 22)
        assert p.underlying == 23346.4
        assert p.as_of.replace(tzinfo=None) == datetime(2026, 9, 18, 15, 40)

    def test_asks_for_the_nearest_expiry_on_or_after_the_trade_date(self, nse):
        nse_india.get_nifty_pcr("2026-09-18")
        assert "expiry=22-Sep-2026" in nse.urls[-1]

    def test_rolls_to_the_next_expiry_once_the_first_has_passed(self, nse):
        p = nse_india.get_nifty_pcr("2026-09-23")
        assert "expiry=29-Sep-2026" in nse.urls[-1]
        assert p.expiry == date(2026, 9, 29)

    def test_rows_for_other_expiries_are_ignored(self, nse):
        calls, puts, _ = _expected_totals()
        payload = _chain_for_requested_expiry("x?expiry=22-Sep-2026")
        stray = copy.deepcopy(payload["records"]["data"][:5])
        for row in stray:
            row["expiryDates"] = "29-Sep-2026"
            row["PE"]["openInterest"] = 10**9
        payload["records"]["data"] += stray
        nse.routes["option-chain-v3"] = payload
        assert nse_india.get_nifty_pcr("2026-09-18").pcr == pytest.approx(puts / calls)

    def _mutate_chain(self, nse, fn):
        payload = _chain_for_requested_expiry("x?expiry=22-Sep-2026")
        fn(payload["records"])
        nse.routes["option-chain-v3"] = payload

    def test_too_few_strikes_is_invalid(self, nse):
        self._mutate_chain(nse, lambda rec: rec.update(data=rec["data"][:5]))
        with pytest.raises(IndiaDataInvalid, match="only 5 strikes"):
            nse_india.get_nifty_pcr("2026-09-18")

    def test_no_call_open_interest_is_invalid(self, nse):
        def zero_calls(rec):
            for row in rec["data"]:
                row["CE"]["openInterest"] = 0

        self._mutate_chain(nse, zero_calls)
        with pytest.raises(IndiaDataInvalid, match="no open interest on one side"):
            nse_india.get_nifty_pcr("2026-09-18")

    def test_implausible_pcr_is_invalid(self, nse):
        def huge_puts(rec):
            for row in rec["data"]:
                row["PE"]["openInterest"] *= 1000

        self._mutate_chain(nse, huge_puts)
        with pytest.raises(IndiaDataInvalid, match="plausible band"):
            nse_india.get_nifty_pcr("2026-09-18")

    def test_missing_underlying_is_invalid(self, nse):
        self._mutate_chain(nse, lambda rec: rec.update(underlyingValue=None))
        with pytest.raises(IndiaDataInvalid, match="underlying"):
            nse_india.get_nifty_pcr("2026-09-18")

    def test_no_expiry_left_is_unavailable(self, nse):
        nse.routes["option-chain-contract-info"] = {
            "expiryDates": ["22-Sep-2026", "29-Sep-2026"],
            "strikePrice": [],
        }
        with pytest.raises(IndiaSourceUnavailable, match="no listed Nifty expiry"):
            nse_india.get_nifty_pcr("2026-10-05")
        assert nse.calls_to("option-chain-v3") == 0

    def test_the_retired_endpoint_is_reported_as_changed(self, nse):
        nse.routes["option-chain-v3"] = _http_error(404)
        with pytest.raises(IndiaDataInvalid, match="may have changed"):
            nse_india.get_nifty_pcr("2026-09-18")

    def test_snapshot_after_the_trade_date_is_refused(self, nse):
        with pytest.raises(IndiaSourceUnavailable, match="leak post-decision data"):
            nse_india.get_nifty_pcr("2026-09-17")


# --- shareholding -------------------------------------------------------------------


class TestShareholding:
    def test_parses_the_captured_filings_newest_first(self, nse):
        rows = nse_india.get_shareholding("RELIANCE.NS", "2026-09-20")
        assert len(rows) == 8
        top = rows[0]
        assert top.quarter_end == date(2026, 6, 30)
        assert (top.promoter_pct, top.public_pct) == (50.48, 49.52)
        assert top.filed.replace(tzinfo=None) == datetime(2026, 7, 16, 19, 24, 44)
        assert [r.quarter_end for r in rows] == sorted((r.quarter_end for r in rows), reverse=True)

    def test_quarters_caps_the_result(self, nse):
        assert len(nse_india.get_shareholding("RELIANCE.NS", "2026-09-20", quarters=3)) == 3

    def test_a_filing_not_yet_public_on_the_trade_date_is_excluded(self, nse):
        # The Jun-2026 quarter was filed on 16-Jul-2026; a run dated 10-Jul must
        # see the March quarter as the latest.
        rows = nse_india.get_shareholding("RELIANCE.NS", "2026-07-10")
        assert rows[0].quarter_end == date(2026, 3, 31)
        assert rows[0].promoter_pct == 50.0

    def test_filed_on_the_trade_date_counts(self, nse):
        rows = nse_india.get_shareholding("RELIANCE.NS", "2026-07-16")
        assert rows[0].quarter_end == date(2026, 6, 30)

    def test_nothing_public_yet_is_unavailable(self, nse):
        with pytest.raises(IndiaSourceUnavailable, match="was public by"):
            nse_india.get_shareholding("RELIANCE.NS", "2000-01-01")

    def test_a_row_that_does_not_sum_to_100_is_skipped(self, nse):
        payload = load("nse_shareholding_reliance.json")
        payload[0]["public_val"] = "10"
        nse.routes["corporate-share-holdings-master"] = payload
        rows = nse_india.get_shareholding("RELIANCE.NS", "2026-09-20")
        assert len(rows) == 7
        assert rows[0].quarter_end == date(2026, 3, 31)

    def test_all_rows_invalid_is_an_error_not_an_empty_answer(self, nse):
        payload = load("nse_shareholding_reliance.json")
        for row in payload:
            row["public_val"] = "1"
        nse.routes["corporate-share-holdings-master"] = payload
        with pytest.raises(IndiaDataInvalid, match="every shareholding row"):
            nse_india.get_shareholding("RELIANCE.NS", "2026-09-20")

    def test_rows_for_another_company_are_never_used(self, nse):
        payload = load("nse_shareholding_reliance.json")
        for row in payload:
            row["symbol"] = "SOMEONEELSE"
        nse.routes["corporate-share-holdings-master"] = payload
        with pytest.raises(IndiaDataInvalid, match="symbol filter ignored"):
            nse_india.get_shareholding("RELIANCE.NS", "2026-09-20")

    def test_a_revision_filed_after_the_trade_date_does_not_replace_the_original(self, nse):
        payload = load("nse_shareholding_reliance.json")
        revised = copy.deepcopy(payload[0])
        revised.update(pr_and_prgrp="49.00", public_val="51.00", broadcastDate="30-JUL-2026 10:00:00")
        nse.routes["corporate-share-holdings-master"] = payload + [revised]

        before = nse_india.get_shareholding("RELIANCE.NS", "2026-07-20")[0]
        assert before.promoter_pct == 50.48  # the revision was not public yet
        nse_india.reset_state()
        after = nse_india.get_shareholding("RELIANCE.NS", "2026-08-01")[0]
        assert after.promoter_pct == 49.0  # now it was

    def test_empty_response_is_unavailable(self, nse):
        nse.routes["corporate-share-holdings-master"] = []
        with pytest.raises(IndiaSourceUnavailable, match="no shareholding filings"):
            nse_india.get_shareholding("RELIANCE.NS", "2026-09-20")

    def test_non_list_response_is_invalid(self, nse):
        nse.routes["corporate-share-holdings-master"] = {"error": "x"}
        with pytest.raises(IndiaDataInvalid, match="not a list"):
            nse_india.get_shareholding("RELIANCE.NS", "2026-09-20")

    def test_bse_ticker_is_refused_before_any_request(self, nse):
        with pytest.raises(IndiaSourceUnavailable, match="BSE-listed"):
            nse_india.get_shareholding("RELIANCE.BO", "2026-09-20")
        assert nse.urls == []

    def test_symbol_with_an_ampersand_is_url_encoded(self, nse):
        nse.routes["corporate-share-holdings-master"] = []
        with pytest.raises(IndiaSourceUnavailable):
            nse_india.get_shareholding("M&M.NS", "2026-09-20")
        assert "symbol=M%26M" in nse.urls[-1]


# --- corporate actions ---------------------------------------------------------------


def _action(ex: str, subject: str = "Dividend - Rs 1 Per Share", symbol: str = "RELIANCE"):
    return {"symbol": symbol, "subject": subject, "exDate": ex, "recDate": ex, "caBroadcastDate": None}


class TestCorporateActions:
    def test_recent_past_actions_only_within_the_history_window(self, nse):
        actions = nse_india.get_corporate_actions("RELIANCE.NS", "2026-09-18")
        assert [a.ex_date for a in actions] == [date(2026, 6, 5)]
        assert actions[0].subject == "Dividend - Rs 6 Per Share"
        assert actions[0].upcoming is False

    def test_wider_history_and_limit(self, nse):
        wide = nse_india.get_corporate_actions("RELIANCE.NS", "2026-09-18", history_days=1000)
        assert [a.ex_date.year for a in wide] == [2026, 2025, 2024, 2024]
        assert len(nse_india.get_corporate_actions(
            "RELIANCE.NS", "2026-09-18", limit=2, history_days=1000)) == 2

    def _with_upcoming(self, nse, ex):
        nse.routes["corporates-corporateActions"] = load("nse_corp_actions_reliance.json") + [
            _action(ex, "Bonus 1:1")
        ]

    def test_upcoming_action_is_shown_on_a_live_run(self, nse):
        self._with_upcoming(nse, "01-Oct-2026")
        actions = nse_india.get_corporate_actions("RELIANCE.NS", TODAY)
        assert actions[0].ex_date == date(2026, 10, 1)
        assert actions[0].upcoming is True

    def test_upcoming_action_is_withheld_from_a_historical_run(self, nse):
        # Its announcement date is unknown, so a run dated before today cannot
        # be shown an ex-date that lies in that run's future.
        self._with_upcoming(nse, "01-Oct-2026")
        actions = nse_india.get_corporate_actions("RELIANCE.NS", "2026-09-18")
        assert all(a.ex_date <= date(2026, 9, 18) for a in actions)

    def test_upcoming_beyond_the_horizon_is_dropped(self, nse):
        self._with_upcoming(nse, "01-Jun-2027")
        actions = nse_india.get_corporate_actions("RELIANCE.NS", TODAY)
        assert all(not a.upcoming for a in actions)

    def test_rows_without_a_usable_ex_date_are_skipped(self, nse):
        nse.routes["corporates-corporateActions"] = [_action("-"), _action("05-Jun-2026")]
        assert len(nse_india.get_corporate_actions("RELIANCE.NS", "2026-09-18")) == 1

    def test_other_companies_rows_are_never_used(self, nse):
        nse.routes["corporates-corporateActions"] = [_action("05-Jun-2026", symbol="TCS")]
        with pytest.raises(IndiaDataInvalid, match="symbol filter ignored"):
            nse_india.get_corporate_actions("RELIANCE.NS", "2026-09-18")


# --- announcements ---------------------------------------------------------------------


class TestAnnouncements:
    def test_asks_nse_for_only_the_window(self, nse):
        nse_india.get_announcements("RELIANCE.NS", "2026-09-18", lookback_days=30)
        query = parse_qs(urlparse(nse.urls[-1]).query)
        assert query["symbol"] == ["RELIANCE"]
        assert query["from_date"] == ["19-08-2026"]
        assert query["to_date"] == ["18-09-2026"]

    def test_material_filings_lead_and_routine_ones_are_separated(self, nse):
        """The captured window is realistic: of six filings, five are
        conference intimations (or their "no UPSI" follow-ups) and one is a
        Rs 12,00,000,000 debenture allotment. The allotment must lead."""
        items = nse_india.get_announcements("RELIANCE.NS", "2026-09-18", limit=3)
        material = [a for a in items if not a.routine]
        routine = [a for a in items if a.routine]
        assert [a.category for a in material] == ["Allotment of Securities"]
        assert len(routine) == 5
        # Each group stays newest-first.
        assert [a.at for a in routine] == sorted((a.at for a in routine), reverse=True)

    def test_items_after_the_trade_date_are_excluded(self, nse):
        items = nse_india.get_announcements("RELIANCE.NS", "2026-09-16")
        assert max(a.at.date() for a in items) == date(2026, 9, 16)
        assert len(items) == 4

    def test_the_ist_calendar_day_decides_not_utc(self, nse):
        payload = load("nse_announcements_reliance.json")
        payload[0]["an_dt"] = "19-Sep-2026 00:30:00"  # 19th in India, still the 18th in UTC
        nse.routes["corporate-announcements"] = payload
        items = nse_india.get_announcements("RELIANCE.NS", "2026-09-18")
        assert all(a.at.date() <= date(2026, 9, 18) for a in items)
        assert len(items) == 5

    def test_lookback_window_is_applied_client_side_too(self, nse):
        items = nse_india.get_announcements("RELIANCE.NS", "2026-09-18", lookback_days=3)
        assert {a.at.date() for a in items} == {date(2026, 9, 16), date(2026, 9, 17)}

    def test_text_whitespace_is_normalised(self, nse):
        payload = load("nse_announcements_reliance.json")
        payload[0]["attchmntText"] = "Line one\r\n\r\n   line   two"
        nse.routes["corporate-announcements"] = payload
        assert nse_india.get_announcements("RELIANCE.NS", "2026-09-18")[0].text == "Line one line two"

    def test_a_window_with_no_filings_is_an_empty_list(self, nse):
        nse.routes["corporate-announcements"] = []
        assert nse_india.get_announcements("RELIANCE.NS", "2026-09-18") == []

    def test_rows_without_a_timestamp_are_skipped(self, nse):
        payload = load("nse_announcements_reliance.json")
        payload[0]["an_dt"] = None
        nse.routes["corporate-announcements"] = payload
        assert len(nse_india.get_announcements("RELIANCE.NS", "2026-09-18")) == 5

    def test_other_companies_rows_are_never_used(self, nse):
        payload = load("nse_announcements_reliance.json")
        for row in payload:
            row["symbol"] = "TCS"
        nse.routes["corporate-announcements"] = payload
        with pytest.raises(IndiaDataInvalid, match="symbol filter ignored"):
            nse_india.get_announcements("RELIANCE.NS", "2026-09-18")


# --- formatted blocks --------------------------------------------------------------------


class TestBlocks:
    def test_fii_dii(self, nse):
        text = nse_india.fii_dii_block("2026-09-18")
        assert "FII/FPI net +599.54 Rs crore" in text
        assert "DII net +1,019.69 Rs crore" in text
        assert "NSE, session of 18-Sep-2026" in text

    def test_market_levels(self, nse):
        text = nse_india.market_levels_block("2026-09-18")
        assert "India VIX 11.39 (-7.36% vs previous close 12.29)" in text
        assert "Nifty 50 23,346.40 (+0.33%)" in text
        assert "as of 18-Sep-2026 15:30 IST" in text

    def test_pcr_states_what_it_measures(self, nse):
        text = nse_india.nifty_pcr_block("2026-09-18")
        assert "open interest, 22-Sep-2026 expiry" in text
        assert "put OI" in text and "call OI" in text
        assert "as of 18-Sep-2026 15:40 IST" in text

    def test_shareholding_table_and_change(self, nse):
        text = nse_india.shareholding_block("RELIANCE.NS", "2026-09-20", quarters=3)
        assert "| 30-Jun-2026 | 50.48 | 49.52 | 16-Jul-2026 |" in text
        assert "FII/DII split not in this source" in text
        assert "Promoter holding change vs previous filing: +0.48 pp" in text

    def test_corporate_actions(self, nse):
        text = nse_india.corporate_actions_block("RELIANCE.NS", "2026-09-18")
        assert "ex-date 05-Jun-2026: Dividend - Rs 6 Per Share, record date 05-Jun-2026" in text

    def test_corporate_actions_none_in_window_is_said_plainly(self, nse):
        nse.routes["corporates-corporateActions"] = [_action("05-Jun-2020")]
        assert "lists no corporate actions" in nse_india.corporate_actions_block(
            "RELIANCE.NS", "2026-09-18"
        )

    def test_long_material_announcements_are_truncated(self, nse):
        nse.routes["corporate-announcements"] = [
            {
                "an_dt": "17-Sep-2026 19:50:00",
                "desc": "Acquisition",
                "attchmntText": "word " * 200,
                "attchmntFile": "",
                "symbol": "RELIANCE",
            }
        ]
        line = nse_india.announcements_block("RELIANCE.NS", "2026-09-18", limit=1).splitlines()[1]
        assert line.startswith("- 17-Sep-2026 19:50 IST | Acquisition: word word")
        assert line.endswith("…")
        assert len(line) < 300

    def test_the_material_filing_leads_the_block(self, nse):
        text = nse_india.announcements_block("RELIANCE.NS", "2026-09-18")
        first = text.splitlines()[1]
        assert "Allotment of Securities" in first
        assert "Debentures" in first
        assert "3x Analysts/Institutional Investor Meet" in text

    def test_announcements_none_in_window_is_said_plainly(self, nse):
        nse.routes["corporate-announcements"] = []
        assert "No NSE announcements filed" in nse_india.announcements_block(
            "RELIANCE.NS", "2026-09-18"
        )

    @pytest.mark.parametrize(
        "call",
        [
            lambda: nse_india.fii_dii_block("2026-09-18"),
            lambda: nse_india.market_levels_block("2026-09-18"),
            lambda: nse_india.nifty_pcr_block("2026-09-18"),
            lambda: nse_india.shareholding_block("RELIANCE.NS", "2026-09-18"),
            lambda: nse_india.corporate_actions_block("RELIANCE.NS", "2026-09-18"),
            lambda: nse_india.announcements_block("RELIANCE.NS", "2026-09-18"),
        ],
    )
    def test_every_block_degrades_to_a_sentinel_when_nse_is_down(self, nse, call):
        for key in list(nse.routes):
            nse.routes[key] = _http_error(403)
        text = call()
        assert text.startswith("<NSE ") and " unavailable: HTTP 403" in text
        assert "not an absence of data" in text

    def test_a_look_ahead_refusal_is_a_sentinel_too(self, nse):
        text = nse_india.fii_dii_block("2026-09-10")
        assert "unavailable" in text and "leak post-decision data" in text

    def test_a_programming_error_is_not_swallowed(self, nse):
        broken = patch.object(nse_india, "get_fii_dii", side_effect=RuntimeError("bug"))
        with broken, pytest.raises(RuntimeError):
            nse_india.fii_dii_block("2026-09-18")


# --- shareholding shapes the Reliance fixture does not cover ----------------------------


class TestShareholdingVariants:
    """Real Infosys and HDFC Bank rows (2026-09-20). The first version of the
    promoter+public==100 check wrongly rejected every Infosys filing because it
    ignored the employee-trusts slice; the live verification script caught it."""

    def _serve(self, nse, symbol):
        nse.routes["corporate-share-holdings-master"] = load("nse_shareholding_variants.json")[
            symbol
        ]

    def test_employee_trusts_count_toward_the_hundred_percent(self, nse):
        self._serve(nse, "INFY")
        rows = nse_india.get_shareholding("INFY.NS", "2026-09-20")
        assert len(rows) == 4
        top = rows[0]
        assert (top.promoter_pct, top.public_pct, top.other_pct) == (13.82, 85.97, 0.21)

    def test_a_row_short_by_more_than_the_trusts_is_still_rejected(self, nse):
        self._serve(nse, "INFY")
        payload = nse.routes["corporate-share-holdings-master"]
        payload[0]["employeeTrusts"] = "0"  # 13.82 + 85.97 = 99.79: no longer sums to 100
        rows = nse_india.get_shareholding("INFY.NS", "2026-09-20")
        assert len(rows) == 3
        assert rows[0].quarter_end == date(2026, 3, 31)

    def test_missing_trust_fields_default_to_zero(self, nse):
        payload = load("nse_shareholding_reliance.json")
        for row in payload:
            row.pop("employeeTrusts", None)
            row.pop("underlyingDrs", None)
        nse.routes["corporate-share-holdings-master"] = payload
        assert nse_india.get_shareholding("RELIANCE.NS", "2026-09-20")[0].other_pct == 0.0

    def test_a_company_with_no_promoter_reports_zero(self, nse):
        self._serve(nse, "HDFCBANK")
        rows = nse_india.get_shareholding("HDFCBANK.NS", "2026-09-20")
        assert all(r.promoter_pct == 0 and r.public_pct == 100 for r in rows)

    def test_the_block_explains_a_zero_promoter_instead_of_leaving_a_bare_zero(self, nse):
        self._serve(nse, "HDFCBANK")
        text = nse_india.shareholding_block("HDFCBANK.NS", "2026-09-20")
        assert "no identified promoter" in text
        assert "not missing data" in text
        assert "Promoter holding change" not in text

    def test_the_trusts_column_appears_only_when_there_is_something_to_show(self, nse):
        self._serve(nse, "INFY")
        infy = nse_india.shareholding_block("INFY.NS", "2026-09-20")
        assert "| Employee trusts % |" in infy
        assert "| 30-Jun-2026 | 13.82 | 85.97 | 0.21 |" in infy
        nse_india.reset_state()
        reliance = nse_india.shareholding_block("RELIANCE.NS", "2026-09-20")
        assert "Employee trusts" not in reliance

    def test_skipped_rows_are_logged_once_not_per_row(self, nse, caplog):
        payload = load("nse_shareholding_reliance.json")
        for row in payload[:5]:
            row["public_val"] = "1"
        nse.routes["corporate-share-holdings-master"] = payload
        with caplog.at_level("WARNING", logger=nse_india.logger.name):
            nse_india.get_shareholding("RELIANCE.NS", "2026-09-20")
        skipped = [r for r in caplog.records if "failed validation" in r.getMessage()]
        assert len(skipped) == 1
        assert "skipped 5 row(s)" in skipped[0].getMessage()


# --- announcement triage ------------------------------------------------------------------


def _ann(at: str, desc: str, text: str = "body text", symbol: str = "RELIANCE") -> dict:
    return {"an_dt": at, "desc": desc, "attchmntText": text, "attchmntFile": "", "symbol": symbol}


class TestAnnouncementTriage:
    """Routine filings are summarised, never dropped.

    A survey of 709 real filings (8 companies, 6.5 months, 2026-09-20) showed
    the generic "Updates" bucket is 25% of volume and carries both noise and
    the most material news in the window — Reliance's Jio Platforms IPO
    observation letter arrived under it. So classification is a short deny-list
    of provably procedural categories, and anything unrecognised is material.
    """

    def test_procedural_categories_are_marked_routine(self, nse):
        nse.routes["corporate-announcements"] = [
            _ann("17-Sep-2026 10:00:00", "Copy of Newspaper Publication"),
            _ann("17-Sep-2026 11:00:00", "Trading Window"),
            _ann("17-Sep-2026 12:00:00", "Analysts/Institutional Investor Meet/Con. Call Updates"),
            _ann(
                "17-Sep-2026 13:00:00",
                "Certificate under SEBI (Depositories and Participants) Regulations, 2018",
            ),
        ]
        items = nse_india.get_announcements("RELIANCE.NS", "2026-09-18")
        assert len(items) == 4
        assert all(a.routine for a in items)

    def test_the_jio_ipo_case_is_kept_material(self, nse):
        """The regression this filter exists to avoid: a generic category
        carrying genuinely market-moving news."""
        nse.routes["corporate-announcements"] = [
            _ann(
                "17-Sep-2026 10:00:00",
                "Updates",
                "Observation Letter on the Draft Red Herring Prospectus for the proposed "
                "Initial Public Offer of Jio Platforms Limited",
            )
        ]
        items = nse_india.get_announcements("RELIANCE.NS", "2026-09-18")
        assert items[0].routine is False
        text = nse_india.announcements_block("RELIANCE.NS", "2026-09-18")
        assert "Jio Platforms" in text

    @pytest.mark.parametrize(
        "category",
        ["Acquisition", "Credit Rating", "Outcome of Board Meeting", "Some New Category 2027"],
    )
    def test_unrecognised_and_material_categories_default_to_material(self, nse, category):
        nse.routes["corporate-announcements"] = [_ann("17-Sep-2026 10:00:00", category)]
        assert nse_india.get_announcements("RELIANCE.NS", "2026-09-18")[0].routine is False

    def test_a_self_certified_non_material_filing_is_routine_whatever_its_category(self, nse):
        nse.routes["corporate-announcements"] = [
            _ann(
                "17-Sep-2026 10:00:00",
                "Updates",
                "Executives participated in the Forum and no unpublished price sensitive "
                "information was shared or discussed in the said meeting.",
            )
        ]
        assert nse_india.get_announcements("RELIANCE.NS", "2026-09-18")[0].routine is True

    def test_routine_filings_are_summarised_not_hidden(self, nse):
        nse.routes["corporate-announcements"] = [
            _ann("17-Sep-2026 10:00:00", "Copy of Newspaper Publication"),
            _ann("17-Sep-2026 11:00:00", "Copy of Newspaper Publication"),
            _ann("17-Sep-2026 12:00:00", "Trading Window"),
            _ann("17-Sep-2026 13:00:00", "Acquisition", "Acquired a stake in Foo Ltd"),
        ]
        text = nse_india.announcements_block("RELIANCE.NS", "2026-09-18")
        assert "Acquired a stake in Foo Ltd" in text
        assert "2x Copy of Newspaper Publication" in text
        assert "1x Trading Window" in text
        assert "detail omitted as procedural" in text

    def test_the_limit_applies_to_material_filings_only(self, nse):
        """A burst of newspaper notices must not crowd out a real filing."""
        rows = [_ann(f"1{i}-Sep-2026 10:00:00", "Copy of Newspaper Publication") for i in range(5)]
        rows.append(_ann("17-Sep-2026 10:00:00", "Acquisition", "Acquired Foo Ltd"))
        rows.append(_ann("16-Sep-2026 10:00:00", "Credit Rating", "Rating upgraded"))
        nse.routes["corporate-announcements"] = rows
        items = nse_india.get_announcements("RELIANCE.NS", "2026-09-18", limit=2)
        material = [a for a in items if not a.routine]
        assert len(material) == 2
        assert {a.category for a in material} == {"Acquisition", "Credit Rating"}
        assert len([a for a in items if a.routine]) == 5

    def test_a_window_of_only_routine_filings_says_so_plainly(self, nse):
        nse.routes["corporate-announcements"] = [
            _ann("17-Sep-2026 10:00:00", "Copy of Newspaper Publication")
        ]
        text = nse_india.announcements_block("RELIANCE.NS", "2026-09-18")
        assert "No substantive NSE announcements" in text
        assert "1x Copy of Newspaper Publication" in text

    def test_category_matching_ignores_case_and_spacing(self, nse):
        nse.routes["corporate-announcements"] = [
            _ann("17-Sep-2026 10:00:00", "  TRADING   WINDOW  ")
        ]
        assert nse_india.get_announcements("RELIANCE.NS", "2026-09-18")[0].routine is True
