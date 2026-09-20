"""Tests for the RBI policy-rate scraper (tradingagents.dataflows.rbi_rates).

The HTML fixture is the real "Current Rates" table from rbi.org.in, captured
2026-09-20 — same markup, same label wording, same ``: 5.25%`` value format —
so a parser change that breaks on the real page is caught here.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import india_data_common as common, rbi_rates
from tradingagents.dataflows.india_data_common import (
    IndiaDataInvalid,
    IndiaSourceUnavailable,
    Throttle,
)

TODAY = "2026-09-20"


def _row(label: str, value: str) -> str:
    return f"<tr><th>{label}</th><td>: {value}</td></tr>"


def page(**overrides: str) -> str:
    """The real page structure: rate rows inside ``#wrapper``, plus unrelated
    rows (ranges, footnote-marked market data) that must not be mistaken for
    policy rates."""
    rows = {
        "Policy Repo Rate": "5.25%",
        "Standing Deposit Facility Rate": "5.00%",
        "Marginal Standing Facility Rate": "5.50%",
        "Bank Rate": "5.50%",
        "Fixed Reverse Repo Rate": "3.35%",
        "CRR": "3.00%",
        "SLR": "18.00%",
    }
    rows.update(overrides)
    body = "".join(_row(k, v) for k, v in rows.items() if v is not None)
    # Real decoys from the same table: ranged rates, footnoted market levels.
    body += _row("Base Rate", "8.40% - 10.00%")
    body += _row("MCLR (Overnight)", "7.80% - 8.00%")
    body += _row("Call Rates", "4.50% - 5.15% *")
    body += _row("91 day T-bills", "5.2801%*")
    body += _row("Nifty 50", "23270.60 *")
    body += _row("INR / 1 USD", "95.7910")
    return f"<html><body><div id='wrapper'><table>{body}</table></div></body></html>"


class _Resp:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self._data if size is None or size < 0 else self._data[:size]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    rbi_rates.reset_state()
    monkeypatch.setattr(rbi_rates, "_THROTTLE", Throttle(0.0))
    monkeypatch.setattr(common, "get_current_date", lambda: TODAY)
    yield
    rbi_rates.reset_state()


@pytest.fixture()
def serve():
    """Serve a given page body from the patched urlopen; returns a call counter."""
    calls: list[str] = []

    def _serve(html: str | Exception):
        def fake(req, timeout=None):
            calls.append(req.full_url)
            if isinstance(html, Exception):
                raise html
            return _Resp(html.encode())

        patcher = patch.object(rbi_rates, "urlopen", fake)
        patcher.start()
        return calls

    yield _serve
    patch.stopall()


class TestParsing:
    def test_reads_every_policy_rate(self, serve):
        serve(page())
        r = rbi_rates.get_policy_rates(TODAY)
        assert (r.repo, r.sdf, r.msf) == (5.25, 5.00, 5.50)
        assert (r.bank_rate, r.crr, r.slr) == (5.50, 3.00, 18.00)
        assert r.reverse_repo == 3.35

    def test_ranged_and_footnoted_rows_are_not_mistaken_for_rates(self, serve):
        serve(page())
        r = rbi_rates.get_policy_rates(TODAY)
        # Base Rate "8.40% - 10.00%" and Call Rates "4.50% - 5.15% *" sit in the
        # same table; none of their numbers may leak into a policy rate.
        assert 8.40 not in (r.repo, r.sdf, r.msf, r.bank_rate, r.crr, r.slr)
        assert r.bank_rate == 5.50

    def test_labels_are_matched_case_and_space_insensitively(self, serve):
        serve(page().replace("Policy Repo Rate", "POLICY   REPO  RATE"))
        assert rbi_rates.get_policy_rates(TODAY).repo == 5.25

    def test_row_order_does_not_matter(self, serve):
        html = page()
        # Reverse the row order; parsing is by label, not position.
        head, table = html.split("<table>")
        rows = table.replace("</table></div></body></html>", "").split("</tr>")
        shuffled = "</tr>".join(reversed([r for r in rows if r])) + "</tr>"
        serve(f"{head}<table>{shuffled}</table></div></body></html>")
        assert rbi_rates.get_policy_rates(TODAY).repo == 5.25

    def test_optional_reverse_repo_may_be_absent(self, serve):
        serve(page(**{"Fixed Reverse Repo Rate": None}))
        assert rbi_rates.get_policy_rates(TODAY).reverse_repo is None


class TestValidation:
    @pytest.mark.parametrize(
        "missing",
        ["Policy Repo Rate", "Standing Deposit Facility Rate", "CRR", "SLR"],
    )
    def test_a_missing_required_rate_fails_loudly(self, serve, missing):
        serve(page(**{missing: None}))
        with pytest.raises(IndiaDataInvalid, match="layout may have changed"):
            rbi_rates.get_policy_rates(TODAY)

    def test_an_inverted_corridor_is_rejected(self, serve):
        # SDF above MSF cannot happen; it means the labels were misread.
        serve(page(**{"Standing Deposit Facility Rate": "9.00%"}))
        with pytest.raises(IndiaDataInvalid, match="corridor is inverted"):
            rbi_rates.get_policy_rates(TODAY)

    def test_an_implausible_rate_is_rejected(self, serve):
        serve(page(**{"SLR": "1800.00%"}))
        with pytest.raises(IndiaDataInvalid, match="implausible"):
            rbi_rates.get_policy_rates(TODAY)

    def test_an_unparseable_page_fails_rather_than_returning_partial_data(self, serve):
        serve("<html><body><p>Site under maintenance</p></body></html>")
        with pytest.raises(IndiaDataInvalid):
            rbi_rates.get_policy_rates(TODAY)


class TestLiveOnly:
    """The rates box states current values with no as-of date, so it cannot be
    served into a run dated in the past."""

    def test_a_historical_run_is_refused_without_fetching(self, serve):
        calls = serve(page())
        with pytest.raises(IndiaSourceUnavailable, match="no as-of date"):
            rbi_rates.get_policy_rates("2026-03-01")
        assert calls == []

    def test_yesterday_is_still_refused(self, serve):
        serve(page())
        with pytest.raises(IndiaSourceUnavailable):
            rbi_rates.get_policy_rates("2026-09-19")

    def test_today_is_served(self, serve):
        serve(page())
        assert rbi_rates.get_policy_rates(TODAY).repo == 5.25

    def test_none_means_today(self, serve):
        serve(page())
        assert rbi_rates.get_policy_rates().repo == 5.25


class TestTransport:
    def test_the_page_is_fetched_once_and_cached(self, serve):
        calls = serve(page())
        rbi_rates.get_policy_rates(TODAY)
        rbi_rates.get_policy_rates(TODAY)
        assert len(calls) == 1

    def test_http_failure_is_unavailable(self, serve):
        serve(HTTPError("https://www.rbi.org.in/", 503, "err", {}, None))
        with pytest.raises(IndiaSourceUnavailable, match="HTTP 503"):
            rbi_rates.get_policy_rates(TODAY)

    def test_network_failure_is_unavailable(self, serve):
        serve(TimeoutError("timed out"))
        with pytest.raises(IndiaSourceUnavailable, match="TimeoutError"):
            rbi_rates.get_policy_rates(TODAY)

    def test_failures_are_not_cached(self, serve):
        calls = serve(HTTPError("https://www.rbi.org.in/", 503, "err", {}, None))
        for _ in range(2):
            with pytest.raises(IndiaSourceUnavailable):
                rbi_rates.get_policy_rates(TODAY)
        assert len(calls) == 2

    def test_breaker_opens_after_repeated_failures(self, serve):
        calls = serve(HTTPError("https://www.rbi.org.in/", 503, "err", {}, None))
        for _ in range(3):
            with pytest.raises(IndiaSourceUnavailable, match="HTTP 503"):
                rbi_rates.get_policy_rates(TODAY)
        with pytest.raises(IndiaSourceUnavailable, match="circuit breaker"):
            rbi_rates.get_policy_rates(TODAY)
        assert len(calls) == 3


class TestBlock:
    def test_renders_every_rate_with_its_caveat(self, serve):
        serve(page())
        text = rbi_rates.policy_rates_block(TODAY)
        assert "repo 5.25%" in text
        assert "standing deposit facility 5.00%" in text
        assert "marginal standing facility 5.50%" in text
        assert "CRR 3.00%, SLR 18.00%" in text
        assert "without an as-of date" in text

    def test_a_failure_becomes_a_sentinel(self, serve):
        serve(HTTPError("https://www.rbi.org.in/", 503, "err", {}, None))
        text = rbi_rates.policy_rates_block(TODAY)
        assert text.startswith("<RBI policy rates unavailable: HTTP 503")
        assert "not an absence of data" in text

    def test_a_historical_run_explains_itself_in_the_prompt(self, serve):
        serve(page())
        text = rbi_rates.policy_rates_block("2026-03-01")
        assert "unavailable" in text and "no as-of date" in text

    def test_a_programming_error_is_not_swallowed(self, serve):
        serve(page())
        broken = patch.object(rbi_rates, "get_policy_rates", side_effect=RuntimeError("bug"))
        with broken, pytest.raises(RuntimeError):
            rbi_rates.policy_rates_block(TODAY)


class TestStalenessWarning:
    """fred._staleness_warning: the guard that would have caught the dead
    India discount-rate series before it reached an analyst."""

    def test_a_current_monthly_series_is_not_flagged(self):
        from tradingagents.dataflows import fred

        assert fred._staleness_warning("Monthly", "2026-08-01", "2026-09-20") == ""

    def test_a_years_stale_monthly_series_is_flagged(self):
        from tradingagents.dataflows import fred

        # India CPI: newest observation was March 2025 as of 2026-09-20.
        out = fred._staleness_warning("Monthly", "2025-03-01", "2026-09-20")
        assert "Stale series" in out
        assert "2025-03-01" in out and "568 days" in out

    def test_a_daily_series_tolerates_a_weekend(self):
        from tradingagents.dataflows import fred

        assert fred._staleness_warning("Daily", "2026-09-18", "2026-09-20") == ""

    def test_an_annual_series_is_not_flagged_for_being_annual(self):
        from tradingagents.dataflows import fred

        assert fred._staleness_warning("Annual", "2025-01-01", "2026-09-20") == ""

    def test_an_unknown_frequency_is_not_guessed_at(self):
        from tradingagents.dataflows import fred

        assert fred._staleness_warning("", "2000-01-01", "2026-09-20") == ""

    def test_an_unparseable_date_is_not_flagged(self):
        from tradingagents.dataflows import fred

        assert fred._staleness_warning("Monthly", "not-a-date", "2026-09-20") == ""


class TestRetiredAliases:
    def test_the_dead_india_rate_alias_points_at_the_live_source(self):
        from tradingagents.dataflows import fred

        with pytest.raises(ValueError) as exc:
            fred._resolve_series_id("india_discount_rate")
        message = str(exc.value)
        assert "stopped updating in July 2022" in message
        assert "India market data section" in message

    def test_a_plausible_but_wrong_rbi_name_is_also_caught(self):
        from tradingagents.dataflows import fred

        with pytest.raises(ValueError, match="not a FRED series"):
            fred._resolve_series_id("rbi_lending_rate")

    def test_the_raw_discontinued_series_id_still_resolves(self):
        from tradingagents.dataflows import fred

        assert fred._resolve_series_id("INTDSRINM193N") == "INTDSRINM193N"

    def test_get_macro_data_surfaces_the_pointer_instead_of_raising(self):
        from tradingagents.dataflows import fred

        out = fred.get_macro_data("india_discount_rate", "2026-09-20")
        assert out.startswith("FRED: ")
        assert "India market data section" in out


class TestIndiaContextWiring:
    def test_the_rbi_block_reaches_the_news_analyst_context(self):
        from tradingagents.dataflows import india_context
        from tradingagents.dataflows.config import set_config

        set_config({"india_data_enabled": True})
        stubs = dict.fromkeys(
            [
                "fii_dii_block",
                "market_levels_block",
                "nifty_pcr_block",
                "announcements_block",
            ],
            lambda *a, **k: "x",
        )
        with patch.multiple(
            india_context, policy_rates_block=lambda *a, **k: "RBI_STUB", **stubs
        ):
            text = india_context.india_market_context("RELIANCE.NS", TODAY)
        assert "Policy rates: RBI_STUB" in text

    def test_dates_are_forwarded_to_the_rbi_fetcher(self):
        from tradingagents.dataflows import india_context
        from tradingagents.dataflows.config import set_config

        set_config({"india_data_enabled": True})
        seen = []
        stubs = dict.fromkeys(
            ["fii_dii_block", "market_levels_block", "nifty_pcr_block", "announcements_block"],
            lambda *a, **k: "x",
        )
        with patch.multiple(
            india_context,
            policy_rates_block=lambda d=None: seen.append(d) or "x",
            **stubs,
        ):
            india_context.india_market_context("RELIANCE.NS", "2026-09-18")
        assert seen == ["2026-09-18"]


def test_module_dataclass_is_immutable():
    """PolicyRates is frozen so a caller cannot edit a rate in place and pass
    it on as though it came from RBI."""
    rates = rbi_rates.PolicyRates(5.25, 5.0, 5.5, 5.5, 3.0, 18.0)
    with pytest.raises(FrozenInstanceError):
        rates.repo = 9.0  # type: ignore[misc]
    assert rates.repo == 5.25
