"""Tests for the screener.in fetcher (tradingagents.dataflows.screener_in).

The HTML fixture mirrors the real page captured 2026-09-20: ``#top-ratios li``
with ``.name``/``.value`` spans, and the ``#shareholding`` section whose FIRST
table is the quarterly view and second the yearly one. Both carry the "+"
suffix screener renders on holder labels ("Promoters +").
"""

from __future__ import annotations

from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import india_data_common as common, screener_in
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.india_data_common import (
    IndiaDataInvalid,
    IndiaSourceUnavailable,
    Throttle,
)

TODAY = "2026-09-20"

_QUARTERS = ["Mar 2026", "Jun 2026"]
_DEFAULT_ROWS = {
    "Promoters +": ["50.00%", "50.48%"],
    "FIIs +": ["18.67%", "17.19%"],
    "DIIs +": ["20.46%", "21.10%"],
    "Government +": ["0.17%", "0.17%"],
    "Public +": ["10.70%", "11.05%"],
    "No. of Shareholders": ["44,21,289", "46,51,863"],
}


def page(rows: dict | None = None, quarters: list | None = None, ratios: bool = True) -> str:
    rows = _DEFAULT_ROWS if rows is None else rows
    quarters = _QUARTERS if quarters is None else quarters
    head = "".join(f"<th>{q}</th>" for q in quarters)
    body = "".join(
        f"<tr><td class='text'>{label}</td>"
        + "".join(f"<td>{v}</td>" for v in values)
        + "</tr>"
        for label, values in rows.items()
    )
    ratio_html = ""
    if ratios:
        for name, value in (
            ("Market Cap", "₹ 16,59,630 Cr."),
            ("Stock P/E", "22.2"),
            ("Book Value", "₹ 668"),
            ("Dividend Yield", "0.49 %"),
            ("ROCE", "10.3 %"),
            ("ROE", "8.91 %"),
            ("Face Value", "₹ 10.0"),  # not carried; must be ignored
        ):
            ratio_html += f"<li><span class='name'>{name}</span><span class='value'>{value}</span></li>"
    # A second (yearly) table must be ignored: only the first is quarterly.
    yearly = "<table><thead><tr><th>Mar 2017</th></tr></thead><tbody>" \
             "<tr><td class='text'>Promoters +</td><td>46.32%</td></tr></tbody></table>"
    return (
        f"<html><body><ul id='top-ratios'>{ratio_html}</ul>"
        f"<section id='shareholding'>"
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        f"{yearly}</section></body></html>"
    )


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
    screener_in.reset_state()
    monkeypatch.setattr(screener_in, "_THROTTLE", Throttle(0.0))
    monkeypatch.setattr(common, "get_current_date", lambda: TODAY)
    set_config({"screener_enabled": True})
    yield
    screener_in.reset_state()


@pytest.fixture()
def serve():
    calls: list[str] = []

    def _serve(payload):
        def fake(req, timeout=None):
            calls.append(req.full_url)
            item = payload(req.full_url) if callable(payload) else payload
            if isinstance(item, Exception):
                raise item
            return _Resp(item.encode())

        patch.object(screener_in, "urlopen", fake).start()
        return calls

    yield _serve
    patch.stopall()


class TestDisabledByDefault:
    def test_off_unless_explicitly_enabled(self):
        set_config({"screener_enabled": False})
        assert screener_in.screener_enabled() is False

    def test_block_is_empty_when_disabled_and_makes_no_request(self, serve):
        calls = serve(page())
        set_config({"screener_enabled": False})
        assert screener_in.ownership_split_block("RELIANCE.NS", TODAY) == ""
        assert calls == []

    def test_snapshot_raises_when_disabled(self):
        set_config({"screener_enabled": False})
        with pytest.raises(IndiaSourceUnavailable, match="disabled"):
            screener_in.get_snapshot("RELIANCE.NS", TODAY)


