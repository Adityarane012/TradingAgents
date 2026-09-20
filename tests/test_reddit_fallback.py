"""Tests for the RSS-first Reddit fetcher, its 429 backoff, the opt-in JSON
path's degradation (#862), and chunked-transfer error handling (#1024)."""

from __future__ import annotations

import http.client
import json
import time
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import reddit

_SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>NVDA earnings beat, stock pops</title>
    <published>2026-05-20T14:30:00+00:00</published>
    <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;&lt;p&gt;Great &lt;b&gt;quarter&lt;/b&gt; for NVDA&amp;#39;s datacenter unit.&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt;</content>
  </entry>
  <entry>
    <title>Is NVDA overvalued?</title>
    <published>2026-05-19T09:00:00Z</published>
    <content type="html">&lt;p&gt;Forward P/E discussion&lt;/p&gt;</content>
  </entry>
</feed>
"""


def _resp(read_fn):
    """A minimal context-manager response whose read() runs ``read_fn``."""
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner, size=-1):
            data = read_fn()
            return data if size is None or size < 0 else data[:size]
    return _Resp()


def _atom_resp():
    return _resp(lambda: _SAMPLE_ATOM.encode("utf-8"))


def _raise(exc):
    def _r():
        raise exc
    return _resp(_r)


@pytest.mark.unit
class TestIsoToTimestamp:
    def test_parses_offset_and_z(self):
        assert reddit._iso_to_timestamp("2026-05-20T14:30:00+00:00") > 0
        assert reddit._iso_to_timestamp("2026-05-19T09:00:00Z") > 0

    def test_none_and_garbage_return_none(self):
        assert reddit._iso_to_timestamp(None) is None
        assert reddit._iso_to_timestamp("not-a-date") is None


@pytest.mark.unit
class TestStripHtml:
    def test_extracts_between_sc_markers_and_unescapes(self):
        raw = "<!-- SC_OFF --><div class=\"md\"><p>Great <b>quarter</b> &amp; more</p></div><!-- SC_ON -->"
        assert reddit._strip_html(raw) == "Great quarter & more"

    def test_empty(self):
        assert reddit._strip_html("") == ""


@pytest.mark.unit
class TestRssParsing:
    def test_parses_atom_entries(self):
        with patch.object(reddit, "urlopen", return_value=_atom_resp()):
            posts = reddit._fetch_subreddit_rss("NVDA", "stocks", limit=5, timeout=5.0)
        assert len(posts) == 2
        assert posts[0]["title"] == "NVDA earnings beat, stock pops"
        assert posts[0]["source"] == "rss"
        assert posts[0]["score"] is None
        assert posts[0]["num_comments"] is None
        assert posts[0]["created_utc"] > 0
        assert "datacenter unit" in posts[0]["selftext"]

    def test_malformed_xml_reports_unavailable(self):
        with patch.object(reddit, "urlopen", return_value=_resp(lambda: b"<<not xml>>")):
            assert reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0) is None


@pytest.mark.unit
class TestFetchSubredditIsRssFirst:
    """Without OAuth credentials, the per-subreddit fetch goes straight to
    RSS — it must not hit the WAF-blocked JSON endpoint, which only burned
    rate-limit budget (issue #862). See TestOAuthDispatch for the
    credentials-configured half of this behavior."""

    def test_delegates_to_rss_without_touching_json(self, monkeypatch):
        monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
        sentinel = [{"title": "x", "source": "rss", "score": None,
                     "num_comments": None, "created_utc": None, "selftext": ""}]
        with patch.object(reddit, "_fetch_subreddit_rss", return_value=sentinel) as rss, \
             patch.object(reddit, "urlopen",
                          side_effect=AssertionError("JSON endpoint must not be called")):
            out = reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
        rss.assert_called_once()
        assert out is sentinel


@pytest.mark.unit
class TestJsonPathFallsBackToRss:
    """The OAuth JSON path reports failure as None; _fetch_subreddit (the
    dispatcher, not _fetch_subreddit_json itself) is what falls back to RSS
    — see TestOAuthDispatch below for that half of the contract."""

    def test_403_returns_none(self):
        err = HTTPError("url", 403, "Blocked", {}, None)
        with patch.object(reddit, "urlopen", side_effect=err):
            out = reddit._fetch_subreddit_json("NVDA", "stocks", 5, 5.0, token="tok")
        assert out is None


@pytest.mark.unit
class TestRss429Backoff:
    def test_429_then_success_retries_once(self):
        err = HTTPError("url", 429, "Too Many Requests", {}, None)
        with patch.object(reddit, "urlopen", side_effect=[err, _atom_resp()]) as op, \
             patch.object(reddit.time, "sleep") as slept:
            posts = reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0)
        assert op.call_count == 2          # original + exactly one retry
        slept.assert_called_once()         # backed off before retrying
        assert len(posts) == 2

    def test_429_twice_gives_up_after_one_retry(self):
        err = HTTPError("url", 429, "Too Many Requests", {}, None)
        with patch.object(reddit, "urlopen", side_effect=[err, err]) as op, \
             patch.object(reddit.time, "sleep"):
            posts = reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0)
        assert op.call_count == 2          # one retry, then gives up cleanly
        assert posts is None

    def test_retry_after_header_is_honoured(self):
        err = HTTPError("url", 429, "Too Many Requests", {"Retry-After": "12"}, None)
        with patch.object(reddit, "urlopen", side_effect=[err, _atom_resp()]), \
             patch.object(reddit.time, "sleep") as slept:
            reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0)
        slept.assert_called_once_with(12.0)

    def test_retry_after_zero_is_honoured_not_treated_as_absent(self):
        # A valid "Retry-After: 0" means retry at once; it must not fall through
        # to the fallback wait (the earlier `or 5.0` bug turned 0 into 5s).
        err = HTTPError("url", 429, "Too Many Requests", {"Retry-After": "0"}, None)
        with patch.object(reddit, "urlopen", side_effect=[err, _atom_resp()]), \
             patch.object(reddit.time, "sleep") as slept:
            reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0)
        slept.assert_called_once_with(0.0)

    def test_headerless_429_fallback_is_jittered(self):
        # No Retry-After -> our own ~5s fallback, jittered so concurrent runs
        # don't retry in lockstep (kept within a tight band).
        err = HTTPError("url", 429, "Too Many Requests", {}, None)
        with patch.object(reddit, "urlopen", side_effect=[err, _atom_resp()]), \
             patch.object(reddit.time, "sleep") as slept:
            reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0)
        slept.assert_called_once()
        (wait,), _ = slept.call_args
        assert 48.0 <= wait <= 72.0  # 60s +/-20% jitter


@pytest.mark.unit
class TestChunkedTransferErrorsHandled:
    """IncompleteRead/RemoteDisconnected come from http.client and are NOT
    OSErrors, so they were previously uncaught and crashed the pipeline (#1024)."""

    def test_rss_incomplete_read_reports_unavailable(self):
        with patch.object(reddit, "urlopen", return_value=_raise(http.client.IncompleteRead(b""))):
            assert reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0) is None

    def test_json_incomplete_read_returns_none(self):
        with patch.object(reddit, "urlopen", return_value=_raise(http.client.IncompleteRead(b""))):
            out = reddit._fetch_subreddit_json("NVDA", "stocks", 5, 5.0, token="tok")
        assert out is None

    def test_oversized_rss_feed_is_refused_not_parsed(self):
        # A hostile/misbehaving endpoint streaming an unbounded body must not be
        # read into memory before parsing; overflow degrades to an empty feed.
        big = _resp(lambda: b"x" * 100)
        with patch.object(reddit, "_MAX_FEED_BYTES", 10), \
             patch.object(reddit, "urlopen", return_value=big):
            assert reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0) is None


