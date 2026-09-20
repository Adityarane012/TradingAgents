"""Reddit search fetcher for ticker-specific discussion posts.

Default path (no credentials configured) is Reddit's public Atom/RSS search
feed (``reddit.com/r/{sub}/search.rss``). The richer JSON search endpoint
(``/search.json``) is reliably WAF-blocked (``HTTP 403``) for anonymous
clients (issue #862), and probing it on every call only doubled our request
volume against Reddit's per-IP rate limit — tripping ``429`` on the RSS
fallback — so it was kept (``_fetch_subreddit_json``) but not used by
default. On a 429 the RSS path backs off once (honouring ``Retry-After``).
RSS also lacks score / comment counts, so those posts are marked and the
formatter omits the metrics rather than printing fake zeros.

Optional OAuth path (set ``REDDIT_CLIENT_ID`` + ``REDDIT_CLIENT_SECRET``,
#1352): a Reddit "script" app (reddit.com/prefs/apps; Reddit may require
approval via its Data Access Request form to create one) gets a
``client_credentials`` token and switches every fetch to the
JSON search endpoint via ``oauth.reddit.com``. This is the actual fix for
the RSS path's rate-limit backoffs, not a band-aid on top of them — an OAuth
client has its own per-client budget instead of sharing the anonymous
per-IP pool that got hit repeatedly in a multi-ticker batch run, and JSON
carries real score/comment counts RSS cannot. Falls back to the RSS path on
any OAuth failure (bad credentials, token-endpoint error, ...) rather than
raising, so a misconfigured or revoked app degrades instead of breaking the
run.

A fetch that fails is reported as ``<unavailable>``, never as "no posts found":
the two are different claims, and passing a rate-limited fetch off as silence
hands the sentiment analyst a signal that was never observed (#1295).

No API key required for the default path. Returns formatted plaintext blocks
ready for prompt injection and degrades gracefully — returns a placeholder
string rather than raising, so callers never special-case missing data.
"""

from __future__ import annotations

import base64
import html
import http.client
import json
import logging
import os
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .date_window import in_window
from .symbol_utils import crypto_base, is_india_ticker, strip_india_suffix

logger = logging.getLogger(__name__)


def _within_window(posts, start_date, end_date):
    """Keep only posts published in [start_date, end_date] (look-ahead safe).

    No window (both None) leaves the list untouched for live callers. A post with
    no ``created_utc`` epoch is dropped in a historical window (#1220).
    """
    if not (start_date and end_date):
        return posts
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    kept = []
    for p in posts:
        ts = p.get("created_utc")
        created = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
        if in_window(created, start_dt, end_dt):
            kept.append(p)
    return kept

_API = "https://oauth.reddit.com/r/{sub}/search?{qs}"
_RSS = "https://www.reddit.com/r/{sub}/search.rss?{qs}"
_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
# A descriptive, identified User-Agent (per Reddit's API etiquette). Reddit
# blocks generic/anonymous tokens like bare "Mozilla/5.0" or "curl/…" but
# serves this one on both endpoints; the RSS feed accepts it even when the
# JSON search endpoint 403s, so no browser-spoofing is needed.
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# Default subreddits ordered roughly by signal density for ticker-specific
# discussion. wallstreetbets has the most volume but most noise; stocks /
# investing trend more measured. Caller can override.
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")

# Indian retail/investing subreddits, used automatically for .NS/.BO/.NSE/.BSE
# tickers when the caller does not pass an explicit `subreddits` override.
# wallstreetbets/stocks/investing return essentially nothing for NSE/BSE
# names (they're US-market communities), so the sentiment analyst otherwise
# always sees an empty Reddit block and defaults to Neutral/low-confidence
# regardless of actual retail sentiment.
#
# Verified live against Reddit's RSS feed on 2026-09-17 (each subreddit's own
# feed, not a search — confirms the community exists and is active, not just
# that the name resolves):
#   r/IndianStreetBets — active, newest post from today.
#   r/IndiaInvestments  — active, newest post 3 days old.
# r/DalalStreet was tried and dropped: the subreddit exists but its RSS
# feed's newest post was from January 2024 — effectively dead, so including
# it would just add a guaranteed "<no posts found>" block to every report
# rather than real signal. r/stocks is kept third for ADR-related discussion
# (Infosys/INFY, Wipro/WIT, ICICI Bank/IBN, etc. trade on US exchanges and
# get discussed there).
INDIA_SUBREDDITS = ("IndianStreetBets", "IndiaInvestments", "stocks")