class TestParsing:
    def test_reads_the_ownership_split(self, serve):
        serve(page())
        snap = screener_in.get_snapshot("RELIANCE.NS", TODAY)
        latest = snap.holdings[-1]
        assert latest.quarter == "Jun 2026"
        assert (latest.promoters, latest.fii, latest.dii) == (50.48, 17.19, 21.10)
        assert latest.public == 11.05

    def test_reads_only_the_headline_ratios(self, serve):
        serve(page())
        ratios = screener_in.get_snapshot("RELIANCE.NS", TODAY).ratios
        assert ratios["Stock P/E"] == "22.2"
        assert ratios["ROCE"] == "10.3 %"
        assert "Face Value" not in ratios

    def test_reads_the_shareholder_count(self, serve):
        serve(page())
        assert screener_in.get_snapshot("RELIANCE.NS", TODAY).shareholders == "46,51,863"

    def test_the_yearly_table_is_not_mistaken_for_quarterly(self, serve):
        serve(page())
        quarters = [h.quarter for h in screener_in.get_snapshot("RELIANCE.NS", TODAY).holdings]
        assert quarters == ["Mar 2026", "Jun 2026"]
        assert "Mar 2017" not in quarters

    def test_uses_the_consolidated_page(self, serve):
        calls = serve(page())
        screener_in.get_snapshot("RELIANCE.NS", TODAY)
        assert calls[0].endswith("/company/RELIANCE/consolidated/")

    def test_falls_back_to_standalone_on_404(self, serve):
        def router(url):
            return HTTPError(url, 404, "nf", {}, None) if "consolidated" in url else page()

        calls = serve(router)
        assert screener_in.get_snapshot("RELIANCE.NS", TODAY).holdings
        assert len(calls) == 2
        assert calls[1].endswith("/company/RELIANCE/")


class TestValidation:
    def test_a_column_that_does_not_sum_to_100_is_skipped(self, serve):
        rows = {**_DEFAULT_ROWS, "FIIs +": ["18.67%", "1.00%"]}
        serve(page(rows=rows))
        quarters = [h.quarter for h in screener_in.get_snapshot("RELIANCE.NS", TODAY).holdings]
        assert quarters == ["Mar 2026"]

    def test_a_missing_holder_row_fails_loudly(self, serve):
        rows = {k: v for k, v in _DEFAULT_ROWS.items() if not k.startswith("DIIs")}
        serve(page(rows=rows))
        with pytest.raises(IndiaDataInvalid, match="missing dii"):
            screener_in.get_snapshot("RELIANCE.NS", TODAY)

    def test_no_valid_column_is_an_error_not_an_empty_table(self, serve):
        rows = {**_DEFAULT_ROWS, "Promoters +": ["0.00%", "0.00%"]}
        serve(page(rows=rows))
        with pytest.raises(IndiaDataInvalid, match="no shareholding column"):
            screener_in.get_snapshot("RELIANCE.NS", TODAY)

    def test_missing_ratios_fail_loudly(self, serve):
        serve(page(ratios=False))
        with pytest.raises(IndiaDataInvalid, match="no headline ratios"):
            screener_in.get_snapshot("RELIANCE.NS", TODAY)

    def test_an_unrecognisable_page_fails(self, serve):
        serve("<html><body>maintenance</body></html>")
        with pytest.raises(IndiaDataInvalid):
            screener_in.get_snapshot("RELIANCE.NS", TODAY)


class TestScopeAndDates:
    def test_a_historical_run_is_refused_without_fetching(self, serve):
        calls = serve(page())
        with pytest.raises(IndiaSourceUnavailable, match="quarter end rather than filing date"):
            screener_in.get_snapshot("RELIANCE.NS", "2026-03-01")
        assert calls == []

    def test_bse_tickers_are_refused(self, serve):
        calls = serve(page())
        with pytest.raises(IndiaSourceUnavailable, match="BSE-listed"):
            screener_in.get_snapshot("RELIANCE.BO", TODAY)
        assert calls == []

    def test_non_indian_tickers_are_refused(self, serve):
        serve(page())
        with pytest.raises(IndiaSourceUnavailable):
            screener_in.get_snapshot("AAPL", TODAY)