@pytest.mark.unit
class TestFormatterHandlesRssPosts:
    def test_rss_posts_omit_fake_counts_and_note_source(self):
        rss_posts = [{
            "title": "NVDA pops", "score": None, "num_comments": None,
            "created_utc": reddit._iso_to_timestamp("2026-05-20T14:30:00Z"),
            "selftext": "great quarter", "source": "rss",
        }]
        with patch.object(reddit, "_fetch_subreddit", return_value=rss_posts):
            out = reddit.fetch_reddit_posts("NVDA", subreddits=("stocks",), inter_request_delay=0)
        assert "via RSS feed" in out
        assert "↑" not in out  # no fake score arrow
        assert "NVDA pops" in out
        assert "great quarter" in out

    def test_json_posts_still_show_counts(self):
        json_posts = [{
            "title": "NVDA pops", "score": 1234, "num_comments": 56,
            "created_utc": reddit._iso_to_timestamp("2026-05-20T14:30:00Z"),
            "selftext": "",
        }]
        with patch.object(reddit, "_fetch_subreddit", return_value=json_posts):
            out = reddit.fetch_reddit_posts("NVDA", subreddits=("stocks",), inter_request_delay=0)
        assert "1234↑" in out
        assert "56c" in out
        assert "via RSS" not in out


