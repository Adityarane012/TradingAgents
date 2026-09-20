"""Tests for the Indian market news RSS vendor (get_global_news_india, #1350).

Feed fixtures mirror the real Economic Times / Mint RSS structure observed
live on 2026-09-17 (CDATA-wrapped title/link/description, RFC 822 pubDate),
so a change to the parsing logic that breaks on real-shaped XML gets caught
here rather than only in production.
"""

from __future__ import annotations

import http.client
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import india_news

_ET_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Stocks-Markets-Economic Times</title>
<item>
<title><![CDATA[Sensex, Nifty rally on strong FII inflows]]></title>
<description><![CDATA[Benchmark indices closed higher, led by banking stocks.]]></description>
<link>https://economictimes.indiatimes.com/markets/stocks/news/sensex-nifty-rally/articleshow/1.cms</link>
<pubDate>Thu, 17 Sep 2026 13:26:11 +0530</pubDate>
</item>
<item>
<title><![CDATA[RBI holds repo rate steady at MPC meeting]]></title>
<description><![CDATA[The central bank kept rates unchanged citing inflation concerns.]]></description>
<link>https://economictimes.indiatimes.com/markets/stocks/news/rbi-holds-rate/articleshow/2.cms</link>
<pubDate>Wed, 16 Sep 2026 09:00:00 +0530</pubDate>
</item>
</channel></rss>
"""

_MINT_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:media="http://search.yahoo.com/mrss/" version="2.0"><channel>
<title>mint - markets</title>
<item>
<title><![CDATA[IPO GMP today: three new listings open for subscription]]></title>
<link><![CDATA[https://www.livemint.com/market/ipo/gmp-today.html]]></link>
<description><![CDATA[Grey market premiums point to a strong debut for all three issues.]]></description>
<pubDate><![CDATA[Thu, 17 Sep 2026 14:10:48 +0530]]></pubDate>
<guid><![CDATA[https://www.livemint.com/market/ipo/gmp-today.html]]></guid>
</item>
</channel></rss>
"""


def _resp(data: bytes):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner, size=-1):
            return data if size is None or size < 0 else data[:size]
    return _Resp()


def _raise(exc):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner, size=-1):
            raise exc
    return _Resp()


@pytest.mark.unit
class TestParsePubDate:
    def test_parses_rfc_822_with_offset(self):
        dt = india_news._parse_pub_date("Thu, 17 Sep 2026 13:26:11 +0530")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 9 and dt.day == 17

    def test_none_and_garbage_return_none(self):
        assert india_news._parse_pub_date(None) is None
        assert india_news._parse_pub_date("") is None
        assert india_news._parse_pub_date("not a date") is None


@pytest.mark.unit
class TestFetchFeed:
    def test_parses_et_style_feed(self):
        with patch.object(india_news, "urlopen", return_value=_resp(_ET_SAMPLE.encode())):
            items = india_news._fetch_feed("Economic Times", "https://example.com/rss")
        assert len(items) == 2
        assert items[0]["title"] == "Sensex, Nifty rally on strong FII inflows"
        assert items[0]["source"] == "Economic Times"
        assert "articleshow/1.cms" in items[0]["link"]
        assert items[0]["pub_date"] is not None

    def test_parses_mint_style_feed_with_cdata_wrapped_link(self):
        with patch.object(india_news, "urlopen", return_value=_resp(_MINT_SAMPLE.encode())):
            items = india_news._fetch_feed("Mint", "https://example.com/rss")
        assert len(items) == 1
        assert items[0]["link"] == "https://www.livemint.com/market/ipo/gmp-today.html"

    @pytest.mark.parametrize(
        "exc",
        [
            HTTPError("url", 403, "forbidden", {}, None),
            TimeoutError("slow"),
            http.client.IncompleteRead(b""),
        ],
    )
    def test_transport_errors_return_none_not_empty_list(self, exc):
        # None (failed) must stay distinguishable from [] (genuinely empty).
        with patch.object(india_news, "urlopen", return_value=_raise(exc)):
            assert india_news._fetch_feed("Economic Times", "https://example.com/rss") is None

    def test_malformed_xml_returns_none(self):
        with patch.object(india_news, "urlopen", return_value=_resp(b"<rss><item>")):
            assert india_news._fetch_feed("Economic Times", "https://example.com/rss") is None