# Cached client_credentials token: (token, expiry_epoch). A lock guards
# refresh so concurrent callers (e.g. several subreddits fetched close
# together) don't each fire their own token request.
_oauth_token_cache: dict[str, object] = {"token": None, "expires_at": 0.0}
_oauth_lock = threading.Lock()

# Refresh this many seconds before Reddit's own expiry to avoid a token that
# goes stale mid-request.
_TOKEN_REFRESH_MARGIN = 60.0


def _get_oauth_token(timeout: float = 10.0) -> str | None:
    """Return a cached (or freshly fetched) OAuth bearer token, or ``None``.

    ``None`` means either no credentials are configured (the common case —
    OAuth is opt-in) or the token request itself failed; both are treated
    identically by callers, which fall back to the RSS path. Reddit's
    ``client_credentials`` grant needs only the app's id/secret (no separate
    Reddit account login), matching a "script" app created at
    reddit.com/prefs/apps.
    """
    client_id = os.getenv("REDDIT_CLIENT_ID")
    client_secret = os.getenv("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None

    with _oauth_lock:
        cached_token = _oauth_token_cache["token"]
        if cached_token and time.time() < _oauth_token_cache["expires_at"]:
            return cached_token

        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        body = urlencode({"grant_type": "client_credentials"}).encode()
        req = Request(
            _TOKEN_URL,
            data=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": _UA,
            },
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read())
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            logger.warning(
                "Reddit OAuth token request failed: %s — falling back to the RSS path.", exc
            )
            return None

        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not token or not isinstance(expires_in, (int, float)):
            logger.warning(
                "Reddit OAuth token response missing access_token/expires_in — "
                "falling back to the RSS path."
            )
            return None

        _oauth_token_cache["token"] = token
        _oauth_token_cache["expires_at"] = time.time() + expires_in - _TOKEN_REFRESH_MARGIN
        return token


def _search_qs(ticker: str, limit: int) -> str:
    return urlencode({
        "q": ticker,
        "restrict_sr": "on",
        "sort": "new",
        "t": "week",  # last 7 days
        "limit": limit,
    })


def _iso_to_timestamp(iso_str: str | None) -> float | None:
    """Parse an Atom ``published`` timestamp to a UTC epoch, or None."""
    if not iso_str:
        return None
    try:
        normalized = iso_str[:-1] + "+00:00" if iso_str.endswith("Z") else iso_str
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return None


def _strip_html(content: str) -> str:
    """Reduce the HTML body Reddit embeds in an Atom entry to plain text."""
    if not content:
        return ""
    # Reddit wraps the real selftext between SC_OFF / SC_ON markers.
    if "<!-- SC_OFF -->" in content and "<!-- SC_ON -->" in content:
        content = content.split("<!-- SC_OFF -->")[1].split("<!-- SC_ON -->")[0]
    text = re.sub(r"<[^>]+>", " ", content)
    return " ".join(html.unescape(text).split())


# Headerless-429 backoff when Reddit gives no Retry-After. Measured against
# /r/{sub}/search.rss, a retry still 429s at 8s, 10s and 30s of spacing and
# succeeds at 60s, so a shorter wait spends the one retry on a request that
# cannot succeed (#1295). Jittered so several analyses sharing an IP don't
# retry in lockstep and re-collide on the limit.
_RETRY_FALLBACK_SECONDS = 60.0


def _jitter(seconds: float, frac: float = 0.2) -> float:
    """Return ``seconds`` with +/-``frac`` random jitter, to desynchronize
    concurrent runs pacing against the same per-IP limit."""
    return seconds * (1.0 + random.uniform(-frac, frac))


def _retry_after_seconds(exc: HTTPError) -> float | None:
    """Seconds to wait from a 429's ``Retry-After`` header, capped at 60s.

    The cap matches ``_RETRY_FALLBACK_SECONDS``: honouring less than we would
    wait on our own would spend the one retry on a request we already know is
    too early.

    Returns ``None`` only when the header is absent or unparseable; a valid
    ``Retry-After: 0`` returns ``0.0`` (retry at once), not ``None``.
    """
    try:
        val = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
        return min(float(val), 60.0) if val is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


# Reddit search feeds are small (a page of results); cap the read so a
# compromised or misbehaving endpoint can't stream an unbounded body into
# memory before we parse it. Overflow raises http.client.HTTPException, which
# both fetch paths already treat as a failed fetch (degrade to empty / RSS).
_MAX_FEED_BYTES = 5 * 1024 * 1024


def _read_capped(resp) -> bytes:
    """Read a response body bounded to ``_MAX_FEED_BYTES``, raising on overflow."""
    data = resp.read(_MAX_FEED_BYTES + 1)
    if len(data) > _MAX_FEED_BYTES:
        raise http.client.HTTPException(
            f"Reddit feed exceeded {_MAX_FEED_BYTES} bytes; refusing to parse"
        )
    return data