@pytest.mark.unit
class TestCryptoSearchTerm:
    """A crypto pair (BTC-USD) barely matches Reddit text; search the base (#1113)."""

    def _captured_ticker(self, ticker):
        seen = {}

        def fake_fetch(t, sub, limit, timeout, **kwargs):
            seen["ticker"] = t
            return []

        with patch.object(reddit, "_fetch_subreddit", side_effect=fake_fetch):
            reddit.fetch_reddit_posts(ticker, subreddits=("stocks",), inter_request_delay=0)
        return seen["ticker"]

    def test_crypto_pair_searches_base(self):
        assert self._captured_ticker("BTC-USD") == "BTC"

    def test_equity_passes_through(self):
        assert self._captured_ticker("NVDA") == "NVDA"


@pytest.mark.unit
class TestFailedFetchIsNotSilence:
    """A throttled fetch must not be rendered as "no posts found" (#1295).

    Returning [] for both a failed request and a genuinely empty search made the
    sentiment analyst read rate limiting as real silence ("r/stocks and
    r/investing are silent"), which is a signal that was never observed.
    """

    _POST = {
        "title": "NVDA pops", "score": None, "num_comments": None,
        "created_utc": reddit._iso_to_timestamp("2026-05-20T14:30:00Z"),
        "selftext": "", "source": "rss",
    }

    def _run(self, results):
        """Drive fetch_reddit_posts with a per-subreddit result sequence."""
        subs = tuple(f"s{i}" for i in range(len(results)))
        with patch.object(reddit, "_fetch_subreddit", side_effect=list(results)):
            return reddit.fetch_reddit_posts(
                "NVDA", subreddits=subs, inter_request_delay=0
            )

    def test_failed_subreddit_is_marked_unavailable_not_empty(self):
        out = self._run([None, [self._POST]])
        assert "unavailable" in out
        assert "no posts found" not in out.split("unavailable")[0]

    def test_all_sources_failing_does_not_claim_no_posts(self):
        out = self._run([None, None])
        assert "Reddit unavailable" in out
        assert "no Reddit posts found" not in out

    def test_mixed_failure_and_empty_only_claims_silence_for_searched_subs(self):
        # s0 failed, s1 genuinely returned nothing: the "no posts" claim must
        # cover only s1, with s0 reported separately as unavailable.
        out = self._run([None, []])
        assert "r/s1" in out.split("unavailable (fetch failed)")[0]
        assert "unavailable (fetch failed): r/s0" in out

    def test_genuine_empty_still_reports_no_posts(self):
        out = self._run([[], []])
        assert "no Reddit posts found" in out
        assert "unavailable" not in out

    def test_retry_is_not_spent_again_after_a_failure(self):
        # The 60s back-off must be paid at most once per run, so subsequent
        # subreddits are fetched with retry disabled rather than stalling.
        seen = []

        def record(t, sub, limit, timeout, _retry=True):
            seen.append(_retry)
            return None

        with patch.object(reddit, "_fetch_subreddit", side_effect=record):
            reddit.fetch_reddit_posts(
                "NVDA", subreddits=("a", "b", "c"), inter_request_delay=0
            )
        assert seen == [True, False, False]