@pytest.mark.unit
class TestGetGlobalNewsIndia:
    def _patch_feeds(self, et_result, mint_result):
        def fake_fetch(name, url):
            return et_result if name == "Economic Times" else mint_result
        return patch.object(india_news, "_fetch_feed", side_effect=fake_fetch)

    def test_combines_both_feeds_within_window(self):
        with patch.object(india_news, "urlopen") as mock_open:
            mock_open.side_effect = lambda req, timeout=None: (
                _resp(_ET_SAMPLE.encode()) if "economictimes" in req.full_url
                else _resp(_MINT_SAMPLE.encode())
            )
            out = india_news.get_global_news_india("2026-09-17", look_back_days=7, limit=10)
        assert "Sensex, Nifty rally" in out
        assert "RBI holds repo rate" in out
        assert "IPO GMP today" in out
        assert "## Indian Market News" in out

    def test_lookahead_safety_excludes_future_dated_items(self):
        # curr_date is BEFORE the ET sample's pubDates — a historical run must
        # not see them (#1220's rule, applied to this vendor too).
        with patch.object(india_news, "urlopen") as mock_open:
            mock_open.side_effect = lambda req, timeout=None: (
                _resp(_ET_SAMPLE.encode()) if "economictimes" in req.full_url
                else _resp(_MINT_SAMPLE.encode())
            )
            out = india_news.get_global_news_india("2026-09-10", look_back_days=7, limit=10)
        assert "Sensex, Nifty rally" not in out
        assert "No global news found between" in out

    def test_one_feed_failing_still_returns_the_other(self):
        with self._patch_feeds(et_result=None, mint_result=[
            {"title": "Mint story", "link": "l", "summary": "s",
             "pub_date": __import__("datetime").datetime(2026, 9, 17, tzinfo=__import__("datetime").timezone.utc),
             "source": "Mint"},
        ]):
            out = india_news.get_global_news_india("2026-09-17", look_back_days=7, limit=10)
        assert "Mint story" in out
        assert "unavailable (fetch failed): Economic Times" in out

    def test_all_feeds_failing_is_reported_as_unavailable_not_silence(self):
        with self._patch_feeds(et_result=None, mint_result=None):
            out = india_news.get_global_news_india("2026-09-17", look_back_days=7, limit=10)
        assert "India news unavailable" in out
        assert "not an absence of news" in out

    def test_limit_caps_returned_articles(self):
        import datetime as dt
        many = [
            {"title": f"Story {i}", "link": "l", "summary": "s",
             "pub_date": dt.datetime(2026, 9, 17, tzinfo=dt.timezone.utc), "source": "Mint"}
            for i in range(5)
        ]
        with self._patch_feeds(et_result=[], mint_result=many):
            out = india_news.get_global_news_india("2026-09-17", look_back_days=7, limit=2)
        assert out.count("### Story") == 2

    def test_dedup_by_title_across_feeds(self):
        import datetime as dt
        dupe = {"title": "Same headline", "link": "l1", "summary": "s",
                "pub_date": dt.datetime(2026, 9, 17, tzinfo=dt.timezone.utc), "source": "Economic Times"}
        dupe2 = {**dupe, "link": "l2", "source": "Mint"}
        with self._patch_feeds(et_result=[dupe], mint_result=[dupe2]):
            out = india_news.get_global_news_india("2026-09-17", look_back_days=7, limit=10)
        assert out.count("### Same headline") == 1


@pytest.mark.unit
def test_india_rss_registered_as_global_news_vendor():
    from tradingagents.dataflows.interface import VENDOR_METHODS

    assert VENDOR_METHODS["get_global_news"]["india_rss"] is india_news.get_global_news_india