def _fetch_subreddit_rss(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
    _retry: bool = True,
) -> list[dict] | None:
    """Default path: parse the public Atom search feed for a subreddit.

    Carries no score / comment counts, so those fields are left None and the
    post is tagged ``source="rss"`` for honest display. On a 429 (Reddit's
    per-IP rate limit) we back off once — honouring ``Retry-After`` when
    present — before giving up, so a transient burst doesn't blank the feed.

    Returns ``[]`` when the search ran and matched nothing, and ``None`` when
    the fetch itself failed. The caller must keep these apart: rendering a
    failed fetch as "no posts found" hands the sentiment analyst an absence of
    discussion that was never observed (#1295).
    """
    url = _RSS.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(url, headers={"User-Agent": _UA})
    try:
        with urlopen(req, timeout=timeout) as resp:
            root = ET.fromstring(_read_capped(resp))
    except HTTPError as exc:
        if exc.code == 429 and _retry:
            # Honour a server-supplied Retry-After exactly (including 0); jitter
            # only our own fallback so concurrent runs don't retry in lockstep.
            retry_after = _retry_after_seconds(exc)
            wait = retry_after if retry_after is not None else _jitter(_RETRY_FALLBACK_SECONDS)
            logger.warning(
                "Reddit RSS 429 for r/%s · %s — backing off %.1fs then retrying once",
                sub, ticker, wait,
            )
            time.sleep(wait)
            return _fetch_subreddit_rss(ticker, sub, limit, timeout, _retry=False)
        logger.warning("Reddit RSS fetch failed for r/%s · %s: %s", sub, ticker, exc)
        return None
    except (OSError, http.client.HTTPException, ET.ParseError) as exc:
        # OSError covers URLError/TimeoutError/connection resets; HTTPException
        # covers chunked-transfer errors (IncompleteRead/BadStatusLine, #1024).
        logger.warning("Reddit RSS fetch failed for r/%s · %s: %s", sub, ticker, exc)
        return None

    posts = []
    for entry in root.findall("atom:entry", _ATOM_NS)[:limit]:
        title_el = entry.find("atom:title", _ATOM_NS)
        published_el = entry.find("atom:published", _ATOM_NS)
        content_el = entry.find("atom:content", _ATOM_NS)
        posts.append({
            "title": (title_el.text if title_el is not None else "") or "",
            "score": None,
            "num_comments": None,
            "created_utc": _iso_to_timestamp(
                published_el.text if published_el is not None else None
            ),
            "selftext": _strip_html(content_el.text if content_el is not None else ""),
            "source": "rss",
        })
    return posts