@pytest.mark.unit
class TestIndiaRegionalRouting:
    """.NS/.BO tickers auto-route to Indian subreddits with the suffix
    stripped from the search term (#1346) — wallstreetbets/stocks/investing
    return nothing for NSE/BSE names, and nobody searches "RELIANCE.NS"."""

    def test_india_ticker_uses_india_subreddits_by_default(self):
        seen_subs = []

        def record(t, sub, limit, timeout, _retry=True):
            seen_subs.append(sub)
            return []

        with patch.object(reddit, "_fetch_subreddit", side_effect=record):
            reddit.fetch_reddit_posts("RELIANCE.NS", inter_request_delay=0)
        assert tuple(seen_subs) == reddit.INDIA_SUBREDDITS

    def test_us_ticker_still_uses_default_subreddits(self):
        seen_subs = []

        def record(t, sub, limit, timeout, _retry=True):
            seen_subs.append(sub)
            return []

        with patch.object(reddit, "_fetch_subreddit", side_effect=record):
            reddit.fetch_reddit_posts("AAPL", inter_request_delay=0)
        assert tuple(seen_subs) == reddit.DEFAULT_SUBREDDITS

    def test_explicit_subreddits_override_india_auto_selection(self):
        seen_subs = []

        def record(t, sub, limit, timeout, _retry=True):
            seen_subs.append(sub)
            return []

        with patch.object(reddit, "_fetch_subreddit", side_effect=record):
            reddit.fetch_reddit_posts(
                "RELIANCE.NS", subreddits=("stocks",), inter_request_delay=0
            )
        assert seen_subs == ["stocks"]

    def test_india_suffix_stripped_from_search_term(self):
        seen_tickers = []

        def record(t, sub, limit, timeout, _retry=True):
            seen_tickers.append(t)
            return []

        with patch.object(reddit, "_fetch_subreddit", side_effect=record):
            reddit.fetch_reddit_posts("HDFCBANK.BO", inter_request_delay=0)
        assert seen_tickers == ["HDFCBANK"] * len(reddit.INDIA_SUBREDDITS)

    def test_non_india_ticker_search_term_unchanged(self):
        seen_tickers = []

        def record(t, sub, limit, timeout, _retry=True):
            seen_tickers.append(t)
            return []

        with patch.object(reddit, "_fetch_subreddit", side_effect=record):
            reddit.fetch_reddit_posts("AAPL", inter_request_delay=0)
        assert seen_tickers == ["AAPL"] * len(reddit.DEFAULT_SUBREDDITS)


# ── OAuth token caching + dispatch (#1352) ───────────────────────────

def _token_resp(access_token="tok-abc", expires_in=3600):
    body = json.dumps({"access_token": access_token, "expires_in": expires_in}).encode()
    return _resp(lambda: body)


@pytest.fixture(autouse=True)
def _reset_oauth_cache():
    """The token cache is module-level state — must not leak between tests."""
    reddit._oauth_token_cache["token"] = None
    reddit._oauth_token_cache["expires_at"] = 0.0
    yield
    reddit._oauth_token_cache["token"] = None
    reddit._oauth_token_cache["expires_at"] = 0.0