class TestTransport:
    def test_the_page_is_cached_per_symbol(self, serve):
        calls = serve(page())
        screener_in.get_snapshot("RELIANCE.NS", TODAY)
        screener_in.get_snapshot("RELIANCE.NS", TODAY)
        assert len(calls) == 1

    def test_http_error_is_unavailable(self, serve):
        serve(HTTPError("https://www.screener.in/", 503, "err", {}, None))
        with pytest.raises(IndiaSourceUnavailable, match="HTTP 503"):
            screener_in.get_snapshot("RELIANCE.NS", TODAY)

    def test_breaker_opens_after_repeated_failures(self, serve):
        calls = serve(HTTPError("https://www.screener.in/", 503, "err", {}, None))
        for _ in range(3):
            with pytest.raises(IndiaSourceUnavailable, match="HTTP 503"):
                screener_in.get_snapshot("RELIANCE.NS", TODAY)
        with pytest.raises(IndiaSourceUnavailable, match="circuit breaker"):
            screener_in.get_snapshot("RELIANCE.NS", TODAY)
        assert len(calls) == 3

    def test_an_identified_user_agent_is_sent(self, serve):
        seen = {}

        def fake(req, timeout=None):
            seen["ua"] = req.get_header("User-agent")
            return _Resp(page().encode())

        patch.object(screener_in, "urlopen", fake).start()
        screener_in.get_snapshot("RELIANCE.NS", TODAY)
        assert seen["ua"].startswith("tradingagents/")
        assert "Mozilla" not in seen["ua"]


class TestBlock:
    def test_renders_the_split_and_the_trend(self, serve):
        serve(page())
        text = screener_in.ownership_split_block("RELIANCE.NS", TODAY)
        assert "| Jun 2026 | 50.48 | 17.19 | 21.10 | 11.05 |" in text
        assert "FII 18.67% -> 17.19% (-1.48 pp)" in text
        assert "DII 20.46% -> 21.10% (+0.64 pp)" in text
        assert "Retail shareholder count (latest): 46,51,863." in text

    def test_explains_why_it_exists_alongside_nse(self, serve):
        serve(page())
        text = screener_in.ownership_split_block("RELIANCE.NS", TODAY)
        assert "NSE's filings give only promoter vs public" in text
        assert "not point-in-time filing dates" in text

    def test_agreeing_sources_produce_no_warning(self, serve):
        serve(page())
        text = screener_in.ownership_split_block("RELIANCE.NS", TODAY, nse_promoter_pct=50.48)
        assert "Sources disagree" not in text

    def test_a_disagreement_is_surfaced_and_defers_to_nse(self, serve):
        serve(page())
        text = screener_in.ownership_split_block("RELIANCE.NS", TODAY, nse_promoter_pct=61.0)
        assert "Sources disagree" in text
        assert "Prefer the NSE figure" in text

    def test_a_failure_becomes_a_sentinel(self, serve):
        serve(HTTPError("https://www.screener.in/", 503, "err", {}, None))
        text = screener_in.ownership_split_block("RELIANCE.NS", TODAY)
        assert "unavailable: HTTP 503" in text
        assert "not an absence of data" in text


class TestContextWiring:
    def _stub_nse(self):
        from tradingagents.dataflows import india_context

        return patch.multiple(
            india_context,
            shareholding_block=lambda *a, **k: "NSE_SHARES",
            corporate_actions_block=lambda *a, **k: "NSE_ACTIONS",
        )

    def test_the_split_is_appended_to_the_fundamentals_context(self, serve):
        from tradingagents.dataflows import india_context

        serve(page())
        set_config({"india_data_enabled": True, "screener_enabled": True})
        with self._stub_nse(), patch.object(
            india_context, "_nse_promoter_pct", lambda *a, **k: 50.48
        ):
            text = india_context.india_ownership_context("RELIANCE.NS", TODAY)
        assert "NSE_SHARES" in text
        assert "Institutional ownership split (screener.in)" in text
        assert text.index("NSE_SHARES") < text.index("screener.in")

    def test_nothing_is_appended_when_screener_is_off(self, serve):
        from tradingagents.dataflows import india_context

        calls = serve(page())
        set_config({"india_data_enabled": True, "screener_enabled": False})
        with self._stub_nse():
            text = india_context.india_ownership_context("RELIANCE.NS", TODAY)
        assert "screener.in" not in text
        assert calls == []

    def test_the_cross_check_never_breaks_the_prompt(self, serve):
        from tradingagents.dataflows import india_context

        serve(page())
        set_config({"india_data_enabled": True, "screener_enabled": True})
        boom = patch.object(india_context, "get_shareholding", side_effect=RuntimeError("boom"))
        with self._stub_nse(), boom:
            text = india_context.india_ownership_context("RELIANCE.NS", TODAY)
        assert "Institutional ownership split (screener.in)" in text
        assert "Sources disagree" not in text