def _fetch_subreddit_json(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
    token: str,
) -> list[dict] | None:
    """OAuth JSON search path (carries real score / comment counts).

    Only ever called with a valid bearer ``token`` — without OAuth, Reddit's
    WAF returns ``403 Blocked`` on this endpoint for anonymous clients
    (issue #862). Returns ``None`` (not ``[]``) on any failure so the caller
    falls back to RSS rather than reporting a failure as "no posts" (#1295).
    """
    url = _API.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(
        url,
        headers={
            "User-Agent": _UA,
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(_read_capped(resp))
        children = (payload.get("data") or {}).get("children") or []
        return [c.get("data", {}) for c in children if isinstance(c, dict)]
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning(
            "Reddit OAuth fetch failed for r/%s · %s: %s — falling back to RSS feed.",
            sub, ticker, exc,
        )
        return None


def _fetch_subreddit(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
    _retry: bool = True,
) -> list[dict] | None:
    """Fetch one subreddit. ``None`` means the fetch failed.

    Tries the OAuth JSON path first when ``REDDIT_CLIENT_ID``/
    ``REDDIT_CLIENT_SECRET`` are configured — an OAuth client has its own
    per-client rate budget rather than sharing the anonymous per-IP pool, and
    JSON carries real score/comment counts RSS cannot. Falls back to RSS on
    any OAuth failure (no credentials, bad credentials, a failed token
    request, or the search call itself failing) so a misconfigured app
    degrades instead of losing Reddit data for the run.
    """
    token = _get_oauth_token(timeout=timeout)
    if token is not None:
        posts = _fetch_subreddit_json(ticker, sub, limit, timeout, token)
        if posts is not None:
            return posts
    return _fetch_subreddit_rss(ticker, sub, limit, timeout, _retry=_retry)


def fetch_reddit_posts(
    ticker: str,
    subreddits: Iterable[str] | None = None,
    limit_per_sub: int = 5,
    timeout: float = 10.0,
    inter_request_delay: float = 1.0,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str:
    """Fetch recent Reddit posts mentioning ``ticker`` across finance
    subreddits and return them as a formatted plaintext block.

    ``subreddits=None`` (the default) auto-selects: ``INDIA_SUBREDDITS`` for
    an NSE/BSE-suffixed ticker, ``DEFAULT_SUBREDDITS`` otherwise. Pass an
    explicit iterable to override for either case.

    ``inter_request_delay`` paces the per-subreddit requests to stay under
    Reddit's public per-IP rate limit on the RSS path (the default, no
    credentials configured). With ``REDDIT_CLIENT_ID``/``REDDIT_CLIENT_SECRET``
    set, requests go through Reddit's OAuth JSON endpoint instead, which has
    its own per-client budget rather than sharing the anonymous per-IP pool —
    the actual fix for repeated 429s in a multi-ticker batch run, not just a
    slower pace against the same limit.

    When ``start_date``/``end_date`` (yyyy-mm-dd) are given, posts are trimmed to
    that window so a historical run does not leak current discussion into a
    backtest (#1220).
    """
    india = is_india_ticker(ticker)
    if subreddits is None:
        subreddits = INDIA_SUBREDDITS if india else DEFAULT_SUBREDDITS
    # Crypto reaches us as a Yahoo pair (BTC-USD); search Reddit for the base
    # ("BTC") so the query actually matches discussion instead of near-nothing.
    # Indian tickers reach us with an exchange suffix (RELIANCE.NS) that
    # retail posts never spell out — search for the bare NSE/BSE root instead.
    ticker = crypto_base(ticker) or (strip_india_suffix(ticker) if india else ticker)
    subreddits = list(subreddits)
    blocks = []
    total_posts = 0
    unavailable = []
    allow_retry = True
    for i, sub in enumerate(subreddits):
        if i > 0 and inter_request_delay:
            time.sleep(_jitter(inter_request_delay))
        fetched = _fetch_subreddit(ticker, sub, limit_per_sub, timeout, _retry=allow_retry)
        if fetched is None:
            # A failed fetch is not an absence of discussion, so it must not be
            # rendered as "no posts found" (#1295). One failure also means the
            # per-IP budget is likely gone, so skip the (now 60s) back-off on
            # the remaining subreddits rather than stalling the run on retries
            # that cannot succeed; #1286 tracks coordinating this properly.
            allow_retry = False
            unavailable.append(sub)
            blocks.append(f"r/{sub}: <unavailable: fetch failed, not an absence of posts>")
            continue
        posts = _within_window(fetched, start_date, end_date)
        total_posts += len(posts)
        if not posts:
            blocks.append(f"r/{sub}: <no posts found mentioning {ticker.upper()} in the past 7 days>")
            continue

        via_rss = any(p.get("source") == "rss" for p in posts)
        header = f"r/{sub} — {len(posts)} recent posts mentioning {ticker.upper()}"
        header += " (via RSS feed; scores/comments unavailable):" if via_rss else ":"
        lines = [header]
        for p in posts:
            title = (p.get("title") or "").replace("\n", " ").strip()
            score = p.get("score")
            comments = p.get("num_comments")
            created = p.get("created_utc")
            created_str = (
                time.strftime("%Y-%m-%d", time.gmtime(created)) if created else "?"
            )
            # Score / comment counts are absent on the RSS fallback path —
            # show them only when present rather than printing fake zeros.
            meta = created_str
            if score is not None and comments is not None:
                meta += f" · {score:>4}↑ · {comments:>3}c"
            selftext = (p.get("selftext") or "").replace("\n", " ").strip()
            if len(selftext) > 240:
                selftext = selftext[:240] + "…"
            lines.append(
                f"  [{meta}] {title}"
                + (f"\n    body excerpt: {selftext}" if selftext else "")
            )
        blocks.append("\n".join(lines))

    if total_posts == 0:
        searched = [s for s in subreddits if s not in unavailable]
        if not searched:
            # Every source failed: claiming "no posts" here would assert a
            # silence we never observed.
            return (
                f"<Reddit unavailable: every source failed to fetch "
                f"({', '.join(f'r/{s}' for s in unavailable)}); this is not an "
                f"absence of discussion>"
            )
        summary = (
            f"<no Reddit posts found mentioning {ticker.upper()} across "
            f"{', '.join(f'r/{s}' for s in searched)} in the past 7 days>"
        )
        if unavailable:
            summary += (
                f"\n<unavailable (fetch failed): "
                f"{', '.join(f'r/{s}' for s in unavailable)}>"
            )
        return summary
    return "\n\n".join(blocks)