@pytest.mark.unit
class TestOAuthToken:
    def test_no_credentials_returns_none(self, monkeypatch):
        monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
        assert reddit._get_oauth_token() is None

    def test_only_one_credential_set_returns_none(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
        assert reddit._get_oauth_token() is None

    def test_fetches_and_returns_token(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        with patch.object(reddit, "urlopen", return_value=_token_resp("tok-1")) as op:
            token = reddit._get_oauth_token()
        assert token == "tok-1"
        op.assert_called_once()

    def test_second_call_uses_cache_not_a_new_request(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        with patch.object(reddit, "urlopen", return_value=_token_resp("tok-1")) as op:
            first = reddit._get_oauth_token()
            second = reddit._get_oauth_token()
        assert first == second == "tok-1"
        op.assert_called_once()

    def test_expired_token_is_refetched(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        with patch.object(reddit, "urlopen", return_value=_token_resp("tok-1", expires_in=3600)):
            reddit._get_oauth_token()
        # Force the cached token to look expired.
        reddit._oauth_token_cache["expires_at"] = time.time() - 1
        with patch.object(reddit, "urlopen", return_value=_token_resp("tok-2")) as op:
            token = reddit._get_oauth_token()
        assert token == "tok-2"
        op.assert_called_once()

    def test_token_endpoint_failure_returns_none(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        with patch.object(reddit, "urlopen", side_effect=HTTPError("url", 401, "unauthorized", {}, None)):
            assert reddit._get_oauth_token() is None

    def test_malformed_token_response_returns_none(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        bad = _resp(lambda: b'{"no_access_token_here": true}')
        with patch.object(reddit, "urlopen", return_value=bad):
            assert reddit._get_oauth_token() is None

    def test_uses_basic_auth_and_client_credentials_grant(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "myid")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "mysecret")
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["auth_header"] = req.headers.get("Authorization")
            seen["body"] = req.data
            seen["url"] = req.full_url
            return _token_resp("tok-1")

        with patch.object(reddit, "urlopen", side_effect=fake_urlopen):
            reddit._get_oauth_token()
        assert seen["url"] == reddit._TOKEN_URL
        assert seen["auth_header"].startswith("Basic ")
        assert b"grant_type=client_credentials" in seen["body"]


@pytest.mark.unit
class TestOAuthDispatch:
    """_fetch_subreddit: OAuth-first when configured, RSS otherwise or on
    OAuth failure — this is where the RSS-fallback contract now lives."""

    def test_no_credentials_goes_straight_to_rss(self, monkeypatch):
        monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
        monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
        with patch.object(reddit, "_fetch_subreddit_json") as json_fetch, \
             patch.object(reddit, "_fetch_subreddit_rss", return_value=[]) as rss:
            reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
        json_fetch.assert_not_called()
        rss.assert_called_once()

    def test_oauth_success_skips_rss_entirely(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        oauth_posts = [{"title": "x", "score": 10, "num_comments": 2,
                         "created_utc": 1700000000, "selftext": ""}]
        with patch.object(reddit, "_get_oauth_token", return_value="tok"), \
             patch.object(reddit, "_fetch_subreddit_json", return_value=oauth_posts) as json_fetch, \
             patch.object(reddit, "_fetch_subreddit_rss") as rss:
            out = reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
        json_fetch.assert_called_once()
        rss.assert_not_called()
        assert out == oauth_posts

    def test_oauth_failure_falls_back_to_rss(self, monkeypatch):
        monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
        monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
        rss_posts = [{"title": "y", "score": None, "num_comments": None,
                      "created_utc": None, "selftext": "", "source": "rss"}]
        with patch.object(reddit, "_get_oauth_token", return_value="tok"), \
             patch.object(reddit, "_fetch_subreddit_json", return_value=None) as json_fetch, \
             patch.object(reddit, "_fetch_subreddit_rss", return_value=rss_posts) as rss:
            out = reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
        json_fetch.assert_called_once()
        rss.assert_called_once()
        assert out == rss_posts
